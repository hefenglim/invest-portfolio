"""Contract tests for the notify settings API (WP 3B): masked config + test-send.

Self-contained: an in-memory DB, a local FastAPI app mounting ONLY ``notify.router``,
``get_conn``/``get_now`` overridden, sockets re-enabled for the in-process TestClient
transport, and the channel send monkeypatched so NO real network. Mirrors the LLM-settings
contract-test conventions (masked read, placeholder-preserving write, hermetic test-send).

Auth modes (security review F1): the default ``conn``/``client`` fixtures run PROTECTED
(one auth user seeded — the router's guest lockdown checks ``auth_store.is_protected``,
not the session gate, which is app-level). ``guest_client`` runs the same app over a
guest DB (auth tables present, ZERO users) for the 403-lockdown + masked-topic tests.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.auth_store import create_auth_tables, create_user
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import notify as notify_router
from portfolio_dash.ops import notify

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _make_conn(*, protected: bool) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    notify.ensure_seeded(c)
    create_auth_tables(c)
    if protected:
        create_user(c, name="Owner", username="owner", password="password123", now=NOW)
    return c


def _make_client(conn: sqlite3.Connection) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(notify_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    return app, TestClient(app)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = _make_conn(protected=True)
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app, c = _make_client(conn)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


@pytest.fixture
def guest_client() -> Iterator[TestClient]:
    """The same router over a GUEST-mode DB (no auth users — the public demo)."""
    enable_socket()
    conn = _make_conn(protected=False)
    app, c = _make_client(conn)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        conn.close()
        disable_socket(allow_unix_socket=True)


def test_get_config_masked_defaults(client: TestClient) -> None:
    b = client.get("/api/notify/config").json()
    # protected mode: full topic shown so the owner can copy it to the phone
    assert b["ntfy"]["topic"].startswith("pd-")
    assert b["ntfy"]["token_masked"] is None and b["ntfy"]["token_set"] is False
    assert b["telegram"]["bot_token_masked"] is None
    assert "bot_token" not in b["telegram"]  # raw secret never serialized
    assert "password" not in b["email"]
    assert len(b["rule_catalog"]) == len(notify.RULE_CATALOG)
    assert all(b["subscriptions"][rid] for rid, _, _ in notify.RULE_CATALOG)


def test_put_sets_and_masks_secret(client: TestClient, conn: sqlite3.Connection) -> None:
    r = client.put("/api/notify/config", json={
        "telegram": {"enabled": True, "bot_token": "123456:SECRETTOKEN", "chat_id": "9988"}
    })
    assert r.status_code == 200
    b = r.json()
    assert b["telegram"]["enabled"] is True and b["telegram"]["chat_id"] == "9988"
    assert b["telegram"]["bot_token_set"] is True
    assert b["telegram"]["bot_token_masked"] == "123•••KEN"
    assert notify.load_config(conn).telegram.bot_token == "123456:SECRETTOKEN"  # stored raw


def test_put_masked_placeholder_preserves_secret(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    client.put("/api/notify/config", json={"telegram": {"bot_token": "123456:SECRETTOKEN"}})
    # send the mask back (unchanged) + only edit chat_id
    client.put("/api/notify/config", json={
        "telegram": {"bot_token": "123•••KEN", "chat_id": "42"}
    })
    cfg = notify.load_config(conn)
    assert cfg.telegram.bot_token == "123456:SECRETTOKEN"  # preserved, not overwritten by mask
    assert cfg.telegram.chat_id == "42"


def test_put_empty_string_clears_secret(client: TestClient, conn: sqlite3.Connection) -> None:
    client.put("/api/notify/config", json={"telegram": {"bot_token": "123456:SECRETTOKEN"}})
    client.put("/api/notify/config", json={"telegram": {"bot_token": ""}})
    assert notify.load_config(conn).telegram.bot_token == ""


@pytest.mark.parametrize("payload", [
    {"email": {"tls": "wat"}},
    {"email": {"port": 0}},
    {"email": {"port": 70000}},
    {"quiet_hours": {"start": "25:00"}},
    {"quiet_hours": {"end": "8am"}},
    {"ntfy": {"server": "ftp://nope"}},
    # F1 URL hygiene: userinfo smuggling in the ntfy server URL
    {"ntfy": {"server": "https://ntfy.sh@evil.example"}},
    {"ntfy": {"server": "https://user:pw@evil.example"}},
    # F1 host hygiene: SMTP host must be a bare host (no empty / whitespace / '/@:')
    {"email": {"host": ""}},
    {"email": {"host": "   "}},
    {"email": {"host": "smtp.example.com/path"}},
    {"email": {"host": "user@smtp.example.com"}},
    {"email": {"host": "smtp.example.com:587"}},
    {"email": {"host": "smtp.exa mple.com"}},
    # FU-D17: the public base URL must be empty or an http(s) URL — never a bare host.
    {"public_base_url": "invest.example.com"},
    {"public_base_url": "ftp://nope"},
    {"public_base_url": "javascript:alert(1)"},
])
def test_put_junk_400(client: TestClient, payload: dict[str, object]) -> None:
    r = client.put("/api/notify/config", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_put_subscriptions_merge_and_ignore_junk(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    r = client.put("/api/notify/config", json={
        "subscriptions": {"single_weight": False, "bogus_rule": True}
    })
    b = r.json()
    assert b["subscriptions"]["single_weight"] is False
    assert "bogus_rule" not in b["subscriptions"]  # junk keys ignored
    assert notify.load_config(conn).subscriptions.get("bogus_rule") is None


# --- FU-D17: public base URL (deep links; NOT a secret) -------------------------


def test_get_config_public_base_url_default_empty(client: TestClient) -> None:
    b = client.get("/api/notify/config").json()
    assert b["public_base_url"] == ""  # default: deep links off


def test_put_public_base_url_strips_trailing_slash(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    r = client.put(
        "/api/notify/config", json={"public_base_url": "https://invest.example.com/"}
    )
    assert r.status_code == 200
    assert r.json()["public_base_url"] == "https://invest.example.com"  # trailing slash gone
    assert notify.load_config(conn).public_base_url == "https://invest.example.com"


def test_put_public_base_url_empty_clears(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    client.put("/api/notify/config", json={"public_base_url": "https://x.co"})
    client.put("/api/notify/config", json={"public_base_url": ""})
    assert notify.load_config(conn).public_base_url == ""


def test_test_send_unconfigured_channel(client: TestClient) -> None:
    b = client.post("/api/notify/test", json={"channel": "telegram"}).json()
    assert b["ok"] is False and "尚未設定" in b["detail"]


def test_test_send_hermetic_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ntfy has a seeded topic -> buildable. Monkeypatch the fan-out so NO network.
    monkeypatch.setattr(notify, "dispatch", lambda *a, **k: {"ntfy": "ok"})
    b = client.post("/api/notify/test", json={"channel": "ntfy"}).json()
    assert b["channel"] == "ntfy" and b["ok"] is True and b["detail"] == "ok"


def test_test_send_unknown_channel_400(client: TestClient) -> None:
    r = client.post("/api/notify/test", json={"channel": "sms"})
    assert r.status_code == 400


# --- guest-mode lockdown (security review F1) -----------------------------------


def test_guest_put_403(guest_client: TestClient) -> None:
    r = guest_client.put("/api/notify/config", json={"ntfy": {"enabled": True}})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_guest_test_send_403(guest_client: TestClient) -> None:
    r = guest_client.post("/api/notify/test", json={"channel": "ntfy"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_guest_get_masks_topic(guest_client: TestClient) -> None:
    b = guest_client.get("/api/notify/config").json()
    n = b["ntfy"]
    assert "topic" not in n  # the read secret NEVER reaches a demo visitor in full
    assert n["topic_set"] is True  # seeded topic exists...
    assert "•••" in n["topic_masked"]  # ...but only its mask is shown
    # the seeded topic ("pd-" + 24 hex chars) must not be reconstructable from the wire
    assert len(n["topic_masked"]) < 10
    # the rest of the read surface still works for the demo (catalog, subscriptions)
    assert len(b["rule_catalog"]) == len(notify.RULE_CATALOG)
    assert "token_masked" in n and "bot_token_masked" in b["telegram"]
    # the public base URL is NOT a secret → exposed even to a demo visitor (FU-D17)
    assert b["public_base_url"] == ""


def test_protected_put_allowed(client: TestClient) -> None:
    # protected mode: the same write the guest gets 403 for succeeds (owner via gate)
    r = client.put("/api/notify/config", json={"ntfy": {"enabled": True}})
    assert r.status_code == 200
    assert r.json()["ntfy"]["enabled"] is True
