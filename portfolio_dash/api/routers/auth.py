"""Auth API (spec 9.1-9.2): login, session, logout, lock.

Thin over ``auth_store``. login establishes a server-side session + HttpOnly cookie;
session is the page-load gate query (exempt from the global gate, so it reads the
cookie itself); logout/lock return 204 — logout is exempt from the gate too (2026-10-22), so
an expired cookie can be cleared. Bad username and bad password return the same
401 (no user enumeration). Never returns ``password_hash``.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import auth_store as A
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body

router = APIRouter()

_COOKIE = "pd_session"


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> Any:
    if not A.authenticate(conn, body.username, body.password):
        return JSONResponse(
            status_code=401,
            content=error_body("invalid_credentials", "帳號或密碼錯誤"),
        )
    # the action log names who logged in (an open path: the gate set no login)
    setattr(request.state, A.LOGIN_STATE_ATTR, body.username)
    token = A.create_session(conn, body.username, now=now)
    user = A.get_user(conn, body.username)
    name = user["name"] if user is not None else body.username
    response.set_cookie(
        key=_COOKIE, value=token, httponly=True, samesite="lax", path="/"
    )
    return {"username": body.username, "name": name}


@router.get("/auth/session")
def session(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    pd_session: str | None = Cookie(default=None),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    # Case 1: guest mode (no users) -> everything open.
    if not A.is_protected(conn):
        return {"mode": "guest"}
    signed_out: dict[str, Any] = {"mode": "user", "username": None, "name": None,
                                  "locked": False}
    # Case 2: protected + known, unexpired cookie (even if locked -> surface lock state).
    if pd_session is not None:
        state = A.session_state(conn, pd_session, now=now)
        if state is not None and not state.expired:
            user = A.get_user(conn, state.username)
            name = user["name"] if user is not None else None
            return {
                "mode": "user",
                "username": state.username,
                "name": name,
                "locked": state.locked,
            }
        # Case 3: protected + a cookie that no longer opens a session (2026-10-22). Signed
        # out, plus the reason for the login page: expired after 30 days without use, or a
        # token the store no longer knows (the boot purged it, its user was removed). The
        # expired row is left alone — a GET never writes; the boot purges it.
        expired = state is not None
        return {**signed_out, "ended": "expired" if expired else "invalid",
                "notice": A.SESSION_EXPIRED_MESSAGE if expired else A.SESSION_INVALID_MESSAGE}
    # Case 4: protected + no cookie -> signed-out (shell shows login).
    return signed_out


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    pd_session: str | None = Cookie(default=None),
    now: datetime = Depends(get_now),
) -> Response:
    """An open path (2026-10-22): it ends the session the request's OWN cookie names —
    valid, expired, locked or unknown — and always clears the cookie, so an expired login can
    be cleared. The action log names the login only when that session was still live."""
    if pd_session is not None:
        state = A.session_state(conn, pd_session, now=now)
        if A.is_protected(conn) and state is not None and not (state.expired or state.locked):
            setattr(request.state, A.LOGIN_STATE_ATTR, state.username)
        A.delete_session(conn, pd_session)
    response.delete_cookie(_COOKIE, path="/")
    response.status_code = 204
    return response


@router.post("/auth/lock")
def lock(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    pd_session: str | None = Cookie(default=None),
) -> Response:
    # Require a valid (unlocked, known) session before locking.
    if pd_session is None or A.session_user(conn, pd_session) is None:
        return JSONResponse(status_code=401, content=error_body("unauthorized", "需要登入"))
    A.lock_session(conn, pd_session)
    return Response(status_code=204)
