"""Per-request dependencies: SQLite connection, injectable clock, reporting currency."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency

APP_TZ = ZoneInfo("Asia/Taipei")


def get_conn() -> Iterator[sqlite3.Connection]:
    """A fresh per-request connection (never share one across threads)."""
    with session() as conn:
        yield conn


def get_now() -> datetime:
    """Current time in the application timezone (overridden in tests via freezegun)."""
    return datetime.now(APP_TZ)


def get_reporting() -> Currency:
    return get_settings().reporting_currency
