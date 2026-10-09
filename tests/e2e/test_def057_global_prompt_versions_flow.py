"""E2E (Playwright, real server + real frontend) — DEF-057 on the pages the verifier uses.

Owner ruling 2026-09-24: the SYSTEM prompt and the NEWS-ORGANIZER prompt keep versions like
the strategy prompts (DEF-033). 設定 › AI 提示詞: each of the two panels leads its meta line
with the version (「v3・更新 …」) and carries a 版本記錄 button opening the SAME history modal
the strategy cards use — list with time and source, 對照目前 (the server's line diff), 回復此版
confirmed inside the modal, recording a NEW version and putting the text back in the panel's
textarea. On AI 洞察 › 持倉健診 a card names the system-prompt version it was built with
(「提示詞 系統 v2・v1」).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import ConsoleMessage, Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight import system_prompt as sp
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.llm_insight.composer_store import StrategyVersionRef
from portfolio_dash.llm_insight.system_prompt import SystemPromptRef
from portfolio_dash.news import organizer_prompt as npr
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
SYS2 = "系統第二版\n共同守則"
SYS3 = "系統第三版\n共同守則"
NEWS2 = "新聞整理 自訂版 title news_date body_summary related_stocks"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    sp.set_system_prompt(conn, SYS2, now=NOW)   # v2 (v1 = the seeded official body)
    sp.set_system_prompt(conn, SYS3, now=NOW)   # v3
    npr.set_news_prompt(conn, NEWS2, now=NOW)   # news v2
    s = cs.create_strategy(conn, name="我的健診", body="健診 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="健診", scope="per_symbol", now=NOW)
    cs.set_strategies(conn, it.id, [(s.id, 0)])
    istore.ensure_tables(conn)
    istore.add_card(
        conn, insight_type_id=it.id, card=InsightCard(
            title="玉山金描述", summary="描述", body_md="b", symbol="2884", confidence=0),
        fingerprint="new", calibration_version=None, horizon_days=5, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW,
        strategy_versions=[StrategyVersionRef(strategy_id=s.id, name="我的健診", version=1)],
        system_prompt_ref=SystemPromptRef(used=True, version=2),
    )


_TOAST_JS = """
(want) => Array.from(document.querySelectorAll('.toast'))
    .some((t) => (t.textContent || '').indexOf(want) !== -1)
"""


@pytest.mark.e2e
def test_system_and_news_prompt_history_diff_restore_and_the_card_names_its_version(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # --- 系統提示詞: the version leads the meta line; 版本記錄 lists every version -----------
    page.goto(base + "/settings.html#prompts", wait_until="load")
    page.wait_for_function(
        "() => (document.getElementById('sys-prompt-meta').textContent || '').startsWith('v3・')")
    assert page.locator("#sys-prompt").input_value() == SYS3
    page.locator("#sys-versions").click()
    rows = page.locator(".pv-box .ver-row")
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 3")
    assert "版本記錄 — 系統提示詞" in (page.locator(".pv-box .pv-title").text_content() or "")
    top = rows.nth(0).text_content() or ""
    assert "v3" in top and "目前" in top and "儲存" in top
    assert "啟用版本記錄時的內容" in (rows.nth(2).text_content() or "")

    # --- 對照目前: the server's line diff, v2 → v3 ----------------------------------------
    rows.nth(1).get_by_role("button", name="對照目前").click()
    page.locator(".ver-diff-pre .ver-line").first.wait_for()
    head = page.locator(".ver-diff-head").text_content() or ""
    assert "v2 → v3" in head and "＋1 行・－1 行" in head
    assert page.locator(".ver-line.ver-del").all_text_contents() == ["- 系統第二版"]
    assert page.locator(".ver-line.ver-add").all_text_contents() == ["+ 系統第三版"]

    # --- 回復此版 v2: confirm inside the modal → a NEW v4, text back in the textarea -------
    rows.nth(1).locator(".ver-restore").click()
    page.locator(".ver-confirm:not([hidden])").wait_for()
    assert "另存為新版本" in (page.locator(".ver-confirm").text_content() or "")
    page.locator(".ver-confirm-ok").click()
    page.wait_for_function(_TOAST_JS, arg="已回復至 v2")
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 4")
    top = page.locator(".pv-box .ver-row").nth(0).text_content() or ""
    assert "v4" in top and "回復（回復自 v2）" in top
    assert page.locator("#sys-prompt").input_value() == SYS2
    assert (page.locator("#sys-prompt-meta").text_content() or "").startswith("v4・")
    served = page.request.get(base + "/api/system-prompt").json()
    assert served["body"] == SYS2 and served["current_version"] == 4
    page.locator(".pv-box .sd-close").click()

    # --- 新聞整理提示詞: same modal, its own history ----------------------------------------
    assert (page.locator("#news-prompt-meta").text_content() or "").startswith("v2・")
    page.locator("#news-versions").click()
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 2")
    assert "版本記錄 — 新聞整理提示詞" in (page.locator(".pv-box .pv-title").text_content() or "")
    page.locator(".pv-box .ver-row").nth(1).locator(".ver-restore").click()
    page.locator(".ver-confirm-ok").click()
    page.wait_for_function(_TOAST_JS, arg="已回復至 v1")
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 3")
    assert page.locator("#news-prompt").input_value() == official_templates.NEWS_ORGANIZER_PROMPT
    assert (page.locator("#news-prompt-badge").text_content() or "") == "與官方版相同"
    assert (page.locator("#news-prompt-meta").text_content() or "").startswith("v3・")
    page.locator(".pv-box .sd-close").click()

    # --- AI 洞察 › 持倉健診: the card names the system-prompt version it was built with -------
    page.goto(base + "/insights.html", wait_until="load")
    page.locator("#ins-tabs button[data-tab='health']").click()
    card = page.locator(".hc-card", has=page.locator(".sym-code", has_text="2884"))
    card.wait_for()
    chip = card.locator(".insight-pver")
    assert chip.text_content() == "提示詞 系統 v2・v1"
    assert "系統提示詞 v2" in (chip.get_attribute("title") or "")

    # --- the deep links the 新功能 entry uses land ON each panel (DEF-038 grammar) ----------
    for anchor, panel in (("news-prompt", "#news-prompt-panel"),
                          ("system-prompt", "[data-anchor='system-prompt']")):
        page.goto("about:blank")  # a fresh document, so the hash is read on load
        page.goto(f"{base}/settings.html#prompts/{anchor}", wait_until="load")
        page.wait_for_function(
            "(sel) => { const r = document.querySelector(sel).getBoundingClientRect();"
            " return r.top >= -2 && r.top < window.innerHeight / 2; }", arg=panel)

    assert errors == []
