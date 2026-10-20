"""Child process of ``test_lazy_imports.py``: boot the app, call every route, report imports.

Run as ``python -m tests.shared.lazy_import_walk <work dir>`` with ``cwd`` and ``PYTHONPATH`` =
the checkout under test (never ``-I``). A fresh interpreter is the only place the question
"does the app import LiteLLM / yfinance / pandas?" has an answer: four test modules import
them at top level, so the pytest process always holds them.

Every attempt to import one of :data:`HEAVY` is recorded with the phase that made it (the
import of ``api.app``, the boot, or the route being called) and then REFUSED, so a later
phase cannot hide behind an earlier one's import. The providers are stubbed at the fetch seam
(no network, no yfinance needed); the network itself is closed to everything but loopback.
The report is one JSON object on the last line of stdout.
"""

from __future__ import annotations

import json
import re
import socket
import sys
from collections import defaultdict
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

HEAVY: tuple[str, ...] = ("litellm", "yfinance", "pandas")
_attempts: dict[str, list[str]] = defaultdict(list)
_phase = ["import"]


class _HeavyImportGuard(MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None,
                  target: ModuleType | None = None) -> ModuleSpec | None:
        if fullname.split(".")[0] in HEAVY:
            _attempts[_phase[0]].append(fullname)
            raise ImportError(f"lazy-import walk: {fullname} refused during {_phase[0]}")
        return None


_LOCAL = ("127.0.0.1", "::1", "localhost", "testserver")


def _close_the_network() -> None:
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOCAL:
            raise OSError(f"lazy-import walk: no network ({host})")
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in _LOCAL and host is not None:
            raise socket.gaierror(f"lazy-import walk: no network ({host})")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign, assignment]
    socket.getaddrinfo = getaddrinfo


def _stub_providers() -> None:
    """Every yfinance fetch answers "nothing" without importing yfinance."""
    from portfolio_dash.api import news_service
    from portfolio_dash.pricing import (
        consensus_source,
        fundamentals_source,
        index_source,
        names,
        sentiment_source,
    )
    from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider

    def nothing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    def none(*_a: Any, **_k: Any) -> None:
        return None

    for method in ("fetch_quote_latest", "fetch_quote_history", "fetch_fx",
                   "fetch_fx_history", "fetch_dividends"):
        setattr(YFinanceProvider, method, nothing)
    sentiment_source._vix_last_close = none
    index_source._index_last_close = none
    consensus_source._fetch_price_targets = none
    consensus_source._fetch_recommendations = none
    fundamentals_source._fetch_yf_fast_info = none
    fundamentals_source._fetch_yf_quarterly_income = lambda *_a, **_k: {}
    fundamentals_source._fetch_yf_balance_sheet = lambda *_a, **_k: {}
    fundamentals_source._fetch_yf_dividends = nothing
    names._yf_name = none
    news_service._yf_client = lambda: nothing


_PARAMS = {"symbol": "2330", "account": "tw_broker", "job_id": "alert_scan"}
_QUERY = {"/api/cash/statement": {"account": "tw_broker"},
          "/api/input/option-symbol": {"text": "XYZ 12/18/2026 50.00 P"}}


def _concrete(template: str) -> str:
    path = template
    for name in re.findall(r"{(\w+)(?::\w+)?}", template):
        path = re.sub(r"{" + name + r"(?::\w+)?}", _PARAMS.get(name, "1"), path)
    return path


def main(work: Path) -> dict[str, Any]:
    sys.meta_path.insert(0, _HeavyImportGuard())
    _close_the_network()
    import os

    os.environ.update({
        "DB_PATH": str(work / "portfolio.db"), "PD_DISABLE_SCHEDULER": "1",
        "PD_SETTLE": "on", "PD_SETTLE_MODE": "inline", "PD_JOB_MODE": "inline",
        "PD_PREWARM": "0",
    })
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    import portfolio_dash
    from portfolio_dash.api.app import create_app

    _phase[0] = "boot"
    _stub_providers()
    from tests.conftest import _seed_golden

    app = create_app()
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api"):
            routes.extend((method, route.path) for method in sorted(route.methods))
    # Reads first, then writes: a write with an empty body may still delete row 1.
    routes.sort(key=lambda r: (r[0] != "GET", r[1], r[0]))
    status: dict[str, int] = {}
    with TestClient(app, raise_server_exceptions=False) as client:
        import sqlite3

        seed = sqlite3.connect(work / "portfolio.db")
        seed.row_factory = sqlite3.Row
        _seed_golden(seed)
        seed.commit()
        seed.close()
        for method, template in routes:
            label = f"{method} {template}"
            _phase[0] = label
            path = _concrete(template)
            if method == "GET":
                r = client.get(path, params=_QUERY.get(template, {}))
            else:
                r = client.request(method, path, json={})
            status[label] = r.status_code
    # The guard's own proof: an import attempted now must be recorded (and refused).
    _phase[0] = "self-test"
    try:
        import pandas  # noqa: F401
    except ImportError:
        pass
    _phase[0] = "end"
    return {
        "package": str(Path(portfolio_dash.__file__).resolve()),
        "attempts": dict(_attempts),
        "loaded": sorted(m for m in sys.modules if m.split(".")[0] in HEAVY),
        "routes": len(routes),
        "server_errors": sorted(k for k, v in status.items() if v >= 500),
    }


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), ensure_ascii=False))
