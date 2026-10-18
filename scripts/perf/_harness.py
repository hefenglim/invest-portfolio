"""Shared helpers for the PERF measurement harness (``scripts/perf/*``, plan W0.4).

Every script built on this module:

- takes ``--db PATH --now ISO --out DIR`` (``--keep-copy`` keeps the working copy);
- **refuses a ``--db`` or ``--out`` inside the repository** — this checkout and, when run from
  a git worktree, the main checkout it belongs to. The repository is public: a real ledger,
  or a result file computed from one, must never land in it;
- **copies the database first** (SQLite's online backup, read-only on the source, so a WAL is
  folded in) into a fresh temp folder and works only on that copy — the app's boot writes
  (seeds, migrations, a ``news.db`` beside it) all land in the temp folder;
- blocks every non-loopback connection and DNS lookup (no provider is ever reached), and sets
  ``PD_DISABLE_SCHEDULER=1``. Other ``PD_*`` flags pass through from the environment and are
  recorded in the output;
- treats ``psutil`` as optional: without it, RSS reads ``"unavailable"``;
- writes **JSON only, and only timings, counts and sizes** — never an amount, a symbol or an
  account id. A ``{symbol}`` route is labelled by a placeholder, a failure by its exception
  class, a probe day by its offset from the first trend day. SQL text is never kept (the
  trace callback sees statements with their bound values inlined); only statement counts and
  the table names they touch.

Run it from the checkout whose code you want to measure (``python scripts/perf/<script>.py``).
The dev venv's editable install maps ``portfolio_dash`` to the MAIN checkout, so this module
puts its own checkout first on ``sys.path`` and asserts the import landed there.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import inspect
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import CodeType, ModuleType, TracebackType
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_TZ = ZoneInfo("Asia/Taipei")

#: The heavy calculation functions whose calls are counted (label -> "module:qualname").
#: A target that does not exist (renamed by a later wave) is reported, never fatal.
DEFAULT_COUNT_TARGETS: dict[str, str] = {
    "build_dashboard": "portfolio_dash.portfolio.dashboard:_build_dashboard",
    "build_book": "portfolio_dash.portfolio.cost_basis:build_book",
    "daily_value_series": "portfolio_dash.portfolio.timeseries:daily_value_series",
    "daily_cash_series": "portfolio_dash.portfolio.networth:daily_cash_series",
    "get_price_history": "portfolio_dash.pricing.store:get_price_history",
    "get_fx_history": "portfolio_dash.pricing.store:get_fx_history",
    "iter_books": "portfolio_dash.portfolio.cost_basis:iter_books",
    "value_series": "portfolio_dash.portfolio.timeseries:value_series",
    "get_close_series": "portfolio_dash.pricing.store:get_close_series",
    "kpis_as_of": "portfolio_dash.portfolio.dashboard:kpis_as_of",
    "load_ledger_bundle": "portfolio_dash.data_ingestion.store:load_ledger_bundle",
    "xirr_reporting": "portfolio_dash.portfolio.returns:xirr_reporting",
    "load_cash_ledgers": "portfolio_dash.api.routers.cash:load_cash_ledgers",
    "replay_guard_read": "portfolio_dash.api.replay_guard:_read_ledgers",
    "twr_index": "portfolio_dash.portfolio.twr:twr_index",
}

#: Ledger / market tables whose ROW COUNTS describe a corpus's size (counts only).
SHAPE_TABLES = ("transactions", "dividends", "opening_inventory", "fx_conversions",
                "cash_movements", "corporate_actions", "instruments", "accounts", "prices",
                "fx_rates")


def log(message: str) -> None:
    """Progress on stderr (timings and labels only — never data)."""
    print(message, file=sys.stderr, flush=True)


# -- paths -----------------------------------------------------------------------------------
def repository_roots() -> list[Path]:
    """This checkout, plus the main checkout when this one is a git worktree."""
    roots = [REPO_ROOT]
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return roots
    if out:
        main = Path(out).resolve().parent
        if main not in roots:
            roots.append(main)
    return roots


def _inside(path: Path, root: Path) -> bool:
    child, parent = os.path.normcase(str(path)), os.path.normcase(str(root))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:          # different drives on Windows
        return False


def ensure_outside_repository(path: Path, what: str) -> Path:
    """*path*, resolved; exits with a message when it lies inside a repository checkout."""
    resolved = path.expanduser().resolve()
    for root in repository_roots():
        if _inside(resolved, root):
            raise SystemExit(
                f"refusing {what} {resolved}: it is inside the repository {root} "
                "(public; real data and results computed from it stay outside)")
    return resolved


def parse_now(text: str) -> datetime:
    """``--now``: an ISO datetime; a naive one is read in Asia/Taipei (the app clock)."""
    try:
        now = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"--now: not an ISO datetime: {text!r}") from exc
    return now if now.tzinfo is not None else now.replace(tzinfo=APP_TZ)


def common_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--db", required=True, type=Path,
                        help="the SQLite database to measure (copied first; never modified)")
    parser.add_argument("--now", required=True,
                        help="the clock the app sees, ISO (naive = Asia/Taipei)")
    parser.add_argument("--out", required=True, type=Path,
                        help="folder for the JSON results (outside the repository)")
    parser.add_argument("--keep-copy", action="store_true",
                        help="keep the temp working copy (its path is printed)")
    return parser


# -- the working copy ------------------------------------------------------------------------
def copy_database(src: Path, dst: Path) -> None:
    """A consistent copy of *src* at *dst* (online backup; the source is opened read-only)."""
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def connect(path: Path) -> sqlite3.Connection:
    """A connection shaped like ``shared.db.get_connection`` (Row factory, FKs on)."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass
class Workspace:
    """The temp folder, the working copy and the run's fixed inputs."""

    source: Path
    db: Path
    out: Path
    now: datetime
    tmp: Path
    keep: bool
    started: float = field(default_factory=time.perf_counter)

    def scratch_copy(self, name: str) -> Path:
        """A further copy of the working copy, for a probe that mutates it."""
        target = self.tmp / name
        copy_database(self.db, target)
        return target

    def close(self) -> None:
        if self.keep:
            log(f"working copy kept at {self.tmp}")
            return
        # The app's boot opened a log file under <copy>/logs; on Windows an open file cannot
        # be removed, and a log written from a real ledger must not outlive the run.
        _close_file_handlers_under(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.tmp.exists():
            log(f"could not remove the working copy at {self.tmp}; delete it by hand")


def _close_file_handlers_under(folder: Path) -> None:
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers += [lg for lg in logging.Logger.manager.loggerDict.values()
                if isinstance(lg, logging.Logger)]
    for logger in loggers:
        for handler in list(logger.handlers):
            name = getattr(handler, "baseFilename", None)
            if isinstance(name, str) and _inside(Path(name), folder):
                logger.removeHandler(handler)
                handler.close()


def open_workspace(args: argparse.Namespace) -> Workspace:
    """Validate the paths, copy the database, isolate the process. Call before any app import."""
    source = ensure_outside_repository(args.db, "--db")
    if not source.is_file():
        raise SystemExit(f"--db: no such file: {source}")
    out = ensure_outside_repository(args.out, "--out")
    out.mkdir(parents=True, exist_ok=True)
    now = parse_now(args.now)
    tmp = ensure_outside_repository(Path(tempfile.mkdtemp(prefix="pd-perf-")), "the temp folder")
    db = tmp / "portfolio.db"
    copy_database(source, db)
    block_network()
    configure_environment(db)
    return Workspace(source=source, db=db, out=out, now=now, tmp=tmp, keep=args.keep_copy)


def configure_environment(db: Path) -> None:
    """Point the app at the copy, switch the scheduler off, import THIS checkout's package."""
    os.environ["DB_PATH"] = str(db)
    os.environ["PD_DISABLE_SCHEDULER"] = "1"
    # LiteLLM would try to fetch its model price map at import; the block would refuse it
    # anyway, this just keeps the attempt (and its warning) out of the measurement.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    if str(REPO_ROOT) not in sys.path[:1]:
        sys.path.insert(0, str(REPO_ROOT))
    import portfolio_dash
    from portfolio_dash.shared.config import get_settings

    package = Path(portfolio_dash.__file__).resolve()
    if not _inside(package, REPO_ROOT):
        raise SystemExit(f"portfolio_dash imported from {package}, not from {REPO_ROOT}")
    get_settings.cache_clear()
    if get_settings().db_path.resolve() != db.resolve():
        raise SystemExit("the app does not see the working copy as its database")


# -- isolation -------------------------------------------------------------------------------
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


_network_blocked = False


def block_network() -> None:
    """Refuse every non-loopback ``connect`` and DNS lookup in this process (idempotent)."""
    global _network_blocked
    if _network_blocked:
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _host(address: Any) -> object:
        return address[0] if isinstance(address, tuple) else None

    def _connect(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host(address)):
            raise OSError("perf harness: network blocked")
        real_connect(self, address)

    def _connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host(address)):
            raise OSError("perf harness: network blocked")
        return real_connect_ex(self, address)

    def _getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_loopback(host):
            raise OSError("perf harness: DNS blocked")
        return real_getaddrinfo(host, *args, **kwargs)

    setattr(socket.socket, "connect", _connect)  # noqa: B010 - typeshed's signature differs
    setattr(socket.socket, "connect_ex", _connect_ex)  # noqa: B010
    setattr(socket, "getaddrinfo", _getaddrinfo)  # noqa: B010
    _network_blocked = True


# -- measurement -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Timing:
    wall_s: float
    cpu_s: float

    def as_dict(self) -> dict[str, float]:
        return {"wall_s": round(self.wall_s, 4), "cpu_s": round(self.cpu_s, 4)}


def timed[T](fn: Callable[[], T]) -> tuple[T, Timing]:
    """Run *fn*, measuring wall time and process CPU time (all threads)."""
    cpu0, wall0 = time.process_time(), time.perf_counter()
    value = fn()
    return value, Timing(time.perf_counter() - wall0, time.process_time() - cpu0)


def gzip_size(body: bytes) -> int:
    return len(gzip.compress(body, compresslevel=6))


def _resolve(dotted: str) -> Callable[..., Any] | None:
    module_name, _, qualname = dotted.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError):
        return None
    return obj if callable(obj) else None


def parse_count_targets(extra: list[str] | None) -> dict[str, str]:
    """The default targets plus ``--count label=module:qualname`` entries."""
    targets = dict(DEFAULT_COUNT_TARGETS)
    for item in extra or []:
        label, sep, dotted = item.partition("=")
        if not sep or ":" not in dotted:
            raise SystemExit(f"--count: expected label=module:qualname, got {item!r}")
        targets[label] = dotted
    return targets


class CallCounter:
    """How often each target function STARTS, in every thread (PEP 669, ``PY_START``).

    Uses a free ``sys.monitoring`` tool id other than the profiler's, so it coexists with
    ``cProfile`` (which, on 3.12, is itself a ``sys.monitoring`` tool).
    """

    def __init__(self, targets: Mapping[str, str]) -> None:
        self.counts: Counter[str] = Counter()
        self.missing: list[str] = []
        self._codes: dict[CodeType, str] = {}
        for label, dotted in targets.items():
            fn = _resolve(dotted)
            code = getattr(inspect.unwrap(fn), "__code__", None) if fn is not None else None
            if isinstance(code, CodeType):
                self._codes[code] = label
            else:
                self.missing.append(label)
        mon = sys.monitoring
        free = [i for i in (4, 3, 5) if mon.get_tool(i) is None]
        if not free:
            raise RuntimeError("no free sys.monitoring tool id for the call counter")
        self._tool = free[0]
        mon.use_tool_id(self._tool, "pd-perf-counter")
        mon.register_callback(self._tool, mon.events.PY_START, self._on_start)
        for code in self._codes:
            mon.set_local_events(self._tool, code, mon.events.PY_START)

    def _on_start(self, code: CodeType, offset: int) -> None:
        label = self._codes.get(code)
        if label is not None:
            self.counts[label] += 1

    def reset(self) -> None:
        self.counts.clear()

    def snapshot(self) -> dict[str, int]:
        return {k: v for k, v in sorted(self.counts.items()) if v}

    def close(self) -> None:
        mon = sys.monitoring
        for code in self._codes:
            mon.set_local_events(self._tool, code, 0)
        mon.register_callback(self._tool, mon.events.PY_START, None)
        mon.free_tool_id(self._tool)


_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)",
                       re.IGNORECASE)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")


class SqlCounter:
    """Counts the statements every connection opened through ``sqlite3.connect`` executes.

    Only a count and the table names survive — the statement text, which carries the bound
    values, is dropped at once. Counting is on only inside :meth:`window`.
    """

    def __init__(self) -> None:
        self.statements = 0
        self.tables: Counter[str] = Counter()
        self._active = False
        self._lock = threading.Lock()
        self._real_connect = sqlite3.connect

    def install(self) -> None:
        real = self._real_connect

        def _connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn: sqlite3.Connection = real(*args, **kwargs)
            conn.set_trace_callback(self._trace)
            return conn

        sqlite3.connect = _connect  # type: ignore[assignment]

    def uninstall(self) -> None:
        sqlite3.connect = self._real_connect

    def _trace(self, statement: str) -> None:
        if not self._active:
            return
        names = set(_TABLE_RE.findall(_STRING_RE.sub("''", statement)))
        with self._lock:
            self.statements += 1
            self.tables.update(n.lower() for n in names)

    @contextmanager
    def window(self) -> Iterator[SqlCounter]:
        with self._lock:
            self.statements = 0
            self.tables.clear()
        self._active = True
        try:
            yield self
        finally:
            self._active = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"statements": self.statements,
                    "tables": dict(self.tables.most_common())}


def _psutil() -> ModuleType | None:
    try:
        return importlib.import_module("psutil")
    except ImportError:
        return None


class RssPeak:
    """Peak RSS growth over a block, sampled every 10 ms; ``"unavailable"`` without psutil."""

    def __init__(self) -> None:
        self._ps = _psutil()
        self._proc: Any = self._ps.Process() if self._ps is not None else None
        self._base = 0
        self._peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> RssPeak:
        if self._proc is not None:
            self._base = self._peak = int(self._proc.memory_info().rss)
            self._stop.clear()
            self._thread = threading.Thread(target=self._sample, daemon=True)
            self._thread.start()
        return self

    def _sample(self) -> None:
        while not self._stop.wait(0.01):
            rss = int(self._proc.memory_info().rss)
            if rss > self._peak:
                self._peak = rss

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def result(self) -> float | str:
        if self._proc is None:
            return "unavailable"
        return round((self._peak - self._base) / 1e6, 1)


def rss_mb() -> float | str:
    """This process's current RSS in MB, or ``"unavailable"``."""
    ps = _psutil()
    if ps is None:
        return "unavailable"
    return round(int(ps.Process().memory_info().rss) / 1e6, 1)


# -- the simulated settled build -------------------------------------------------------------
class SettledSimulation:
    """Serve a dashboard build's history-dependent pieces from memory, as a settled store
    would, and leave everything else live (the 2026-10-18 feasibility measurement).

    Patched on ``portfolio_dash.portfolio.dashboard`` (names as of the wave-1 engine): the
    trend (``value_series`` — the ``iter_books`` pass runs inside it, lazily), the daily cash
    series and the benchmark comparison are memoised on the first (priming) build, and every
    close-series read is then answered from memory. Names a later wave renames are reported
    by :attr:`missing`, and the simulation is then unavailable.
    """

    MEMOISED = ("value_series", "daily_cash_series", "_benchmark_comparison")
    NEEDED = (*MEMOISED, "get_close_series")

    def __init__(self, dashboard: ModuleType) -> None:
        self._mod = dashboard
        self.missing = [n for n in self.NEEDED if not hasattr(dashboard, n)]
        self._real: dict[str, Any] = {}
        self._memo: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return not self.missing

    def __enter__(self) -> SettledSimulation:
        if not self.available:
            raise RuntimeError(f"settled simulation unavailable: missing {self.missing}")
        self._real = {n: getattr(self._mod, n) for n in self.NEEDED}
        self._memo.clear()
        for name in self.MEMOISED:
            setattr(self._mod, name, self._memoised(name))
        return self

    def _memoised(self, name: str) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if name not in self._memo:
                self._memo[name] = self._real[name](*args, **kwargs)
            return self._memo[name]
        return wrapper

    def prime(self, build: Callable[[], Any]) -> None:
        """Run one build to fill the memo (the settlement's one-time cost; not timed), then
        answer the close-series reads from memory from here on."""
        build()
        real_series = self._real["get_close_series"]
        seen: dict[tuple[Any, Any, Any], Any] = {}

        def series(conn: Any, symbol: Any, start: Any, end: Any) -> Any:
            key = (symbol, start, end)
            if key not in seen:
                seen[key] = real_series(conn, symbol, start, end)
            return list(seen[key])

        setattr(self._mod, "get_close_series", series)  # noqa: B010 - a module attribute

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        for name, fn in self._real.items():
            setattr(self._mod, name, fn)
        self._memo.clear()


def first_difference(a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    """The first top-level key whose JSON differs (a field NAME — never its value)."""
    for key in sorted(set(a) | set(b)):
        if json.dumps(a.get(key), sort_keys=True, default=str) != \
                json.dumps(b.get(key), sort_keys=True, default=str):
            return key
    return None


# -- output ----------------------------------------------------------------------------------
def ledger_shape(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts of the corpus's main tables (counts only)."""
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
            for t in SHAPE_TABLES if t in present}


def environment_info(ws: Workspace) -> dict[str, Any]:
    """Machine, interpreter and code facts for the result file (no data)."""
    import portfolio_dash

    try:
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return {
        "app_version": portfolio_dash.__version__,
        "git_head": sha or None,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "psutil": _psutil() is not None,
        "flags": {k: v for k, v in sorted(os.environ.items()) if k.startswith("PD_")},
        "now": ws.now.isoformat(),
        "db_bytes": ws.db.stat().st_size,
    }


def write_json(ws: Workspace, name: str, payload: Mapping[str, Any]) -> Path:
    """Write *payload* as ``<out>/<name>.json`` and say where."""
    target = ws.out / f"{name}.json"
    body = dict(payload)
    body.setdefault("elapsed_s", round(time.perf_counter() - ws.started, 2))
    target.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"wrote {target}")
    return target
