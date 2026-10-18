"""Per-request dependencies: SQLite connection, injectable clock, reporting currency."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request

from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency

APP_TZ = ZoneInfo("Asia/Taipei")


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """A fresh per-request connection (never share one across threads).

    Every route declares it as ``Depends(get_conn, scope="function")`` (PERF-S10,
    2026-10-18): the session then commits when the path function returns, BEFORE the response
    starts, so a client that received the answer reads its own write on the next request, and
    a failed commit answers 500 instead of a silent 200. The gate
    (``auth_store.require_session``) uses the same scope, so the request has ONE cached
    connection. ``tests/contract/test_s10_conn_scope.py`` scans every site. *request* is for
    the settlement store's change actor (spec §7).
    """
    del request  # bound by the settlement store's actor (W1-B)
    with session() as conn:
        yield conn


def get_now() -> datetime:
    """Current time in the application timezone (overridden in tests via freezegun).

    Delegates to :func:`shared.clock.app_now` — the SINGLE day-anchor clock shared
    with the scheduler's cron path (decision Q6, 2026-07-07).
    """
    return app_now()


def get_reporting() -> Currency:
    return get_settings().reporting_currency
