"""Playwright smoke harness: serve the REAL app (StaticFiles web/ + /api/*) against a
seeded golden DB, drive a headless chromium browser, and assert ZERO console errors +
ZERO uncaught page errors per page (spec 19, Task 0.3).

Design (subprocess uvicorn + on-disk golden DB — see the module brief):
- We spawn uvicorn in its OWN process pointed at an on-disk SQLite file seeded by the
  exact spec-17 `tests/conftest.py::_seed_golden` sequence (DRY: helpers reused, not
  re-derived). A subprocess + file DB sidesteps the shared-in-memory-connection
  cross-thread concurrency a real browser's parallel requests provoke in an in-process
  threaded server.
- The page-smoke assertion attaches BOTH `page.on("console", ...)` (kept: type=="error")
  AND `page.on("pageerror", ...)` (uncaught JS exceptions — where a Decimal-string
  `.toFixed` TypeError surfaces once Phase-2 wires pages to /api). Both lists must be
  empty.

Socket exception (the spec-17-sanctioned loopback exception):
  `pyproject.toml` sets `--disable-socket --allow-unix-socket`, a global network ban.
  The PARENT (pytest) process needs TCP loopback for the free-port probe and the
  readiness poll of `http://127.0.0.1:PORT`. We re-enable sockets RESTRICTED TO
  LOOPBACK ("127.0.0.1"/"localhost") for the duration of `tests/e2e` only, then restore
  the ban on teardown. Real EXTERNAL network stays banned. (The uvicorn subprocess runs
  outside pytest_socket entirely; only the parent's probe/poll needs this exception.)
"""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.api.auth_store import create_user

# Reuse the spec-17 golden-DB seed sequence verbatim (DRY) — see tests/conftest.py.
from tests.conftest import (
    GOLDEN_NOW,
    _seed_golden,
    bootstrap_db,
    create_auth_tables,
    create_pricing_tables,
    create_scheduler_tables,
    datasources_store,
    ensure_alert_events_tables,
    ensure_alert_rules_seeded,
    ensure_composer_seeded,
    ensure_evaluations_tables,
    ensure_insights_tables,
    ensure_system_prompt_seeded,
    init_golden_base,
    snapshots_store,
)

# Worktree root (this app's served web/ + portfolio_dash source).
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]

_READINESS_TIMEOUT_S = 30.0
_READINESS_POLL_S = 0.25


@pytest.fixture(scope="session", autouse=True)
def _e2e_loopback_socket() -> Iterator[None]:
    """Spec-17-sanctioned loopback exception, scoped to tests/e2e ONLY.

    Re-enable sockets but restrict to loopback so the parent can probe a free port and
    poll the subprocess server for readiness; restore the global ban on teardown so no
    external network leaks. Autouse keeps the window open for the whole e2e session
    (port probe, readiness poll, and any incidental parent-side loopback I/O).
    """
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    try:
        yield
    finally:
        disable_socket(allow_unix_socket=True)


def _build_golden_db(path: Path) -> None:
    """Seed an on-disk golden DB at `path` via the SAME ordered setup as
    tests/conftest.py::golden_db (lines 106-118), reusing the real write paths."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        bootstrap_db(conn)
        create_pricing_tables(conn)
        create_scheduler_tables(conn)
        snapshots_store.ensure_tables(conn)
        datasources_store.ensure_seeded(conn)
        ensure_alert_rules_seeded(conn)
        create_auth_tables(conn)  # empty auth tables -> guest mode (no login needed)
        ensure_system_prompt_seeded(conn)
        ensure_composer_seeded(conn)
        ensure_insights_tables(conn)
        ensure_alert_events_tables(conn)
        ensure_evaluations_tables(conn)
        _seed_golden(conn)  # commits internally
        conn.commit()
    finally:
        conn.close()


def _free_port() -> int:
    """Bind to an ephemeral port, read it, release it (inside the socket-enabled window)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(base_url: str, proc: "subprocess.Popen[bytes]", stderr_path: Path) -> None:
    """Poll <base_url>/api/health until 200, else fail loudly with subprocess stderr."""
    deadline = time.monotonic() + _READINESS_TIMEOUT_S
    health_url = base_url + "/api/health"
    last_err: str = "no response yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # subprocess died before becoming ready
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(
                f"uvicorn subprocess exited early (code {proc.returncode}).\n"
                f"--- stderr ---\n{stderr}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=2.0) as resp:  # noqa: S310 (loopback)
                if resp.status == 200:
                    return
                last_err = f"HTTP {resp.status}"
        except urllib.error.URLError as exc:
            last_err = repr(exc)
        except OSError as exc:
            last_err = repr(exc)
        time.sleep(_READINESS_POLL_S)
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    raise TimeoutError(
        f"server not ready at {health_url} within {_READINESS_TIMEOUT_S}s "
        f"(last: {last_err}).\n--- stderr ---\n{stderr}"
    )


@pytest.fixture(scope="session")
def live_server(_e2e_loopback_socket: None) -> Iterator[str]:
    """Spawn a uvicorn subprocess serving create_app() against a seeded golden DB file.

    Yields the base URL (e.g. http://127.0.0.1:PORT). The subprocess runs in its own
    process (outside pytest_socket), so it freely binds the loopback listener; the
    parent only needs the loopback exception for the port probe + readiness poll.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="pd_e2e_"))
    db_path = tmp_dir / "golden.db"
    stderr_path = tmp_dir / "uvicorn.stderr.log"
    _build_golden_db(db_path)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "PD_DISABLE_SCHEDULER": "1",  # no APScheduler / background external calls
    }
    stderr_file = stderr_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "portfolio_dash.api.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=str(_WORKTREE_ROOT),  # served web/ + portfolio_dash are the worktree's
        env=env,
        stdout=stderr_file,
        stderr=stderr_file,
    )
    try:
        _wait_ready(base_url, proc, stderr_path)
        yield base_url
    finally:
        stderr_file.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        try:
            db_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass  # best-effort temp cleanup; never fail teardown on it


@pytest.fixture(scope="session")
def browser_page(_e2e_loopback_socket: None) -> Iterator[Page]:
    """A headless chromium Page for the e2e session. Each test should fully drive a
    navigation; the page is shared but the per-page handlers in `assert_page_ok` are
    detached after each assertion so listeners don't accumulate across tests."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            yield page
        finally:
            page.close()
            browser.close()


def assert_page_ok(
    page: Page,
    base_url: str,
    path: str,
    root_selector: str = "body",
) -> None:
    """Navigate to `base_url + path`, wait for `root_selector`, and assert the page
    produced ZERO console errors AND ZERO uncaught page errors.

    Reusable by Phase-2 per-page smokes. Import as:
        from tests.e2e.conftest import assert_page_ok
        assert_page_ok(page, base_url, "/some.html")  # root_selector optional

    Catches Decimal-string `.toFixed` TypeErrors (surface via pageerror) once pages are
    wired to /api. Handlers are detached after the assertion so a shared session Page
    does not accumulate listeners across tests.
    """
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: object) -> None:
        # msg is playwright.sync_api.ConsoleMessage; keep only error-level entries.
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    try:
        page.goto(base_url + path, wait_until="load")
        page.wait_for_selector(root_selector)
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"{path}: console errors={console_errors!r}; page errors={page_errors!r}"
    )


# --- isolated per-flow servers + pages (spec-17 §17.5 E1-E10) ---------------------
#
# Write/auth flows (manual buy, CSV import, oversell ack, login loop, AI input) mutate
# the DB or the auth mode, so they CANNOT share the session `live_server` (guest, subset
# golden) without polluting later tests. The `flow_server` factory spawns an ISOLATED
# uvicorn subprocess against a fresh on-disk DB seeded by a caller-supplied function, so
# each flow is order-independent and reproducible. `fresh_page` gives each flow its own
# browser context (clean cookies/localStorage — required for the login + bell flows).

SeedFn = Callable[[sqlite3.Connection], None]
FlowServerFactory = Callable[..., str]


def _terminate(proc: "subprocess.Popen[bytes]") -> None:
    """Reap a uvicorn subprocess robustly (terminate -> wait -> kill -> wait).

    Single-process server (no --workers/--reload), so terminate/kill reaps it; the final
    taskkill tree-kill on Windows is belt-and-suspenders against any orphan, only if the
    process is somehow still alive after kill()."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    if sys.platform == "win32" and proc.poll() is None:  # pragma: no cover (defensive)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)


@pytest.fixture
def flow_server(_e2e_loopback_socket: None) -> Iterator[FlowServerFactory]:
    """Factory: spawn an ISOLATED uvicorn subprocess against a fresh on-disk DB seeded
    by `seed`. Optional `users=[(username, password), ...]` makes the DB protected
    (spec-09) for the login flow. Returns the base URL. All spawns are torn down at the
    end of the test."""
    procs: list[subprocess.Popen[bytes]] = []
    handles: list[object] = []
    tmp_dirs: list[Path] = []

    def _make(
        seed: SeedFn,
        *,
        users: list[tuple[str, str]] | None = None,
    ) -> str:
        tmp_dir = Path(tempfile.mkdtemp(prefix="pd_flow_"))
        tmp_dirs.append(tmp_dir)
        db_path = tmp_dir / "flow.db"
        stderr_path = tmp_dir / "uvicorn.stderr.log"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            init_golden_base(conn)
            seed(conn)
            for username, password in users or []:
                create_user(conn, username=username, name=username,
                            password=password, now=GOLDEN_NOW)
            conn.commit()
        finally:
            conn.close()

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = {**os.environ, "DB_PATH": str(db_path), "PD_DISABLE_SCHEDULER": "1"}
        stderr_file = stderr_path.open("wb")
        handles.append(stderr_file)
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "portfolio_dash.api.app:create_app", "--factory",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=str(_WORKTREE_ROOT), env=env, stdout=stderr_file, stderr=stderr_file,
        )
        procs.append(proc)
        _wait_ready(base_url, proc, stderr_path)
        return base_url

    try:
        yield _make
    finally:
        for proc in procs:
            _terminate(proc)
        for h in handles:
            try:
                h.close()  # type: ignore[attr-defined]
            except OSError:
                pass
        for d in tmp_dirs:
            try:
                for child in d.iterdir():
                    child.unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                pass  # best-effort temp cleanup; never fail teardown on it


@pytest.fixture
def fresh_page(browser_page: Page) -> Iterator[Page]:
    """A browser context+page with clean cookies/localStorage per flow test.

    Reuses the session browser (via the existing `browser_page`'s underlying Browser) —
    a second `sync_playwright()` context would collide with the first ("Sync API inside
    the asyncio loop"). A new context isolates cookies/localStorage (the login + bell
    flows depend on a clean slate)."""
    browser = browser_page.context.browser
    assert browser is not None
    context = browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()
        context.close()
