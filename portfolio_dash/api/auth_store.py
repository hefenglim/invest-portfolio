"""Auth store (spec 09): table DDL, scrypt hashing, user/session CRUD, mode check.

Self-contained access-control store for the api layer. Guest mode = ``auth_users``
empty (everything open); protected mode = >=1 user (a valid ``pd_session`` cookie is
required for all ``/api/*`` except login/session). Stdlib only — no third-party
dependency. Stores **only** salted scrypt hashes; ``password_hash`` is never returned
or logged. Imports only ``shared`` + ``api.deps`` + stdlib (never portfolio/forex/
pricing) — auth is access control, not business calculation.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime

from fastapi import Depends, HTTPException, Request

from portfolio_dash.api.deps import get_conn
from portfolio_dash.shared import config_store

# scrypt parameters — named constants so verify reuses exactly what hash used.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

# Paths exempt from the session gate (login + the gate-query endpoint itself).
_OPEN_PATHS = {"/api/auth/login", "/api/auth/session"}


def create_auth_tables(conn: sqlite3.Connection) -> None:
    """Create the auth tables if absent (idempotent; safe on every startup)."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS auth_users ("
        " username TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " password_hash TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS auth_sessions ("
        " token TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0);"
    )
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
    """Create a fresh session token; clears that user's locked sessions (re-login
    unlock per spec 9.2)."""
    conn.execute("DELETE FROM auth_sessions WHERE username = ? AND locked = 1", (username,))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions (token, username, created_at, locked) VALUES (?, ?, ?, 0)",
        (token, username, now.isoformat()),
    )
    conn.commit()
    return token


def session_user(conn: sqlite3.Connection, token: str) -> str | None:
    """Username for a valid, unlocked token; None if unknown or locked."""
    row = conn.execute(
        "SELECT username FROM auth_sessions WHERE token = ? AND locked = 0", (token,)
    ).fetchone()
    return row["username"] if row is not None else None


def session_row(conn: sqlite3.Connection, token: str) -> dict[str, object] | None:
    """Row for ANY known token (including locked): ``{username, locked: bool}``.

    Used by GET /auth/session to tell a locked-but-known session apart from an
    unknown cookie (``session_user`` cannot, as it returns None for locked rows).
    """
    row = conn.execute(
        "SELECT username, locked FROM auth_sessions WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    return {"username": row["username"], "locked": bool(row["locked"])}


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
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> None:
    """Global gate (wired in create_app): protect ``/api/*`` in protected mode.

    Allows: non-``/api/`` paths (static files), the open paths (login/session), and
    everything in guest mode. Otherwise requires a valid, unlocked ``pd_session``
    cookie -> 401. Shares ``Depends(get_conn)`` so tests' DB override applies.
    """
    path = request.url.path
    if not path.startswith("/api/") or path in _OPEN_PATHS:
        return
    if not is_protected(conn):
        return
    token = request.cookies.get("pd_session")
    if token is None or session_user(conn, token) is None:
        raise HTTPException(status_code=401, detail="需要登入")
