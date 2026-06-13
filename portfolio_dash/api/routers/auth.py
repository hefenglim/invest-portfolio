"""Auth API (spec 9.1-9.2): login, session, logout, lock.

Thin over ``auth_store``. login establishes a server-side session + HttpOnly cookie;
session is the page-load gate query (exempt from the global gate, so it reads the
cookie itself); logout/lock return 204. Bad username and bad password return the same
401 (no user enumeration). Never returns ``password_hash``.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Response
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
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if not A.authenticate(conn, body.username, body.password):
        return JSONResponse(
            status_code=401,
            content=error_body("invalid_credentials", "帳號或密碼錯誤"),
        )
    token = A.create_session(conn, body.username, now=now)
    user = A.get_user(conn, body.username)
    name = user["name"] if user is not None else body.username
    response.set_cookie(
        key=_COOKIE, value=token, httponly=True, samesite="lax", path="/"
    )
    return {"username": body.username, "name": name}


@router.get("/auth/session")
def session(
    conn: sqlite3.Connection = Depends(get_conn),
    pd_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    # Case 1: guest mode (no users) -> everything open.
    if not A.is_protected(conn):
        return {"mode": "guest"}
    # Case 2: protected + known cookie (even if locked -> surface lock state).
    if pd_session is not None:
        row = A.session_row(conn, pd_session)
        if row is not None:
            username = row["username"]
            user = A.get_user(conn, str(username))
            name = user["name"] if user is not None else None
            return {
                "mode": "user",
                "username": username,
                "name": name,
                "locked": row["locked"],
            }
    # Case 3: protected + absent/unknown cookie -> signed-out (shell shows login).
    return {"mode": "user", "username": None, "name": None, "locked": False}


@router.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    pd_session: str | None = Cookie(default=None),
) -> Response:
    if pd_session is not None:
        A.delete_session(conn, pd_session)
    response.delete_cookie(_COOKIE, path="/")
    response.status_code = 204
    return response


@router.post("/auth/lock")
def lock(
    conn: sqlite3.Connection = Depends(get_conn),
    pd_session: str | None = Cookie(default=None),
) -> Response:
    # Require a valid (unlocked, known) session before locking.
    if pd_session is None or A.session_user(conn, pd_session) is None:
        return JSONResponse(status_code=401, content=error_body("unauthorized", "需要登入"))
    A.lock_session(conn, pd_session)
    return Response(status_code=204)
