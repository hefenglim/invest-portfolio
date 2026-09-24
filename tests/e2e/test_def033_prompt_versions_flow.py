"""E2E (Playwright, real server + real frontend) — DEF-033 + DEF-003 on the pages the verifier uses.

DEF-033 (owner ruling 2026-09-24): 設定 › AI 提示詞 › a strategy card now carries its version
(「v2・更新 …」) and a 版本記錄 button. The flow walks the ruling end to end in a browser:
the history lists every saved version with its time and source, 對照目前 renders the
server's line diff, 回復此版 asks for confirmation INSIDE the history modal (a confirmDialog
would stack under it — z 72 vs 80), and confirming records a NEW version (v3 「回復（回復自
v1）」), puts v1's text back in the card and on the server, and never rewrites v1/v2.

On AI 洞察 › 持倉健診 the same flow checks the card half of both rulings: a card records the
prompt version it was generated with (「提示詞 v2」; a legacy card 「生成時版本未記錄」), and a
card whose prediction is null reads 「純描述・無預測」 even when its stored confidence is 0 —
the verifier's #182 (DEF-003).
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
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.llm_insight.composer_store import StrategyVersionRef
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
V1 = "第一行\n共同段落"
V2 = "第二行\n共同段落"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="我的健診", body=V1, now=NOW)
    cs.update_strategy(conn, sp.id, name="我的健診", body=V2, enabled=True, now=NOW)
    it = cs.create_insight_type(conn, name="健診", scope="per_symbol", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    # legacy card (before the record existed), WITH a prediction → 「信心 55%」
    _card(conn, it.id, "legacy", InsightCard(
        title="台積電觀察", summary="偏多", body_md="b", symbol="2330", confidence=55,
        prediction=Prediction(metric="price_change", direction="up", horizon_days=5)), None)
    # #182's shape: NO prediction, stored confidence 0; generated with strategy v2
    _card(conn, it.id, "new", InsightCard(
        title="玉山金描述", summary="描述", body_md="b", symbol="2884", confidence=0),
        [StrategyVersionRef(strategy_id=sp.id, name="我的健診", version=2)])


def _card(conn: sqlite3.Connection, it_id: int, fp: str, card: InsightCard,
          versions: list[StrategyVersionRef] | None) -> None:
    istore.add_card(
        conn, insight_type_id=it_id, card=card, fingerprint=fp, calibration_version=None,
        horizon_days=5, input_snapshot="x", model="m", cost_usd=Decimal("0"), now=NOW,
        strategy_versions=versions,
    )


_TOAST_JS = """
(want) => Array.from(document.querySelectorAll('.toast'))
    .some((t) => (t.textContent || '').indexOf(want) !== -1)
"""


@pytest.mark.e2e
def test_version_history_diff_and_restore_then_the_cards_name_their_version(
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

    # --- the card shows its version; 版本記錄 lists every version ----------------------------
    page.goto(base + "/settings.html#prompts", wait_until="load")
    card = page.locator(".tpl-card", has=page.locator(".tpl-name", has_text="我的健診"))
    card.wait_for()
    assert (card.locator(".tpl-meta").text_content() or "").startswith("v2・更新")
    card.locator(".tpl-head").click()
    card.locator("button.tpl-versions").click()
    rows = page.locator(".pv-box .ver-row")
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 2")
    first = rows.nth(0).text_content() or ""
    second = rows.nth(1).text_content() or ""
    assert "v2" in first and "目前" in first and "儲存" in first
    assert "v1" in second and "新增" in second
    assert rows.nth(0).locator(".ver-restore").count() == 0  # the current one has no restore

    # --- 檢視: any version's full text ----------------------------------------------------
    rows.nth(1).get_by_role("button", name="檢視").click()
    page.locator(".ver-body-pre").wait_for()
    assert page.locator(".ver-body-pre").text_content() == V1

    # --- 對照目前: the server's line diff, v1 → v2 ------------------------------------------
    rows.nth(1).get_by_role("button", name="對照目前").click()
    page.locator(".ver-diff-pre .ver-line").first.wait_for()
    head = page.locator(".ver-diff-head").text_content() or ""
    assert "v1 → v2" in head and "＋1 行・－1 行" in head
    assert page.locator(".ver-line.ver-del").all_text_contents() == ["- 第一行"]
    assert page.locator(".ver-line.ver-add").all_text_contents() == ["+ 第二行"]

    # --- 回復此版: confirm inside the modal, then a NEW version ----------------------------
    rows.nth(1).locator(".ver-restore").click()
    page.locator(".ver-confirm:not([hidden])").wait_for()
    assert "另存為新版本" in (page.locator(".ver-confirm").text_content() or "")
    page.locator(".ver-confirm-ok").click()
    page.wait_for_function(_TOAST_JS, arg="已回復至 v1")
    page.wait_for_function("() => document.querySelectorAll('.pv-box .ver-row').length === 3")
    top = page.locator(".pv-box .ver-row").nth(0).text_content() or ""
    assert "v3" in top and "目前" in top and "回復（回復自 v1）" in top
    # v1 and v2 are still there, unchanged
    assert "v2" in (page.locator(".pv-box .ver-row").nth(1).text_content() or "")
    assert "v1" in (page.locator(".pv-box .ver-row").nth(2).text_content() or "")
    fresh = page.locator(".tpl-card", has=page.locator(".tpl-name", has_text="我的健診"))
    assert fresh.locator("textarea").input_value() == V1
    assert (fresh.locator(".tpl-meta").text_content() or "").startswith("v3・更新")
    served = page.request.get(base + "/api/strategy-prompts").json()
    assert [s["body"] for s in served if s["name"] == "我的健診"] == [V1]

    # --- AI 洞察 › 持倉健診: the version each card was built with; DEF-003's chip -----------
    page.goto(base + "/insights.html", wait_until="load")
    page.locator("#ins-tabs button[data-tab='health']").click()
    new_card = page.locator(".hc-card", has=page.locator(".sym-code", has_text="2884"))
    new_card.wait_for()
    foot = new_card.locator(".insight-foot").text_content() or ""
    assert "純描述・無預測" in foot and "信心" not in foot  # prediction null → no confidence
    assert new_card.locator(".insight-pver").text_content() == "提示詞 v2"
    assert new_card.locator(".insight-pver").get_attribute("href") == "settings.html#prompts"
    legacy = page.locator(".hc-card", has=page.locator(".sym-code", has_text="2330"))
    assert "信心 55%" in (legacy.locator(".insight-foot").text_content() or "")
    assert legacy.locator(".insight-pver").text_content() == "生成時版本未記錄"

    assert errors == []
