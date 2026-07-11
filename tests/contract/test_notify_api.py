"""Contract tests for the notify settings API (WP 3B): masked config + test-send.

Self-contained: an in-memory DB, a local FastAPI app mounting ONLY ``notify.router``,
``get_conn``/``get_now`` overridden, sockets re-enabled for the in-process TestClient
transport, and the channel send monkeypatched so NO real network. Mirrors the LLM-settings
contract-test conventions (masked read, placeholder-preserving write, hermetic test-send).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import notify as notify_router
from portfolio_dash.ops import notify

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    notify.ensure_seeded(c)
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(notify_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    c = TestClient(app)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


def test_get_config_masked_defaults(client: TestClient) -> None:
    b = client.get("/api/notify/config").json()
    assert b["ntfy"]["topic"].startswith("pd-")  # topic shown so it can be copied
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
