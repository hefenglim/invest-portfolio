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

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page, Route, sync_playwright
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

# 60s ceiling (not 30s): each per-flow test spawns its OWN uvicorn subprocess + chromium
# context, so under full-suite load on Windows the subprocess cold-start can exceed a
# tight 30s ceiling — a resource/startup contention, NOT an app race. The poll already
# returns the instant the server answers (and fails fast if the subprocess dies), so a
# higher ceiling only absorbs slow starts; it is expect-polling, not a sleep/retry mask
# (spec-17 §17.7.4).
_READINESS_TIMEOUT_S = 60.0
_READINESS_POLL_S = 0.25

# --- diagnostic network log (issue #67) -------------------------------------------
#
# OFF unless ``PD_E2E_NETLOG=<dir>`` is set, so a normal gate run is byte-identical to
# before. When set, every server spawn/terminate (port + pid), every HTTP response with
# status >= 400 and every failed request is appended to ``<dir>/netlog.jsonl`` tagged
# with the owning test's nodeid, and each uvicorn's own stdout/stderr (which carries its
# ACCESS LOG) is copied out of the doomed temp dir. That pair answers the only question
# that matters about an intermittent 404 storm: did the SERVER see the request and answer
# 404 (a server/FS problem), or did the browser get a 404 the server never logged (a
# wrong-listener / third-party problem)?
_NETLOG_DIR = os.environ.get("PD_E2E_NETLOG")
_netlog_nodeid = "<session>"


def _netlog(event: str, **fields: object) -> None:
    """Append one JSON line to the diagnostic log. No-op unless PD_E2E_NETLOG is set."""
    if not _NETLOG_DIR:
        return
    try:
        target = Path(_NETLOG_DIR)
        target.mkdir(parents=True, exist_ok=True)
        row = {"t": round(time.time(), 3), "test": _netlog_nodeid, "event": event, **fields}
        with (target / "netlog.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:  # pragma: no cover (diagnostic only — never fail a test on it)
        pass


def _netlog_keep_server_log(stderr_path: Path, port: int, pid: int) -> None:
    """Copy a uvicorn subprocess log (with its access lines) out before teardown nukes it."""
    if not _NETLOG_DIR:
        return
    try:
        target = Path(_NETLOG_DIR)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stderr_path, target / f"uvicorn-{port}-{pid}.log")
    except OSError:  # pragma: no cover (diagnostic only)
        pass


def _netlog_watch(page: Page, label: str) -> None:
    """Attach response/requestfailed listeners that record 4xx/5xx + network failures."""
    if not _NETLOG_DIR:
        return

    def _on_response(resp: object) -> None:
        status = getattr(resp, "status", 0)
        if status >= 400:
            _netlog("http_error", page=label, status=status, url=getattr(resp, "url", "?"))

    def _on_requestfailed(req: object) -> None:
        _netlog("request_failed", page=label, url=getattr(req, "url", "?"),
                failure=getattr(req, "failure", None))

    page.on("response", _on_response)
    page.on("requestfailed", _on_requestfailed)


# --- third-party isolation (issue #67) --------------------------------------------
#
# THE BUG THIS EXISTS TO KILL. Every shipped page pulls its webfont from
# ``fonts.googleapis.com``, which fans out to 17-24 ``fonts.gstatic.com`` subset files per
# page. Google serves that stylesheet ``Cache-Control: private, max-age=86400,
# stale-while-revalidate=604800`` and ROTATES the subset filenames underneath it, so a
# stylesheet that is merely a few hours stale hands out URLs whose files Google has already
# retired — and then EVERY font subset on the page 404s at once. Our e2e tests assert ZERO
# browser console errors, so that remote rotation reddened ~2 random tests per full run
# (~1.5%/test), a different pair every time, with the unmistakable console text ``a status of
# 404 ()`` — the parentheses are EMPTY because HTTP/2 has no reason phrase, which is how the
# failure was finally distinguished from our own uvicorn (HTTP/1.1, always ``404 (Not
# Found)``). Measured 2026-08-11: the exact subset URL a failing run requested still 404s
# while the CURRENT stylesheet lists a different hash entirely.
#
# THE RULE. A browser under test talks to the app under test and to nothing else. A test
# suite whose verdict depends on a third party's cache-rotation schedule is not a test suite.
# Fonts are presentational: an EMPTY stylesheet means the page never fans out to gstatic at
# all, and the pages render in their declared fallback stack.
#
# SIMPLIFIED 2026-08-12 (ECharts vendored, owner ruling). This used to also fetch ECharts
# ONCE from ``cdn.jsdelivr.net`` and memoise the ~1 MB body on disk under ``.pytest_cache/``,
# with atomic writes, an unreachable-URL memo and a 30s timeout — machinery that existed
# solely because a FUNCTIONAL dependency lived off-origin. ``web/echarts.min.js`` is now
# served by the app itself, so there is nothing left to fetch: **this suite now makes ZERO
# outbound connections, ever.** Anything non-font reaching here is a NEW remote dependency and
# says so loudly rather than being silently satisfied from a cache.
#
# This is deliberately NOT a retry / timeout widening: nothing here waits for or re-attempts
# a remote host. The dependency is removed, not made more patient.

_LOCAL_URL_PREFIXES = (
    "http://127.0.0.1:", "http://localhost:",
    "https://127.0.0.1:", "https://localhost:",
)
_NON_NETWORK_SCHEMES = ("about:", "data:", "blob:", "file:", "chrome:", "devtools:")
_FONT_HOSTS = frozenset({"fonts.googleapis.com", "fonts.gstatic.com"})

# The hosts a PAGE may legitimately contact — a different set from `_FONT_HOSTS`, which is
# merely what the stub recognises as a font request. `fonts.gstatic.com` is deliberately NOT
# here: the stylesheet stub is empty, so the subset fan-out never starts, and a request to
# gstatic means the stub broke. Imported by the tests that enumerate hosts, so the ruling's one
# carve-out (owner 2026-08-12: vendor ECharts, leave the webfont remote) has a single home.
ALLOWED_REMOTE_HOSTS = frozenset({"fonts.googleapis.com"})

# Non-font URLs that were stubbed. Should stay EMPTY: the webfont is the only remote
# dependency the ruling left in place. Recorded (and warned about once per URL) so a newly
# introduced CDN is visible in the run output instead of quietly succeeding against a stub.
_stubbed_third_party: list[str] = []


def _is_app_url(url: str) -> bool:
    """True for the app under test (loopback) and for non-network schemes."""
    return url.startswith(_LOCAL_URL_PREFIXES) or url.startswith(_NON_NETWORK_SCHEMES)


def _third_party_route(route: Route) -> None:
    """Serve every non-loopback request WITHOUT touching the network (see the brief above)."""
    url = route.request.url
    host = urllib.parse.urlparse(url).netloc
    if host in _FONT_HOSTS:
        # Presentational only. An empty stylesheet => zero gstatic subset requests.
        route.fulfill(status=200, content_type="text/css", body="")
        return
    if url not in _stubbed_third_party:
        _stubbed_third_party.append(url)
        _netlog("thirdparty_stubbed", url=url)
        # Never emit a browser-visible error for a third party: that is the whole defect.
        # Serve an empty 200 and say so loudly in the run's own output instead.
        warnings.warn(
            f"e2e: a NON-FONT third-party request was stubbed with an empty 200: {url}. "
            "Every functional front-end dependency is supposed to be VENDORED into web/ — "
            "see docs/reference/vendored-assets.md. Whatever needed this will now fail on "
            "its own terms.",
            stacklevel=1,
        )
    route.fulfill(status=200, content_type="application/javascript", body="")


def install_third_party_stub(context: BrowserContext) -> None:
    """Route every non-loopback request in `context` through the offline stub.

    CALL THIS ON EVERY BrowserContext an e2e test creates. `tests/e2e/
    test_thirdparty_isolation.py` fails the build when a browser-context creation in
    tests/e2e is not paired with this call, because a context that skips it silently
    re-adds a remote CDN to the suite's pass/fail criteria.
    """
    context.route(lambda url: not _is_app_url(url), _third_party_route)


@pytest.fixture(autouse=True)
def _netlog_current_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Tag every diagnostic line with the nodeid of the test that produced it."""
    global _netlog_nodeid
    _netlog_nodeid = request.node.nodeid
    _netlog("test_start")
    try:
        yield
    finally:
        _netlog("test_end")
        _netlog_nodeid = "<session>"


def _assert_loopback_window() -> None:
    """(Re-)open the loopback-only socket window. Idempotent.

    Several flow modules carry a per-test `_loopback_sockets` fixture whose teardown
    calls ``disable_socket()`` — under random test ordering that clobbers the
    session-wide window before a later module's session server first spins up. Every
    socket-needing seam below re-asserts the window at its own setup, so ordering can
    never break it. External hosts stay banned (loopback allow-list only).
    """
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)


@pytest.fixture(scope="session", autouse=True)
def _e2e_loopback_socket() -> Iterator[None]:
    """Spec-17-sanctioned loopback exception, scoped to tests/e2e ONLY.

    Re-enable sockets but restrict to loopback so the parent can probe a free port and
    poll the subprocess server for readiness; restore the global ban on teardown so no
    external network leaks. Autouse keeps the window open for the whole e2e session
    (port probe, readiness poll, and any incidental parent-side loopback I/O).
    """
    _assert_loopback_window()
    try:
        yield
    finally:
        disable_socket(allow_unix_socket=True)


@pytest.fixture(autouse=True)
def _e2e_loopback_per_test(_e2e_loopback_socket: None) -> Iterator[None]:
    """Re-assert the loopback window before EVERY e2e test (ordering-proof).

    A previously-run flow module's per-test teardown may have re-banned sockets; this
    keeps direct parent-side loopback I/O (urllib calls inside tests) working no matter
    which module ran first. The session fixture above still restores the global ban
    when tests/e2e ends.
    """
    _assert_loopback_window()
    yield


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
    _assert_loopback_window()  # session fixtures init lazily — self-heal (see helper)
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
    _netlog("spawn", kind="live_server", port=port, pid=proc.pid)
    try:
        _wait_ready(base_url, proc, stderr_path)
        _netlog("ready", kind="live_server", port=port, pid=proc.pid)
        yield base_url
    finally:
        stderr_file.close()
        _netlog_keep_server_log(stderr_path, port, proc.pid)
        _netlog("terminate", kind="live_server", port=port, pid=proc.pid)
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
    detached after each assertion so listeners don't accumulate across tests.

    ⚠ **`page.route` is NOT cleaned up for you, and this page is session-scoped.** A route
    installed by one test stays installed for every test that runs after it. No file in this
    directory called `unroute` until 2026-08-27, and that was survivable only by luck: every
    stub so far targeted an endpoint nobody else hits (`/api/input/ai/preview`,
    `/api/insight-types/*/run`, `/api/instruments/lookup`). The first stub of a UNIVERSAL
    endpoint — `/api/dashboard` — broke the very next test in its own file, and would have
    quietly fed a doctored payload to every test after it.

    So: if you stub an endpoint that more than one page requests, install and remove it
    around your own navigation (`test_kpi_trading_cost_layout.py` has the context-manager
    shape). A leaked route does not fail loudly; it makes some later test assert against
    data you invented."""
    _assert_loopback_window()  # session fixtures init lazily — self-heal (see helper)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        install_third_party_stub(page.context)  # issue #67 — no third-party network
        _netlog_watch(page, "browser_page")
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

    Best-effort and fully exception-swallowing: this runs in fixture TEARDOWN after the
    test already passed, so a reap race must NEVER surface as an ERROR. On Windows
    `proc.terminate()` (TerminateProcess) can raise OSError "Access is denied" when the
    subprocess is already exiting between the poll() check and the call — exactly the
    intermittent teardown ERROR (38 passed + 1 error) seen under full-suite load. The
    final taskkill tree-kill is belt-and-suspenders against any orphan."""
    try:
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
    except Exception:  # noqa: BLE001 (teardown reap — never fail an already-passed test)
        pass


@pytest.fixture
def flow_server(_e2e_loopback_socket: None) -> Iterator[FlowServerFactory]:
    """Factory: spawn an ISOLATED uvicorn subprocess against a fresh on-disk DB seeded
    by `seed`. Optional `users=[(username, password), ...]` makes the DB protected
    (spec-09) for the login flow. Returns the base URL. All spawns are torn down at the
    end of the test."""
    procs: list[subprocess.Popen[bytes]] = []
    handles: list[object] = []
    tmp_dirs: list[Path] = []
    logs: list[tuple[Path, int, int]] = []  # (stderr_path, port, pid) — diagnostic only

    def _make(
        seed: SeedFn,
        *,
        users: list[tuple[str, str]] | None = None,
    ) -> str:
        _assert_loopback_window()  # ordering-proof: a prior module may have re-banned
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

        env = {**os.environ, "DB_PATH": str(db_path), "PD_DISABLE_SCHEDULER": "1"}
        # Spawn-retry-on-early-exit: _free_port binds→releases→returns, so between release
        # and uvicorn's bind the ephemeral port can be taken (a TOCTOU race amplified by
        # spawning a fresh server PER flow test) — uvicorn then exits early on a bind
        # failure -> a rare fixture ERROR. Retry with a NEW port (hardening fixture SETUP
        # against a known infra race; NOT retrying a flaky assertion — spec-17 §17.7.4).
        last_exc: Exception | None = None
        for _attempt in range(3):
            port = _free_port()
            base_url = f"http://127.0.0.1:{port}"
            stderr_file = stderr_path.open("wb")
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn",
                    "portfolio_dash.api.app:create_app", "--factory",
                    "--host", "127.0.0.1", "--port", str(port),
                ],
                cwd=str(_WORKTREE_ROOT), env=env, stdout=stderr_file, stderr=stderr_file,
            )
            _netlog("spawn", kind="flow_server", port=port, pid=proc.pid, attempt=_attempt)
            try:
                _wait_ready(base_url, proc, stderr_path)
            except (RuntimeError, TimeoutError) as exc:
                last_exc = exc
                _netlog("spawn_failed", kind="flow_server", port=port, pid=proc.pid,
                        attempt=_attempt, error=repr(exc)[:400])
                _terminate(proc)
                stderr_file.close()
                continue
            _netlog("ready", kind="flow_server", port=port, pid=proc.pid)
            procs.append(proc)
            handles.append(stderr_file)
            logs.append((stderr_path, port, proc.pid))
            return base_url
        assert last_exc is not None
        raise last_exc

    try:
        yield _make
    finally:
        for proc in procs:
            _netlog("terminate", kind="flow_server", pid=proc.pid)
            _terminate(proc)
        for stderr_path_kept, port_kept, pid_kept in logs:
            _netlog_keep_server_log(stderr_path_kept, port_kept, pid_kept)
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
    install_third_party_stub(context)  # issue #67 — no third-party network
    page = context.new_page()
    # 60s (not Playwright's default 30s): the flow pages wait on a freshly-spawned isolated
    # uvicorn under full-suite load — give expect-polling a realistic ceiling so contention
    # never reddens a logically-correct flow (spec-17 §17.7.4 — ceiling, not sleep/retry).
    page.set_default_timeout(60_000)
    page.set_default_navigation_timeout(60_000)
    _netlog_watch(page, "fresh_page")
    try:
        yield page
    finally:
        # Best-effort teardown: a Playwright close raising here (e.g. the chromium target
        # already gone after a redirect-heavy flow) must NEVER fail an already-passed test
        # (matches flow_server's `except OSError: pass` cleanup philosophy). This is the
        # 賣超-era e2e teardown ERROR (38 passed + 1 error) — cleanup noise, not a defect.
        try:
            page.close()
        except Exception:  # noqa: BLE001 (teardown cleanup — swallow Playwright close errors)
            pass
        try:
            context.close()
        except Exception:  # noqa: BLE001 (teardown cleanup — swallow Playwright close errors)
            pass
