"""Unit tests for auth_store: scrypt hashing, guest-vs-protected, sessions, locking."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.api import auth_store as A

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _c() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    A.create_auth_tables(c)
    return c


def test_hash_roundtrip_and_format() -> None:
    h = A.hash_password("password123")
    assert h.startswith("scrypt$") and A.verify_password("password123", h)
    assert not A.verify_password("wrong", h)
    assert not A.verify_password("x", "garbage")


def test_guest_until_first_user() -> None:
    c = _c()
    assert A.is_protected(c) is False
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    assert A.is_protected(c) is True


def test_authenticate_same_path_for_missing_and_bad() -> None:
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    assert A.authenticate(c, "chiaming", "password123") is True
    assert A.authenticate(c, "chiaming", "nope") is False
    assert A.authenticate(c, "ghost", "whatever") is False


def test_session_lifecycle_and_lock() -> None:
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    tok = A.create_session(c, "chiaming", now=NOW)
    assert A.session_user(c, tok) == "chiaming"
    assert A.lock_session(c, tok) is True
    assert A.session_user(c, tok) is None  # locked -> invalid
    tok2 = A.create_session(c, "chiaming", now=NOW)  # re-login clears locked sessions
    assert A.session_user(c, tok2) == "chiaming"
    A.delete_session(c, tok2)
    assert A.session_user(c, tok2) is None


def test_list_users_never_leaks_hash() -> None:
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    rows = A.list_users(c)
    assert rows[0]["username"] == "chiaming" and "password_hash" not in rows[0]


def test_delete_user_removes_sessions() -> None:
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    tok = A.create_session(c, "chiaming", now=NOW)
    A.delete_user(c, "chiaming")
    assert A.user_exists(c, "chiaming") is False and A.session_user(c, tok) is None
