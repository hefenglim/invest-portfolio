"""E2E (Playwright, real server + real frontend) — R6 DEF-069 + DEF-071 on the pages G-08 used.

The verifier's G-08 reproduction (a frozen-clock live server) ended on two screens:

* AI 洞察 › 持倉健診 headed every symbol with its SHADOW card (「…（已套校正）…校正 v2」) and
  folded the shown card into 「歷史 2 筆」 — DEF-069;
* the task drawer's ④ 校正版本鏈 offered only 「封存」, so neither v1 nor a winning shadow could
  be adopted while auto_promote was off — DEF-071; the diagnosis G7 「前往校正版本鏈」 landed on
  a chain with nothing to press.

The time-advance part of G-08 (batches on 09-01 / 09-10 / 09-11 / 09-21, scoring, promotion,
the next version) is replayed at the service seam in
``tests/scheduler/test_def070_shadow_period.py``; this flow seeds the END STATE of that story
directly into the server's database — v1 and v2, no active version, one shown card and one
newer shadow card for 2330 — and walks the two screens.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import ConsoleMessage, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 10, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, ModelConfig(
        id="master", model_alias="master", provider="openai", model_name="master",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(conn, LLMRole.MASTER, "master")  # else G7 is the master-missing warning
    sp = cs.create_strategy(conn, name="健診模板", body="{{kpis_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="個股健檢", scope="per_symbol", self_correct=True,
        universe={"mode": "custom", "symbols": ["2330"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    cs.create_calibration(conn, it.id, body="QA-CAL v1", cause="樣本達門檻", now=NOW)
    cs.create_calibration(conn, it.id, body="QA-CAL v2", cause="連續失誤", now=NOW)
    for shadow, title in ((False, "2330 生效卡"), (True, "2330 影子卡（已套校正）")):
        istore.add_card(
            conn, insight_type_id=it.id,
            card=InsightCard(
                title=title, summary="s", body_md="b", symbol="2330", confidence=70,
                prediction=Prediction(metric="price_change", direction="up", horizon_days=5),
            ),
            fingerprint=f"fp-{shadow}", calibration_version=2 if shadow else None,
            horizon_days=5, input_snapshot="x", model="m", cost_usd=Decimal("0"), now=NOW,
            is_shadow=shadow,
        )


def _active(page: Page, base: str) -> object:
    rows = page.request.get(f"{base}/api/insight-tasks").json()
    return next(r for r in rows if r["name"] == "個股健檢")["active_calibration_version"]


@pytest.mark.e2e
def test_shadow_card_is_not_the_head_card_and_a_version_can_be_adopted(
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
    page.set_viewport_size({"width": 1280, "height": 900})

    # --- DEF-069: 持倉健診 heads 2330 with the SHOWN card; the shadow is nowhere -----------
    page.goto(f"{base}/insights.html", wait_until="load")
    page.locator("#ins-tabs button[data-tab='health']").click()
    head = page.locator(".hc-card", has=page.locator(".sym-code", has_text="2330"))
    head.wait_for()
    expect(head).to_contain_text("2330 生效卡")
    expect(page.locator("#ins-pane-health")).not_to_contain_text("影子卡")
    expect(page.locator(".hc-hist-toggle")).to_have_count(0)  # no 「歷史 1 筆」 either
    served = page.request.get(f"{base}/api/insights?symbol=2330").json()  # the drawer's AI 建議
    assert [r["title"] for r in served["rows"]] == ["2330 生效卡"]

    # --- DEF-071: G7's fix lands on 「設為生效」 ---------------------------------------------
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")
    page.click(".pp-card button:has-text('乾跑預檢')")
    fix = page.locator(".pf-row button:has-text('前往設為生效')")
    expect(fix).to_have_count(1)
    # v2 already has a shadow card, so G7 says where its evaluation stands (DEF-070)
    expect(page.locator(".pf-row:has(button:has-text('前往設為生效'))")).to_contain_text(
        "影子評估中：v2 已評分 0／3")
    fix.click()
    focused = page.locator(".pp-drawer .pp-ver-adopt.pp-focus")
    expect(focused).to_have_count(1)
    # v2 is the shadow (latest, none active) and has started: it is the recommendation
    v2 = page.locator(".pp-ver[data-version='2']")
    expect(v2.locator(".pp-ver-shadow")).to_contain_text("影子評估中 0／")
    expect(v2.locator(".pp-ver-adopt")).to_have_class(
        "btn btn-sm btn-primary pp-ver-adopt pp-focus")
    expect(page.locator(".pp-drawer")).to_contain_text("目前未設生效版")

    # --- 設為生效 on v1: confirm in the page's own dialog, then the 生效中 tag moves -------------
    v1 = page.locator(".pp-ver[data-version='1']")
    v1.locator(".pp-ver-adopt").click()
    dialog = page.locator(".modal")
    expect(dialog.locator(".modal-title")).to_have_text("設為生效 v1 — 個股健檢")
    expect(dialog).to_contain_text("已產生的卡片不變")
    with page.expect_response(lambda r: r.request.method == "PUT"
                              and "/active-calibration" in r.url) as resp:
        dialog.locator(".modal-foot .btn-primary").click()
    assert resp.value.ok
    v1 = page.locator(".pp-ver[data-version='1']")
    expect(v1.locator(".pill-ok")).to_have_text("生效中")
    expect(v1.locator(".pp-ver-cancel")).to_have_count(1)
    expect(page.locator(".pp-ver[data-version='2'] .pp-ver-adopt")).to_have_count(1)
    assert _active(page, base) == 1

    # --- 取消生效: back to no calibration layer ------------------------------------------------
    page.locator(".pp-ver[data-version='1'] .pp-ver-cancel").click()
    expect(page.locator(".modal .modal-title")).to_have_text("取消生效 v1 — 個股健檢")
    with page.expect_response(lambda r: r.request.method == "PUT"
                              and "/active-calibration" in r.url) as resp:
        page.locator(".modal .modal-foot .btn-primary").click()
    assert resp.value.ok
    expect(page.locator(".pp-ver .pill-ok", has_text="生效中")).to_have_count(0)
    assert _active(page, base) is None

    assert errors == []
