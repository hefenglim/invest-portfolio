"""E2E — DEF-078 (owner ruling ⑦, 2026-09-26): the settings page never shows a stored or
typed secret in the clear.

Real server, real browser, protected mode (the owner's own view — the full ntfy topic is
shown there, every key and token is not). A 40-character model key, a 40-character data-source
key and three notify secrets are stored; then 設定 › AI 與額度 (model drawer + 重設), 資料來源
(重設金鑰 dialog) and 通知中心 are walked. Pinned:

* the four secret inputs are ``type="password"``, including after 重設 unlocks the key field;
* the stored secrets are shown ONLY as the server's 3 + ••• + 3 mask, as text beside the field;
* no API response the page received and no rendered DOM contains a full secret.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Response, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.ops import notify
from portfolio_dash.pricing.datasources_store import set_api_key
from portfolio_dash.shared.llm_config import ModelConfig, ensure_llm_seeded, upsert_model
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory

_MODEL_KEY = "sk-or-v1-0123456789abcdefghijklmnopqrstu"   # 40 characters
_DS_KEY = "fm-9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeed"      # 40 characters
_NTFY = "tk_" + "n" * 30 + "END"
_TG = "123456:" + "T" * 30 + "KEN"
_MAIL = "app-password-" + "p" * 20 + "WXY"
_SECRETS = (_MODEL_KEY, _DS_KEY, _NTFY, _TG, _MAIL)
_USER, _PASS = "owner", "pw-123456"


def _mask(secret: str) -> str:
    return f"{secret[:3]}•••{secret[-3:]}"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    ensure_llm_seeded(conn)
    upsert_model(conn, ModelConfig(id="qa-model", model_alias="qa-model",
                                   provider="openrouter", model_name="vendor/model",
                                   api_key=_MODEL_KEY))
    set_api_key(conn, "finmind", _DS_KEY)
    notify.ensure_seeded(conn)
    cfg = notify.load_config(conn)
    cfg.ntfy.token = _NTFY
    cfg.telegram.bot_token = _TG
    cfg.email.password = _MAIL
    notify.save_config(conn, cfg, now=GOLDEN_NOW)
    conn.commit()


def _login(page: Page, base: str) -> None:
    page.goto(base + "/login.html", wait_until="load")
    page.fill("#login-user", _USER)
    page.fill("#login-pass", _PASS)
    with page.expect_response("**/api/auth/login") as ok:
        page.click("#login-btn")
    assert ok.value.status == 200
    page.wait_for_url("**/index.html")


def _no_secret_in_dom(page: Page, where: str) -> None:
    html = page.content()
    values = page.evaluate("() => Array.from(document.querySelectorAll('input'))"
                           ".map((n) => n.value).join('\\n')")
    for s in _SECRETS:
        assert s not in html and s not in values, f"{where}: a full secret is on the page"


@pytest.mark.e2e
def test_secrets_are_typed_into_password_fields_and_shown_only_masked(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed, users=[(_USER, _PASS)])
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    pending: list[Response] = []
    bodies: dict[str, str] = {}
    page.on("response", lambda r: pending.append(r) if "/api/" in r.url else None)

    def drain() -> None:
        """Read every API body the page received BEFORE navigating away (a body is gone
        once its page is)."""
        for r in pending:
            if r.status == 200 and r.request.method == "GET":
                bodies[r.url] = r.text()
        pending.clear()

    _login(page, base)
    page.wait_for_load_state("networkidle")
    pending.clear()          # the dashboard's own reads are not this test's subject

    # 設定 › AI 與額度 — the model drawer.
    page.goto(base + "/settings.html#llm", wait_until="networkidle")
    edit = page.locator("#view-llm tr", has_text="qa-model").locator("button", has_text="編輯")
    edit.click()
    key = page.locator("#dr-key")
    expect(key).to_have_attribute("type", "password")
    expect(key).to_have_attribute("autocomplete", "new-password")
    expect(page.locator("#dr-key-current")).to_have_text("目前：" + _mask(_MODEL_KEY))
    _no_secret_in_dom(page, "model drawer")
    page.click("#dr-key-reset")
    expect(key).to_have_attribute("type", "password")      # still hidden while typing
    expect(key).to_be_editable()
    key.fill("sk-new-typed-key-0123456789")
    expect(key).to_have_attribute("type", "password")
    page.click("#drawer-cancel")
    drain()

    # 設定 › 資料來源 — the 重設金鑰 dialog.
    page.goto(base + "/settings.html#datasources", wait_until="networkidle")
    row = page.locator("#view-datasources tr", has_text="FinMind")
    expect(row.locator(".cron-code")).to_have_text(_mask(_DS_KEY))
    row.locator(".ds-key-row button", has_text="重設").click()
    dialog_input = page.locator(".modal-backdrop input.input").last
    expect(dialog_input).to_have_attribute("type", "password")
    expect(dialog_input).to_have_attribute("autocomplete", "new-password")
    _no_secret_in_dom(page, "data-source dialog")
    page.locator(".modal-backdrop .btn", has_text="取消").click()
    drain()

    # 設定 › 通知中心 — the three notify secrets.
    page.goto(base + "/settings.html#notify", wait_until="networkidle")
    for ident, secret in (("nt-ntfy-token", _NTFY), ("nt-tg-token", _TG),
                          ("nt-em-pass", _MAIL)):
        field = page.locator("#" + ident)
        expect(field).to_have_attribute("type", "password")
        expect(page.locator(f"#{ident}-current")).to_have_text("目前：" + _mask(secret))
        # The round trip is unchanged: the field still carries the mask, never the secret.
        assert field.input_value() == _mask(secret)
    _no_secret_in_dom(page, "notify centre")
    drain()

    read = " ".join(bodies)
    for endpoint in ("/api/llm/config", "/api/datasources", "/api/notify/config"):
        assert endpoint in read, f"{endpoint} was never read — the leak check saw nothing"
    leaked = [url for url, body in bodies.items() if any(s in body for s in _SECRETS)]
    assert not leaked, f"an API response carried a full secret: {leaked}"
    assert not page_errors, page_errors
