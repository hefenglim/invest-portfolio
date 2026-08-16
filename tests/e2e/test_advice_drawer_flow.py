"""E2E (Playwright, real server + real frontend): the W2 AI 建議 drawer section (AI-D1/D12).

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) opening the symbol-detail
drawer. The LLM is inactive on the flow server, so the two insight reads the section needs
are the REAL routes — no card exists until one is generated — while the generation POST is
``page.route``-stubbed (no LLM is configured, so the real run would 409/503).

The three states the section must reach honestly, because each is one the owner will actually
hit on the demo site before any real run:

  1. preset installed + no card yet  → an empty note + a working 立即產生 button
  2. preset installed + a card      → the card's title/summary render, and the button reads
                                       重新產生
  3. preset NOT installed           → a pointer to the pipeline hub, NOT a dead run button

…all with ZERO console/page errors (the smoke contract the drawer is held to).
"""

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory

_SYMBOL = "2330"
_ACCOUNT = "tw_broker"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _collect_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) in ("error", "warning") else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _seed_held(conn: sqlite3.Connection) -> None:
    """One held TW position, so the drawer renders the full (held) section stack."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(
        symbol=_SYMBOL, market=Market.TW, quote_ccy=Currency.TWD,
        sector="Tech", name="台積電",
    ))
    insert_transaction(conn, account_id=_ACCOUNT, symbol=_SYMBOL, side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("600"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=GOLDEN_NOW.date())
    upsert_prices(conn, [PriceRow(
        instrument=_SYMBOL, market=Market.TW, as_of=GOLDEN_NOW.date(),
        close=Decimal("600"), source="e2e",
    )], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [FxRow(
        base=Currency.TWD, quote=Currency.TWD, as_of=GOLDEN_NOW.date(),
        rate=Decimal("1"), source="e2e",
    )], fetched_at=GOLDEN_NOW)
    conn.commit()


def _open_drawer(page: Page, base: str) -> None:
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response("**/api/symbol/2330/detail") as resp_info:
        page.evaluate("() => window.pdOpenSymbol('2330')")
    assert resp_info.value.status == 200
    page.wait_for_selector(".sd-drawer .sd-signals")


def _install_advice_preset(page: Page, base: str) -> None:
    """Install ONLY the 「持倉建議與提點」 preset (not the whole pack) via the real route, so
    the section's task lookup has a real task to find. The pack endpoint is idempotent on
    preset_key; the other four presets simply get created alongside, which the section ignores.
    """
    resp = page.request.post(base + "/api/insight-tasks/official-pack")
    assert resp.ok, f"official-pack {resp.status}"


def _stub_advice_run(page: Page, *, card_title: str | None) -> None:
    """Stub the generation POST + the runs poll, and (when card_title) the card list.

    The LLM is inactive on the flow server, so the real /run would 409/503; the point of the
    e2e is the SECTION's render + wiring, not the generation. The run poll resolves ok so the
    section re-pulls the (now-stubbed) card.
    """
    page.route("**/api/insight-types/*/run",
               lambda r: r.fulfill(status=202, content_type="application/json",
                                   body=json.dumps({"run_id": 1, "insight_type_id": 1})))
    page.route("**/api/insight-types/*/runs*",
               lambda r: r.fulfill(status=200, content_type="application/json",
                                   body=json.dumps({"rows": [
                                       {"id": 1, "status": "ok", "finished_at": "x",
                                        "detail": "1 card", "reason": None}
                                   ]})))
    if card_title is not None:
        def _insights(route: Route) -> None:
            # The section joins a card to its task by ``c.insight_type_id in adviceIds``, and
            # adviceIds comes from the REAL (un-stubbed) /api/insight-types list. So the stub's
            # card must carry the REAL advice task's id — captured at install time into
            # _ADVICE_TASK_ID, since this stub cannot reach the server to look it up.
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "rows": [{
                    "id": 1, "insight_type_id": _ADVICE_TASK_ID[0], "symbol": _SYMBOL,
                    "is_shadow": False, "calibration_version": None,
                    "title": card_title, "summary": "偏多持有：趨勢與動能同向。",
                    "body_md": "...", "tags": [], "confidence": None, "prediction": None,
                    "horizon_days": 14, "due_at": None, "model": "mock",
                    "cost_usd": "0.001", "tokens_in": 100, "tokens_out": 50,
                    "created_at": GOLDEN_NOW.isoformat(),
                }],
                "total_count": 1, "limit": 25, "offset": 0,
            }))
        page.route("**/api/insights*", _insights)


#: The real 「持倉建議與提點」 task's id, captured when the pack installs it (the stub's card
#: must carry it so the section's preset_key join recognises the card as the advice card).
#: Module-scope, one-slot mutable: the only test that reads it installs the pack and fills it
#: first, and pytest runs these e2e serially, so the single slot cannot cross-contaminate.
_ADVICE_TASK_ID: list[int] = [0]


@pytest.mark.e2e
def test_advice_section_install_then_card_then_run(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_held)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    _install_advice_preset(page, base)
    # Capture the real advice task id so the stubbed card can claim it.
    tasks = page.request.get(base + "/api/insight-types").json()
    _ADVICE_TASK_ID[0] = next(t["id"] for t in tasks if t.get("preset_key") == "advice")

    # State 1 — installed, no card yet: empty note + 立即產生.
    _open_drawer(page, base)
    sec = page.locator(".sd-advice")
    expect(sec).to_contain_text("尚無此標的的建議卡")
    run_btn = sec.locator(".sd-advice-run")
    expect(run_btn).to_be_visible()
    expect(run_btn).to_be_enabled()

    # Generate (stubbed) → the run resolves ok → the section re-pulls and now a card exists.
    _stub_advice_run(page, card_title="2330 — 偏多持有")
    run_btn.click()
    expect(sec).to_contain_text("2330 — 偏多持有")
    expect(sec.locator(".sd-advice-body")).to_have_text("偏多持有：趨勢與動能同向。")
    expect(sec.locator(".sd-advice-run")).to_have_text("重新產生")

    assert not console_errors and not page_errors, (
        f"advice card flow: console={console_errors!r} page={page_errors!r}")


@pytest.mark.e2e
def test_advice_section_without_preset_points_at_pipeline_hub(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """No preset installed (fresh ledger): the section must point at the pipeline hub, not
    offer a 立即產生 button that would 409."""
    base = flow_server(_seed_held)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    _open_drawer(page, base)
    sec = page.locator(".sd-advice")
    expect(sec).to_contain_text("尚未建立「持倉建議與提點」任務")
    expect(sec.locator(".sd-advice-run")).to_have_count(0)
    expect(sec.locator(".sd-advice-link")).to_have_attribute("href", "pipeline-hub.html")

    assert not console_errors and not page_errors, (
        f"advice no-preset: console={console_errors!r} page={page_errors!r}")
