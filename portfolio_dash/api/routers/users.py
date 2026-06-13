"""Authorized-user management API (spec 9.3): GET/POST/DELETE.

Thin over ``auth_store``. Adding the first user activates protected mode (allowed in
guest mode for the bootstrap flow). Responses never include ``password_hash``;
``is_current`` is derived from the request's ``pd_session`` cookie.
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
_MIN_PASSWORD = 8


class NewUser(BaseModel):
    name: str
    username: str
    password: str


def _current_username(conn: sqlite3.Connection, token: str | None) -> str | None:
    if token is None:
        return None
    return A.session_user(conn, token)


@router.get("/users")
def list_all(
    conn: sqlite3.Connection = Depends(get_conn),
    pd_session: str | None = Cookie(default=None),
) -> list[dict[str, Any]]:
    current = _current_username(conn, pd_session)
    return [
        {
            "username": u["username"],
            "name": u["name"],
            "created_at": u["created_at"],
            "is_current": u["username"] == current,
        }
        for u in A.list_users(conn)
    ]


@router.post("/users")
def create(
    body: NewUser,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if len(body.password) < _MIN_PASSWORD:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", "密碼至少 8 字", field="password"),
        )
    if A.user_exists(conn, body.username):
        return JSONResponse(
            status_code=409,
            content=error_body("duplicate_username", "帳號已存在"),
        )
    A.create_user(conn, name=body.name, username=body.username, password=body.password, now=now)
    created = A.get_user(conn, body.username)
    created_at = created["created_at"] if created is not None else now.isoformat()
    return JSONResponse(
        status_code=201,
        content={
            "username": body.username,
            "name": body.name,
            "created_at": created_at,
            "is_current": False,
        },
    )


@router.delete("/users/{username}", status_code=204)
def delete(
    username: str,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    pd_session: str | None = Cookie(default=None),
) -> Response:
    is_self = _current_username(conn, pd_session) == username
    A.delete_user(conn, username)  # also deletes that user's sessions
    if is_self:
        response.delete_cookie(_COOKIE, path="/")
    response.status_code = 204
    return response
