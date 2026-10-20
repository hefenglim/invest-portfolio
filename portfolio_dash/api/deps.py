"""Per-request dependencies: SQLite connection, injectable clock, reporting currency."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request

from portfolio_dash.api.read_only import is_read_only
from portfolio_dash.portfolio.settlement_models import ACTOR_GUEST, ACTOR_USER_PREFIX
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import install_actor, session
from portfolio_dash.shared.enums import Currency

APP_TZ = ZoneInfo("Asia/Taipei")


def request_actor(conn: sqlite3.Connection, request: Request) -> str | None:
    """Who a mutating request writes as (plan P14): ``user:<login>`` from a valid session
    cookie, ``user:guest`` in guest mode (no ``auth_users`` row), ``None`` for a protected
    request without a valid session (the gate refuses it, or it is the login itself, which
    writes no ledger row)."""
    # Local import: ``auth_store`` imports this module for ``get_conn``.
    from portfolio_dash.api.auth_store import is_protected, session_user

    if not is_protected(conn):
        return ACTOR_GUEST
    token = request.cookies.get("pd_session")
    if token is None:
        return None
    login = session_user(conn, token)
    return None if login is None else f"{ACTOR_USER_PREFIX}{login}"


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """A fresh per-request connection (never share one across threads).

    Every route declares it as ``Depends(get_conn, scope="function")`` (PERF-S10,
    2026-10-18): the session then commits when the path function returns, BEFORE the response
    starts, so a client that received the answer reads its own write on the next request, and
    a failed commit answers 500 instead of a silent 200. The gate
    (``auth_store.require_session``) uses the same scope, so the request has ONE cached
    connection. ``tests/contract/test_s10_conn_scope.py`` scans every site.

    A MUTATING request's connection carries the settlement store's change actor (spec
    2026-10-18 §7, :func:`request_actor`). A GET or a declared read-only POST
    (``api/read_only.py``) gets none — no extra read and no TEMP trigger (DEF-065).
    """
    with session() as conn:
        if not is_read_only(request.method, request.url.path):
            actor = request_actor(conn, request)
            if actor is not None:
                install_actor(conn, actor)
        yield conn


def get_now() -> datetime:
    """Current time in the application timezone (overridden in tests via freezegun).

    Delegates to :func:`shared.clock.app_now` — the SINGLE day-anchor clock shared
    with the scheduler's cron path (decision Q6, 2026-07-07).
    """
    return app_now()


def get_reporting() -> Currency:
    return get_settings().reporting_currency
