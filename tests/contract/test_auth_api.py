"""Contract tests for /api/auth/* and /api/users (spec 09).

Uses the guest ``golden_db`` (no user seeded) + ``api_client``. The global gate is
wired in app.py; guest mode keeps every other contract test green.
"""

import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from portfolio_dash.api import auth_store as A

_NOW = datetime(2026, 6, 11, 14, 30)


def test_guest_session(api_client: TestClient) -> None:
    assert api_client.get("/api/auth/session").json() == {"mode": "guest"}


def test_login_bad_credentials_401(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    A.create_user(golden_db, name="家明", username="chiaming", password="password123", now=_NOW)
    r = api_client.post("/api/auth/login", json={"username": "chiaming", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r2 = api_client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "invalid_credentials"


def test_login_success_sets_cookie_and_session(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    A.create_user(golden_db, name="家明", username="chiaming", password="password123", now=_NOW)
    r = api_client.post(
        "/api/auth/login", json={"username": "chiaming", "password": "password123"}
    )
    assert r.status_code == 200 and r.json() == {"username": "chiaming", "name": "家明"}
    assert "pd_session" in r.cookies
    s = api_client.get("/api/auth/session").json()
    assert s["mode"] == "user" and s["username"] == "chiaming" and s["locked"] is False


def test_protected_mode_blocks_without_cookie(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Create a user via the API in guest mode (allowed), then request with no cookie.
    api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )
    api_client.cookies.clear()
    r = api_client.get("/api/dashboard")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_users_crud(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    # First user is created in guest mode (gate open); this activates protected mode.
    r = api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )
    assert r.status_code == 201
    # Now protected: authenticate before further user management.
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert api_client.post(
        "/api/users", json={"name": "x", "username": "chiaming", "password": "password123"}
    ).status_code == 409
    assert api_client.post(
        "/api/users", json={"name": "x", "username": "u2", "password": "short"}
    ).status_code == 400
    users = api_client.get("/api/users").json()
    assert any(u["username"] == "chiaming" and u["is_current"] for u in users)
    assert all("password_hash" not in u for u in users)


def test_logout_and_lock_204(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert api_client.post("/api/auth/lock").status_code == 204
    # After lock, the session is invalid -> relogin to proceed.
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert api_client.post("/api/auth/logout").status_code == 204


def test_gate_allows_authenticated_protected_request(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Coverage gap (senior review): the happy path THROUGH the gate with a valid cookie.
    api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )  # activates protected mode
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    r = api_client.get("/api/dashboard")  # cookie retained by the TestClient
    assert r.status_code == 200


def test_users_gated_in_protected_mode(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Coverage gap (senior review): /api/users is NOT in _OPEN_PATHS -> gated when protected.
    api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )
    api_client.cookies.clear()
    r = api_client.get("/api/users")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_health_exempt_from_gate_in_protected_mode(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # /api/health is in _OPEN_PATHS -> the liveness probe answers WITHOUT a cookie
    # even in protected mode, while a DIFFERENT protected path still 401s (gate intact).
    api_client.post(
        "/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"}
    )  # activates protected mode
    api_client.cookies.clear()
    r = api_client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # The gate is NOT broadly open: another protected /api/* path still requires a cookie.
    blocked = api_client.get("/api/dashboard")
    assert blocked.status_code == 401 and blocked.json()["error"]["code"] == "unauthorized"


def test_create_user_empty_username_400(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post(
        "/api/users", json={"name": "x", "username": "   ", "password": "password123"}
    )
    assert r.status_code == 400 and r.json()["error"]["field"] == "username"
