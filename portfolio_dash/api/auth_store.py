"""Auth store (spec 09): table DDL, scrypt hashing, user/session CRUD, mode check.

Self-contained access-control store for the api layer. Guest mode = ``auth_users``
empty (everything open); protected mode = >=1 user (a valid ``pd_session`` cookie is
required for all ``/api/*`` except login/session/logout/health). Stdlib only — no third-party
dependency. Stores **only** salted scrypt hashes; ``password_hash`` is never returned
or logged. Imports only ``shared`` + ``api.deps`` + stdlib (never portfolio/forex/
pricing) — auth is access control, not business calculation. The one exception is local and
lazy: the pre-warm after a recorded touch names ``api.alert_inputs.prewarm_today``, imported
only when the pre-warm is on, exactly as ``api/settlement_service.py`` does.

**Session idle expiry** (owner ruling 2026-10-22, review finding L5; sessions never expired
before). A session expires after :data:`SESSION_IDLE_EXPIRY` without use, and each use slides
the window. Its last use is ``auth_sessions.last_seen_at``. Because a GET never writes
(DEF-065), the two halves are kept apart:

* **the check is a pure read** in the gate (:func:`require_session`, :func:`session_state`):
  an expired session answers 401 with :data:`SESSION_EXPIRED_MESSAGE` and is treated as gone
  — never deleted by the request that found it;
* **a use is recorded by the touch** (:func:`run_touch`), which the gate never runs itself.
  When the stored stamp is :data:`SESSION_TOUCH_INTERVAL` old or older, the gate ARMS the
  session's pending touch (:func:`schedule_touch`): a daemon timer that fires once the page
  load has settled, :data:`SESSION_TOUCH_SETTLE_S` after the session's LATEST gated request
  (each one pushes it back), and never later than :data:`SESSION_TOUCH_MAX_DEFER_S` after the
  request that armed it. The touch runs on its OWN short-lived connection with a
  :data:`SESSION_TOUCH_BUSY_TIMEOUT_MS` busy timeout, as one autocommit compare-and-set
  UPDATE (:data:`TOUCH_SQL`). It is skipped when the write lock is held (the next request
  arms it again), and it never raises. The request's own connection still writes nothing and
  takes no write lock: ``tests/contract/test_def065_get_never_writes.py`` (section 5) holds
  the touch as the one reviewed exception, by its statement and by its connection. One touch
  per session is pending at a time, one runs at a time in this process, and none runs again
  within the interval once one recorded (the in-process guard, :func:`touch_wanted`). The
  lifespan's stop cancels the pending ones (:func:`cancel_pending_touches`).

**The cache cost** (owner rulings 2026-10-22). A recorded touch commits from its own
connection, which moves ``PRAGMA data_version`` and empties the result cache. Hence the 24 h
interval (at most one invalidation a day per session), and the order 「續期延後，記完立刻預熱」
— defer the touch, pre-warm the moment it is recorded. Until the touch commits, every read of
the page load and a navigation inside the settle window are served from the previous cache
entry: nothing has changed. The pre-warm then starts AT ONCE: no read of that page load is
left to compete with its build on the host's one vCPU, and a navigation that lands while it
runs joins it in flight (``shared/flight.py``; the pre-warm and ``GET /api/dashboard`` build
under one cache key) instead of building the dashboard a second time. Measured before (a 3 s
pre-warm after a touch committed right after a page load's FIRST response): a navigation
inside those 3 s rebuilt the dashboard itself, about 0.9 s on the VM, once per session a day.
The price of 24 h: a session lapses 29 to 30 days after its last use (that use may have been
recorded up to a day earlier); the notice still says 30 天.

A stamp further than :data:`SESSION_CLOCK_SKEW` in the future, and one that cannot be read
at all (a BLOB, a number, text that is no time), are expired: they fail closed. The boot
(:func:`prepare_sessions_at_boot`) dates the sessions that have no stamp from the boot —
every session created before this change, and any created by an older build re-pinned after
it — and purges the expired, the locked (never revived: re-login issues a new token) and the
orphaned ones. Logout is an open path, so an expired cookie can be cleared.
"""

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol

from fastapi import Depends, HTTPException, Request

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.shared import config_store, flags, result_cache
from portfolio_dash.shared.db import get_connection

logger = logging.getLogger(__name__)

# --- session idle expiry: the owner's ruling, in ONE place ----------------------------------

#: A session unused for longer than this is expired (owner ruling 2026-10-22).
SESSION_IDLE_EXPIRY = timedelta(days=30)
#: A use is recorded at most once per this interval per session (the touch's throttle).
#: 24 h (owner ruling 2026-10-22, the cache cost): every recorded touch empties the result
#: cache, so at most once a day per session. The price: a session lapses 29 to 30 days after
#: its last use, since that use may have been recorded up to a day earlier.
SESSION_TOUCH_INTERVAL = timedelta(hours=24)
#: A stamp further than this in the FUTURE is expired (it fails closed): a few minutes of
#: clock difference are tolerated, a stamp from a clock set ahead or a hand edit is not.
SESSION_CLOCK_SKEW = timedelta(minutes=5)
#: The touch's own connection waits at most this long for the write lock, then skips.
SESSION_TOUCH_BUSY_TIMEOUT_MS = 100
#: A touch that failed for any reason other than a busy lock is retried after this long —
#: logged (WARNING) at most this often per session, rather than on every request.
SESSION_TOUCH_FAILURE_BACKOFF = timedelta(minutes=10)
#: The touch waits for the page load to settle (owner ruling 2026-10-22, 「續期延後」): it
#: fires this long after the LATEST gated request of its session, each further request
#: pushing it back. As long as the write window waits before its pre-warm
#: (``api/app.py::WRITE_PREWARM_DELAY_S``; a test holds the two equal): both mean "the page
#: load is over", and from the cache's view a touch IS a write.
SESSION_TOUCH_SETTLE_S = 3.0
#: ... but never later than this after the request that armed it, so a session that never
#: falls quiet for :data:`SESSION_TOUCH_SETTLE_S` (a page left polling) is still touched.
SESSION_TOUCH_MAX_DEFER_S = 30.0
#: The pre-warm after a recorded touch starts at once (「記完立刻預熱」): the touch itself
#: waited for the page load to settle, so nothing of that load is left to compete with it.
SESSION_TOUCH_PREWARM_DELAY_S = 0.0
#: How long the lifespan's stop waits for a touch already RUNNING (one UPDATE, giving up on a
#: held lock after :data:`SESSION_TOUCH_BUSY_TIMEOUT_MS`): a commit after the stop's WAL
#: checkpoint would leave ``-wal`` / ``-shm`` beside the database.
SHUTDOWN_TOUCH_WAIT_S = 2.0
#: The ``request.state`` attribute the gate sets to the login it let through (and the login /
#: logout routes to the login they authenticated / ended); the action log names it. A request
#: refused — no session, unknown, locked or expired — carries none.
LOGIN_STATE_ATTR = "pd_login"
#: The gate's 401 for an expired session, and the login page's notice.
SESSION_EXPIRED_MESSAGE = f"登入已逾期（{SESSION_IDLE_EXPIRY.days} 天未使用），請重新登入"
#: The login page's notice for a cookie the store no longer knows (purged at boot, user
#: removed): the form is back, and the visitor is told why.
SESSION_INVALID_MESSAGE = "先前的登入已失效，請重新登入"
#: The touch: a compare-and-set on the stamp the gate read, so two requests that both found
#: it due record one use, and a session locked or removed in between is left alone.
TOUCH_SQL = ("UPDATE auth_sessions SET last_seen_at = ? "
             "WHERE token = ? AND locked = 0 AND last_seen_at IS ?")

# scrypt parameters — named constants so verify reuses exactly what hash used.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

# Paths exempt from the session gate (login + the gate-query endpoint itself +
# the liveness probe). ``/api/health`` returns only ``{"status": "ok"}`` (no data),
# so it is unauthenticated by design: a liveness probe must answer regardless of
# login state. Every OTHER ``/api/*`` path still requires a session in protected mode.
# ``/api/auth/logout`` (2026-10-22): an EXPIRED cookie must be clearable — behind the gate
# its logout answered 401 and the cookie stayed. Logout only deletes the session the
# request's own cookie names, so it needs nothing but possession of that token, and the
# cookie's SameSite=Lax keeps a cross-site POST from carrying it. Lock stays gated: an
# expired session has nothing left to lock.
_OPEN_PATHS = {"/api/auth/login", "/api/auth/session", "/api/auth/logout", "/api/health"}


def create_auth_tables(conn: sqlite3.Connection) -> None:
    """Create the auth tables if absent (idempotent; safe on every startup).

    ``auth_sessions.last_seen_at`` (session idle expiry, 2026-10-22) is added to an existing
    table by an additive ``ALTER TABLE … ADD COLUMN`` — no rebuild, every row kept. It runs in
    the boot session, before the settlement store records the schema version (``api/app.py``);
    ``auth_sessions`` is not a settlement trigger table, so no re-settle follows. A build that
    predates the column ignores it (its INSERT names its columns), so re-pinning one is safe.
    """
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS auth_users ("
        " username TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " password_hash TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS auth_sessions ("
        " token TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0,"
        " last_seen_at TEXT);"
    )
    columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_sessions)")}
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN last_seen_at TEXT")
    conn.commit()


def ensure_auth_seeded(conn: sqlite3.Connection) -> None:
    """Integrate with the settings_meta seed framework; seed is a no-op (guest)."""
    config_store.ensure_seeded(conn, "auth", create=create_auth_tables, seed=lambda c: None)


# --- crypto -----------------------------------------------------------------


def hash_password(pw: str) -> str:
    """Salted scrypt hash, stored as ``scrypt$<salt_hex>$<hash_hex>``."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """Constant-time verify against a stored ``scrypt$salt$hash`` string."""
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(
        pw.encode(), salt=bytes.fromhex(salt_hex),
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN,
    )
    return hmac.compare_digest(dk.hex(), hash_hex)


# --- mode + user CRUD -------------------------------------------------------


def is_protected(conn: sqlite3.Connection) -> bool:
    """Protected mode iff there is >=1 authorized user.

    Treats a missing ``auth_users`` table as guest mode (defensive): the gate runs
    on every ``/api/*`` request, and if it ever fired before the lifespan created the
    table the app would 500 instead of degrading. ``_lifespan`` / the test fixtures
    always create it, so this is belt-and-suspenders, not a live path.
    """
    try:
        row = conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def list_users(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Authorized users (never includes ``password_hash``), oldest first."""
    rows = conn.execute(
        "SELECT username, name, created_at FROM auth_users ORDER BY created_at, username"
    ).fetchall()
    return [
        {"username": r["username"], "name": r["name"], "created_at": r["created_at"]}
        for r in rows
    ]


def get_user(conn: sqlite3.Connection, username: str) -> dict[str, str] | None:
    """Single user (name lookup); never includes the hash. None if absent."""
    row = conn.execute(
        "SELECT username, name, created_at FROM auth_users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return None
    return {"username": row["username"], "name": row["name"], "created_at": row["created_at"]}


def user_exists(conn: sqlite3.Connection, username: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM auth_users WHERE username = ?", (username,)
    ).fetchone() is not None


def create_user(
    conn: sqlite3.Connection, *, name: str, username: str, password: str, now: datetime
) -> None:
    """Insert a user with a salted scrypt hash. Caller checks dup/short first."""
    conn.execute(
        "INSERT INTO auth_users (username, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (username, name, hash_password(password), now.isoformat()),
    )
    conn.commit()


def delete_user(conn: sqlite3.Connection, username: str) -> None:
    """Delete a user and all of that user's sessions (idempotent)."""
    conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))
    conn.execute("DELETE FROM auth_users WHERE username = ?", (username,))
    conn.commit()


# Precomputed once at import so a missing-user authenticate pays the SAME scrypt cost
# as a real verify — closes the username-enumeration timing side-channel. The password
# is random, so a verify against it always fails; only its timing is used.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """True iff the user exists and the password verifies.

    Missing-user and bad-password are indistinguishable in status, body, AND timing:
    on a missing user we run a dummy scrypt verify (result discarded) so the response
    time does not reveal whether the username exists (spec 9.1 不可洩漏帳號是否存在).
    """
    row = conn.execute(
        "SELECT password_hash FROM auth_users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)  # equalize timing; result intentionally discarded
        return False
    return verify_password(password, row["password_hash"])


# --- session CRUD -----------------------------------------------------------


def create_session(conn: sqlite3.Connection, username: str, *, now: datetime) -> str:
    """Create a fresh session token, last seen *now*; clears that user's locked sessions
    (re-login unlock per spec 9.2)."""
    conn.execute("DELETE FROM auth_sessions WHERE username = ? AND locked = 1", (username,))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions (token, username, created_at, locked, last_seen_at)"
        " VALUES (?, ?, ?, 0, ?)",
        (token, username, now.isoformat(), now.isoformat()),
    )
    conn.commit()
    return token


@dataclass(frozen=True)
class SessionState:
    """What the store knows of one token at *now* — read only, never written by a reader."""

    username: str
    locked: bool
    #: the stored ``last_seen_at`` as read (``None`` = never recorded): the touch's guard
    seen_raw: str | None
    #: unused for longer than :data:`SESSION_IDLE_EXPIRY` (or an unreadable stamp)
    expired: bool
    #: valid, unlocked, and its last recorded use is :data:`SESSION_TOUCH_INTERVAL` old
    touch_due: bool


def _idle_for(stamp: object, now: datetime) -> timedelta | None:
    """How long ago *stamp* was at *now*; ``None`` when it cannot be read.

    A stamp without an offset (an older writer, a hand edit) reads as application-local
    time, the zone every stamp this module writes carries. Anything that is not an ISO text
    — a BLOB or a number from a hand edit (``fromisoformat`` raises TypeError on those), or
    text that is not a time — cannot be read: the gate refuses it, and the boot purges it
    instead of failing to start.
    """
    if stamp is None:
        return None
    try:
        seen = datetime.fromisoformat(stamp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None and now.tzinfo is not None:
        seen = seen.replace(tzinfo=now.tzinfo)
    elif seen.tzinfo is not None and now.tzinfo is None:
        seen = seen.replace(tzinfo=None)
    return now - seen


def _is_expired(idle: timedelta | None) -> bool:
    """THE expiry rule (the gate and the boot purge both ask it): the stamp cannot be read,
    it is older than the ruling allows, or it lies further in the future than the clock-skew
    tolerance — a future stamp would otherwise keep a session alive for as long as it is
    ahead, so it fails closed."""
    return idle is None or idle > SESSION_IDLE_EXPIRY or idle < -SESSION_CLOCK_SKEW


def session_state(conn: sqlite3.Connection, token: str, *, now: datetime) -> SessionState | None:
    """The token's state at *now*, or ``None`` when the store does not know it.

    A row with no ``last_seen_at`` (written by a build that predates the column, until the
    next boot dates it) reads its ``created_at``.
    """
    row = conn.execute(
        "SELECT username, locked, created_at, last_seen_at FROM auth_sessions WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    seen_raw: str | None = row["last_seen_at"]
    idle = _idle_for(seen_raw if seen_raw is not None else row["created_at"], now)
    expired = _is_expired(idle)
    locked = bool(row["locked"])
    return SessionState(
        username=row["username"], locked=locked, seen_raw=seen_raw, expired=expired,
        touch_due=(not expired and not locked and idle is not None
                   and idle >= SESSION_TOUCH_INTERVAL),
    )


def touch_session(
    conn: sqlite3.Connection, token: str, *, seen_raw: str | None, now: datetime
) -> bool:
    """Record a use of *token* at *now*, guarded by the stamp the caller read; ``True`` when
    this call recorded it. The caller owns the connection (see :func:`run_touch`).
    """
    cur = conn.execute(TOUCH_SQL, (now.isoformat(), token, seen_raw))
    if conn.in_transaction:
        conn.commit()
    return cur.rowcount > 0


#: What one touch did: ``recorded`` (the row changed), ``lost`` (the stamp it read had
#: already moved, or the session was locked or removed: nothing to do), ``busy`` (the write
#: lock was held: skipped, the next request arms it again), ``failed`` (any other error:
#: logged at WARNING, retried after :data:`SESSION_TOUCH_FAILURE_BACKOFF`), ``throttled``
#: (this process has the session's touch in flight, or recorded it within the interval:
#: nothing ran).
TouchOutcome = Literal["recorded", "lost", "busy", "failed", "throttled"]

# The in-process guard (owner ruling 2026-10-22, item 5): the requests of one page load all
# find the touch due at once, and each would run the UPDATE (12 GETs → 12 UPDATEs, one row
# changed). Per token: the touch in flight, and the earliest `now` at which another may run.
# The pending touches (below) live under the same lock, and the stop waits on its condition.
_touch_lock = threading.Lock()
_touch_done = threading.Condition(_touch_lock)
_touch_in_flight: set[str] = set()
_touch_not_before: dict[str, datetime] = {}
#: The guard forgets everything past this many sessions (only the throttling is lost).
_TOUCH_GUARD_MAX = 256


def _guard_allows(token: str, now: datetime) -> bool:
    """Under ``_touch_lock``: not in flight, and no recorded touch within the interval."""
    if token in _touch_in_flight:
        return False
    not_before = _touch_not_before.get(token)
    try:
        return not_before is None or now >= not_before
    except TypeError:  # a naive and an aware clock met (tests only): let it run
        return True


def touch_wanted(token: str, now: datetime) -> bool:
    """A peek: would a touch of *token* run now? It claims nothing — the touch claims when
    it RUNS, so a claim can never be left behind by a request that raised."""
    with _touch_lock:
        return _guard_allows(token, now)


def _claim_locked(token: str, now: datetime) -> bool:
    """Under ``_touch_lock``: claim the session's touch (in flight) if the guard allows it."""
    if not _guard_allows(token, now):
        return False
    _touch_in_flight.add(token)
    return True


def _release_touch(token: str, now: datetime, outcome: TouchOutcome) -> None:
    with _touch_done:
        _touch_in_flight.discard(token)
        if outcome in ("recorded", "lost"):
            _touch_not_before[token] = now + SESSION_TOUCH_INTERVAL
        elif outcome == "failed":
            _touch_not_before[token] = now + SESSION_TOUCH_FAILURE_BACKOFF
        if len(_touch_not_before) > _TOUCH_GUARD_MAX:
            _touch_not_before.clear()
        _touch_done.notify_all()


def _is_busy(exc: sqlite3.Error) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED, extended codes included (the primary code is the low
    byte): another connection holds the lock. Nothing else counts as busy."""
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _prewarm_after_touch() -> None:
    """A recorded touch committed from its own connection, which moves ``PRAGMA
    data_version`` and so empties the result cache (``shared/result_cache.py``). Pre-warm AT
    ONCE (owner ruling 2026-10-22, 「記完立刻預熱」): the touch waited for the page load to
    settle, so no read of that load is left to compete with the build, and a navigation that
    lands while it runs joins it in flight (``alert_inputs.prewarm_today`` and ``GET
    /api/dashboard`` build under one cache key) rather than building again. The same call, and
    the same debounced timer, as the write window's (``api/app.py``): a later schedule
    replaces this one, and a write window open when it fires skips it — that window's end
    schedules the next."""
    if not flags.prewarm_enabled():
        return
    from portfolio_dash.api import alert_inputs  # local: it pulls in the dashboard build

    result_cache.schedule_prewarm(alert_inputs.prewarm_today, SESSION_TOUCH_PREWARM_DELAY_S)


def run_touch(app: Any, token: str, seen_raw: str | None, now: datetime) -> TouchOutcome:
    """Record a use of *token*, dated *now*, right away: best effort, never raises.

    A pending touch (:func:`schedule_touch`) ends in the same work: its timer claims the
    guard in the step that retires it, then calls :func:`_run_claimed`. It opens its OWN connection,
    waits at most :data:`SESSION_TOUCH_BUSY_TIMEOUT_MS` for the write lock and runs one
    autocommit statement. A held lock skips it (the next request arms it again); any other
    error is a WARNING. One touch per session runs at a time in this process, and none again
    within the interval once one recorded (:func:`touch_wanted`). A recorded touch pre-warms
    the result cache at once (:func:`_prewarm_after_touch`).

    When a test has injected ``get_conn`` (the hermetic golden database), the touch writes
    there, exactly as the action-log middleware does — the configured file is not the
    database that request used — and pre-warms nothing (the cache serves the file only).
    """
    with _touch_lock:
        if not _claim_locked(token, now):
            return "throttled"
    return _run_claimed(app, token, seen_raw, now)


def _run_claimed(app: Any, token: str, seen_raw: str | None, now: datetime) -> TouchOutcome:
    """:func:`run_touch` once its claim is held; releases the claim."""
    outcome: TouchOutcome = "failed"
    own = False
    try:
        override = getattr(app, "dependency_overrides", {}).get(get_conn)
        if override is not None:
            injected = override()
            recorded = (isinstance(injected, sqlite3.Connection)
                        and touch_session(injected, token, seen_raw=seen_raw, now=now))
        else:
            own = True
            conn = get_connection()
            try:
                conn.isolation_level = None  # autocommit: the UPDATE is its own transaction
                conn.execute(f"PRAGMA busy_timeout = {int(SESSION_TOUCH_BUSY_TIMEOUT_MS)}")
                recorded = touch_session(conn, token, seen_raw=seen_raw, now=now)
            finally:
                conn.close()
        outcome = "recorded" if recorded else "lost"
    except sqlite3.Error as exc:
        if _is_busy(exc):
            outcome = "busy"
            logger.debug("session touch skipped, the database is busy: %s", exc)
        else:
            logger.warning("session touch failed", exc_info=True)
    except Exception:  # noqa: BLE001 — a bookkeeping write never fails or follows a request
        logger.warning("session touch failed", exc_info=True)
    finally:
        _release_touch(token, now, outcome)
    if outcome == "recorded" and own:
        try:
            _prewarm_after_touch()
        except Exception:  # noqa: BLE001 — the pre-warm only saves the next reader a build
            logger.warning("pre-warm after a session touch could not be scheduled",
                           exc_info=True)
    return outcome


# --- the pending touch: deferred until the page load has settled -----------------------------


class _Cancellable(Protocol):
    def cancel(self) -> None: ...


def _start_timer(delay: float, fn: Callable[[], None]) -> _Cancellable:
    """Run ``fn()`` on a daemon thread *delay* seconds from now. A seam, with
    :data:`_monotonic`: the tests replace both and drive a pending touch without waiting
    (``tests/conftest.py::touch_clock``)."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.name = "session-touch"
    timer.start()
    return timer


#: The pending touch's clock, in seconds. Monotonic, so a step of the wall clock neither fires
#: nor holds a touch; the request's clock (``now``) only dates the use it records.
_monotonic: Callable[[], float] = time.monotonic


@dataclass
class _PendingTouch:
    """One session's armed touch: the latest gated request's view, and when it may fire."""

    app: Any
    seen_raw: str | None
    #: the latest gated request's clock: the use the touch records
    now: datetime
    #: when the latest gated request arrived (:data:`_monotonic`)
    last: float
    #: the cap: the arming request's arrival + :data:`SESSION_TOUCH_MAX_DEFER_S`
    deadline: float
    timer: _Cancellable | None = None

    def due(self) -> float:
        return min(self.last + SESSION_TOUCH_SETTLE_S, self.deadline)


#: The pending touch of each session token, under ``_touch_lock``: at most one per session.
#: An entry leaves when its timer fires (or the stop cancels it), so the map holds only the
#: sessions used within the last :data:`SESSION_TOUCH_MAX_DEFER_S`.
_pending_touches: dict[str, _PendingTouch] = {}


def schedule_touch(app: Any, token: str, seen_raw: str | None, now: datetime) -> bool:
    """The gate's call when the session's touch is due: arm its pending touch, or push the one
    already armed back to :data:`SESSION_TOUCH_SETTLE_S` after this request (never past its
    cap, :data:`SESSION_TOUCH_MAX_DEFER_S` after the request that armed it).

    Pushing back is bookkeeping only: the armed timer, when it fires early, waits out the
    rest itself (:func:`_fire_touch`), so a request never starts a thread for a touch that is
    already armed. The touch records the LATEST request's clock and the stamp it read.

    ``False`` — nothing armed, a later request tries again — when the in-process guard has
    the touch in flight or recorded within the interval (:func:`touch_wanted`), or no timer
    could be started. Never raises: the request goes on either way.
    """
    with _touch_lock:
        if not _guard_allows(token, now):
            return False
        arrived = _monotonic()
        entry = _pending_touches.get(token)
        if entry is not None:
            entry.app, entry.seen_raw, entry.now, entry.last = app, seen_raw, now, arrived
            return True
        entry = _PendingTouch(app=app, seen_raw=seen_raw, now=now, last=arrived,
                              deadline=arrived + SESSION_TOUCH_MAX_DEFER_S)
        try:
            entry.timer = _start_timer(entry.due() - arrived, partial(_fire_touch, token, entry))
        except Exception:  # noqa: BLE001 — e.g. no thread can be started: the next request retries
            logger.warning("session touch could not be armed", exc_info=True)
            return False
        _pending_touches[token] = entry
        return True


def _fire_touch(token: str, entry: _PendingTouch) -> None:
    """A pending touch's timer: run the touch once the session has been quiet for
    :data:`SESSION_TOUCH_SETTLE_S`, or its cap has passed; otherwise wait for the rest. The
    claim is taken in the same step that retires the entry, so no request can arm a second
    touch of the session in between."""
    with _touch_lock:
        if _pending_touches.get(token) is not entry:
            return  # cancelled by the stop
        left = entry.due() - _monotonic()
        if left > 0:  # a later request pushed it back
            try:
                entry.timer = _start_timer(left, partial(_fire_touch, token, entry))
            except Exception:  # noqa: BLE001 — a timer thread has nobody to raise to
                del _pending_touches[token]  # the next request arms it again
                logger.warning("session touch could not be re-armed", exc_info=True)
            return
        del _pending_touches[token]
        if not _claim_locked(token, entry.now):
            return
    _run_claimed(entry.app, token, entry.seen_raw, entry.now)


def cancel_pending_touches(timeout: float = SHUTDOWN_TOUCH_WAIT_S) -> int:
    """The lifespan's stop, BEFORE ``result_cache.shutdown()`` and the WAL checkpoint: cancel
    every pending touch, then wait up to *timeout* seconds for one already running (a commit
    after the checkpoint would leave ``-wal`` / ``-shm``). Returns how many were cancelled.

    A cancelled touch records nothing and loses nothing that matters: the stamp did not move,
    so the session's first request after the boot finds the touch still due and records the
    use then. No request arms a new one during the stop: uvicorn drains the requests in
    flight before it runs the lifespan's stop. Never raises; a touch that outlives the wait
    is logged and the stop goes on.
    """
    with _touch_done:
        pending = list(_pending_touches.values())
        _pending_touches.clear()
        for entry in pending:
            if entry.timer is not None:
                entry.timer.cancel()
        finished = _touch_done.wait_for(lambda: not _touch_in_flight, timeout)
    if not finished:
        logger.warning("a session touch still runs %.0f s into the stop; the WAL checkpoint "
                       "may not complete", timeout)
    return len(pending)


@dataclass(frozen=True)
class SessionBoot:
    """What the boot did to the session table (logged, and kept on ``app.state``)."""

    #: sessions with no ``last_seen_at``, dated from this boot (pre-upgrade or older build)
    dated: int
    expired: int
    locked: int
    #: sessions whose user no longer exists
    orphaned: int

    @property
    def purged(self) -> int:
        return self.expired + self.locked + self.orphaned


def prepare_sessions_at_boot(conn: sqlite3.Connection, *, now: datetime) -> SessionBoot:
    """Date unstamped sessions from *now*, then purge the expired, locked and orphaned ones.

    **Upgrade rule.** A session created before ``last_seen_at`` existed has no recorded use:
    an active one cannot be told apart from an idle one, so it counts from the upgrade (the
    first boot of this build) — nobody is logged out on deploy day, and a session nobody
    uses expires 30 days later. A locked session is never revived (re-login issues a new
    token and deletes it), so it is purged with the expired ones.
    """
    dated = conn.execute(
        "UPDATE auth_sessions SET last_seen_at = ? WHERE last_seen_at IS NULL",
        (now.isoformat(),),
    ).rowcount
    users = {r[0] for r in conn.execute("SELECT username FROM auth_users")}
    expired = locked = orphaned = 0
    doomed: list[tuple[str]] = []
    for token, username, seen, is_locked in conn.execute(
        "SELECT token, username, last_seen_at, locked FROM auth_sessions"
    ).fetchall():
        if username not in users:
            orphaned += 1
        elif _is_expired(_idle_for(seen, now)):
            expired += 1
        elif is_locked:
            locked += 1
        else:
            continue
        doomed.append((token,))
    conn.executemany("DELETE FROM auth_sessions WHERE token = ?", doomed)
    conn.commit()
    return SessionBoot(dated=dated, expired=expired, locked=locked, orphaned=orphaned)


def session_user(conn: sqlite3.Connection, token: str) -> str | None:
    """Username for a known, unlocked token; None if unknown or locked.

    Clock-free on purpose: it names who a request writes as (``deps.request_actor``) and who
    the action log attributes a request to. Whether the session has EXPIRED is the gate's
    decision (:func:`require_session`, :func:`session_state`), and every gated request passes
    the gate before it can write anything.
    """
    row = conn.execute(
        "SELECT username FROM auth_sessions WHERE token = ? AND locked = 0", (token,)
    ).fetchone()
    return row["username"] if row is not None else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
    conn.commit()


def lock_session(conn: sqlite3.Connection, token: str) -> bool:
    """Mark a session locked. False if the token is unknown."""
    cur = conn.execute("UPDATE auth_sessions SET locked = 1 WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount > 0


# --- gate -------------------------------------------------------------------


def require_session(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> None:
    """Global gate (wired in create_app): protect ``/api/*`` in protected mode.

    Allows: non-``/api/`` paths (static files), the open paths (login/session), and
    everything in guest mode. Otherwise requires a known, unlocked, unexpired ``pd_session``
    cookie -> 401 (an expired one says so: :data:`SESSION_EXPIRED_MESSAGE`). Shares
    ``Depends(get_conn)`` — with the routes' ``scope="function"``, so the request has one
    connection — and tests' DB override applies.

    It only READS (DEF-065). When the session's last recorded use is a touch interval old, it
    arms (or pushes back) the session's pending touch (:func:`schedule_touch`), which records
    the use on its own connection once the page load has settled — unless this process has
    the touch in flight or recorded it within the interval (:func:`touch_wanted`). The touch
    is armed before the route runs, so a request whose route RAISES still counts as a use.
    The settle window is measured from each request's arrival: a request still running when
    the session has been quiet that long does not hold the touch back.

    The login it lets through is stored on ``request.state`` (:data:`LOGIN_STATE_ATTR`) for
    the action log; a refused request carries none.
    """
    path = request.url.path
    if not path.startswith("/api/") or path in _OPEN_PATHS:
        return
    if not is_protected(conn):
        return
    token = request.cookies.get("pd_session")
    state = session_state(conn, token, now=now) if token is not None else None
    if token is None or state is None or state.locked:
        raise HTTPException(status_code=401, detail="需要登入")
    if state.expired:
        raise HTTPException(status_code=401, detail=SESSION_EXPIRED_MESSAGE)
    setattr(request.state, LOGIN_STATE_ATTR, state.username)
    if state.touch_due:
        schedule_touch(request.app, token, state.seen_raw, now)
