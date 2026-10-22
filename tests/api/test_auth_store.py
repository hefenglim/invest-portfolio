"""Unit tests for auth_store: scrypt hashing, guest-vs-protected, sessions, locking."""

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import auth_store as A

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_DAY = timedelta(days=1)


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


# --- session idle expiry (owner ruling 2026-10-22, review finding L5) -----------------------
#
# A session expires after SESSION_IDLE_EXPIRY of inactivity, measured from its recorded
# `last_seen_at`. A use is recorded at most once per SESSION_TOUCH_INTERVAL, after the
# response, on its own connection (`touch_session`); the gate's expiry check is a pure read.


def _session_at(c: sqlite3.Connection, seen: datetime) -> str:
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    return A.create_session(c, "chiaming", now=seen)


def _state(c: sqlite3.Connection, token: str, now: datetime) -> A.SessionState:
    state = A.session_state(c, token, now=now)
    assert state is not None
    return state


def test_the_expiry_and_touch_interval_are_the_owner_ruling() -> None:
    assert A.SESSION_IDLE_EXPIRY == timedelta(days=30)
    assert A.SESSION_TOUCH_INTERVAL == timedelta(hours=6)
    assert A.SESSION_EXPIRED_MESSAGE == "登入已逾期（30 天未使用），請重新登入"


def test_a_new_session_is_last_seen_when_it_is_created() -> None:
    c = _c()
    tok = _session_at(c, NOW)
    row = c.execute("SELECT last_seen_at FROM auth_sessions WHERE token = ?", (tok,)).fetchone()
    assert row["last_seen_at"] == NOW.isoformat()


@pytest.mark.parametrize(("idle", "expired"), [
    (timedelta(0), False),
    (29 * _DAY + timedelta(hours=23), False),
    (30 * _DAY, False),
    (30 * _DAY + timedelta(seconds=1), True),
    (400 * _DAY, True),
])
def test_the_expiry_boundary(idle: timedelta, expired: bool) -> None:
    c = _c()
    tok = _session_at(c, NOW)
    state = _state(c, tok, NOW + idle)
    assert state.username == "chiaming"
    assert state.expired is expired


@pytest.mark.parametrize(("idle", "due"), [
    (timedelta(0), False),
    (timedelta(hours=5, minutes=59, seconds=59), False),
    (timedelta(hours=6), True),
    (29 * _DAY, True),
    (30 * _DAY + timedelta(seconds=1), False),  # expired: refused, never renewed
])
def test_a_use_is_recorded_at_most_once_per_touch_interval(idle: timedelta, due: bool) -> None:
    c = _c()
    tok = _session_at(c, NOW)
    assert _state(c, tok, NOW + idle).touch_due is due


def test_a_touch_slides_the_expiry() -> None:
    c = _c()
    tok = _session_at(c, NOW)
    later = NOW + 20 * _DAY
    state = _state(c, tok, later)
    assert state.touch_due
    assert A.touch_session(c, tok, seen_raw=state.seen_raw, now=later) is True
    # 45 days after the login, 25 after the recorded use: still valid
    assert _state(c, tok, NOW + 45 * _DAY).expired is False
    assert _state(c, tok, later + 30 * _DAY + timedelta(seconds=1)).expired is True


def test_a_touch_is_a_compare_and_set_on_the_stamp_it_read() -> None:
    """Two requests that both found the touch due record one use: the second one's guard
    (the stamp IT read) no longer matches."""
    c = _c()
    tok = _session_at(c, NOW)
    first = _state(c, tok, NOW + _DAY)
    assert A.touch_session(c, tok, seen_raw=first.seen_raw, now=NOW + _DAY) is True
    assert A.touch_session(c, tok, seen_raw=first.seen_raw,
                           now=NOW + _DAY + timedelta(seconds=1)) is False
    row = c.execute("SELECT last_seen_at FROM auth_sessions WHERE token = ?", (tok,)).fetchone()
    assert row["last_seen_at"] == (NOW + _DAY).isoformat()


def test_a_locked_session_is_never_touched() -> None:
    c = _c()
    tok = _session_at(c, NOW)
    seen = _state(c, tok, NOW + _DAY)
    A.lock_session(c, tok)
    assert A.touch_session(c, tok, seen_raw=seen.seen_raw, now=NOW + _DAY) is False
    assert _state(c, tok, NOW + _DAY).touch_due is False


def test_an_unreadable_stamp_counts_as_expired() -> None:
    c = _c()
    tok = _session_at(c, NOW)
    c.execute("UPDATE auth_sessions SET last_seen_at = 'not a time' WHERE token = ?", (tok,))
    state = _state(c, tok, NOW)
    assert state.expired and not state.touch_due


def test_a_naive_stamp_reads_as_application_local_time() -> None:
    c = _c()
    tok = _session_at(c, NOW)
    naive = (NOW - 30 * _DAY).replace(tzinfo=None).isoformat()
    c.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token = ?", (naive, tok))
    assert _state(c, tok, NOW).expired is False
    assert _state(c, tok, NOW + timedelta(seconds=1)).expired is True


def test_the_boot_step_purges_expired_locked_and_orphaned_sessions() -> None:
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    live = A.create_session(c, "chiaming", now=NOW - 29 * _DAY)
    stale = A.create_session(c, "chiaming", now=NOW - 31 * _DAY)
    locked = A.create_session(c, "chiaming", now=NOW - _DAY)
    A.lock_session(c, locked)
    c.execute("INSERT INTO auth_sessions (token, username, created_at, last_seen_at, locked) "
              "VALUES ('gone', 'ghost', ?, ?, 0)", (NOW.isoformat(), NOW.isoformat()))
    c.commit()
    outcome = A.prepare_sessions_at_boot(c, now=NOW)
    assert (outcome.expired, outcome.locked, outcome.orphaned, outcome.dated) == (1, 1, 1, 0)
    assert outcome.purged == 3
    left = [r["token"] for r in c.execute("SELECT token FROM auth_sessions")]
    assert left == [live] and stale not in left
    again = A.prepare_sessions_at_boot(c, now=NOW)  # idempotent: a second boot purges nothing
    assert again.purged == 0 and again.dated == 0


def test_a_session_from_before_the_upgrade_counts_from_the_upgrade() -> None:
    """The old table had no `last_seen_at`. The boot adds it and dates every existing session
    from the migration time, so nobody is logged out on deploy day: a 90-day-old login that
    was used yesterday stays valid (its last use was never recorded, so it cannot be told
    apart from an idle one), and it expires 30 days after the upgrade unless it is used."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(  # the schema as it stood through v0.1.32
        "CREATE TABLE auth_users (username TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " password_hash TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE auth_sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0);"
    )
    c.execute("INSERT INTO auth_users VALUES ('chiaming', '家明', 'x', ?)", (NOW.isoformat(),))
    c.execute("INSERT INTO auth_sessions VALUES ('old', 'chiaming', ?, 0)",
              ((NOW - 90 * _DAY).isoformat(),))
    c.commit()
    A.create_auth_tables(c)  # the additive migration: no table rebuild, the row survives
    cols = [r["name"] for r in c.execute("PRAGMA table_info(auth_sessions)")]
    assert cols == ["token", "username", "created_at", "locked", "last_seen_at"]
    outcome = A.prepare_sessions_at_boot(c, now=NOW)
    assert outcome.dated == 1 and outcome.purged == 0
    assert _state(c, "old", NOW + 30 * _DAY).expired is False
    assert _state(c, "old", NOW + 30 * _DAY + timedelta(seconds=1)).expired is True
    A.create_auth_tables(c)  # a second boot changes nothing
    assert [r["name"] for r in c.execute("PRAGMA table_info(auth_sessions)")] == cols


def test_a_row_written_without_a_stamp_reads_its_creation_time() -> None:
    """An older build re-pinned after this one (a rollback) inserts sessions without the
    column: until the next boot dates them, the gate reads their creation time."""
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    c.execute("INSERT INTO auth_sessions (token, username, created_at, locked) "
              "VALUES ('rb', 'chiaming', ?, 0)", (NOW.isoformat(),))
    state = _state(c, "rb", NOW + 30 * _DAY)
    assert not state.expired and state.seen_raw is None and state.touch_due
    assert A.touch_session(c, "rb", seen_raw=None, now=NOW + 30 * _DAY) is True
    assert _state(c, "rb", NOW + 31 * _DAY).expired is False
