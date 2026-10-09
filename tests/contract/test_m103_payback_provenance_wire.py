"""M1-03 / D21 — the SPINOFF child's payback provenance reaches EVERY surface the ratio does.

D21's wording is "on the drawer and anywhere else the figure appears", and the figure appears
in six places: the holdings table, the 股利總覽 strip, the drawer's 部位摘要 (aggregate AND
per-account rows), the holdings CSV, and the LLM's ``symbol_detail_json`` variable — a
narrator that only sees ``payback_ratio`` will say 「KEMG 已回收 12%」 about a company that
never paid a cent. Three of those are the frontend reading ``/api/dashboard``; the other
three have their own wire and are pinned here one by one.

The seed reproduces the QA fixture's KEMB → KEMG carve-out (5,000 @ 2.40 all-in 12,023 ·
NET 1,500 · SPINOFF 1-for-5 at ``cost_carry`` 0.18), dated inside the frozen clock, so the
numbers here are the ones the audit measured: KEMG carries ``1500 × 0.18 = 270.00``.
"""

import csv
import json
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, init_golden_base

_PROVENANCE_KEYS = ("payback_from_symbol", "payback_carried_dividends", "payback_own_dividends")


def _seed_kemb_spinoff(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym, name in (("KEMB", "Kembang Bhd"), ("KEMG", "Kembang Green Bhd")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.MY, quote_ccy=Currency.MYR,
                                           sector="Utilities", name=name))
    insert_transaction(conn, account_id="moomoo_my", symbol="KEMB", side=Side.BUY,
                       quantity=Decimal("5000"), price=Decimal("2.40"),
                       fees=Decimal("11"), tax=Decimal("12"), trade_date=date(2026, 1, 15))
    insert_dividend(conn, account_id="moomoo_my", symbol="KEMB", div_date=date(2026, 3, 2),
                    div_type="NET", gross=Decimal("1500"), withholding=Decimal("0"),
                    net=Decimal("1500"))
    insert_corporate_action(conn, account_id="moomoo_my", action_date=date(2026, 4, 1),
                            kind=CorporateActionKind.SPINOFF, from_symbol="KEMB",
                            to_symbol="KEMG", ratio_to=Decimal("1"), ratio_from=Decimal("5"),
                            cost_carry=Decimal("0.18"))
    upsert_prices(conn, [
        PriceRow(instrument="KEMB", market=Market.MY, as_of=date(2026, 6, 9),
                 close=Decimal("2.10"), source="test"),
        PriceRow(instrument="KEMG", market=Market.MY, as_of=date(2026, 6, 9),
                 close=Decimal("1.80"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _rows(client: TestClient) -> dict[str, dict[str, object]]:
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    return {h["symbol"]: h for h in r.json()["holdings"]}


def _assert_child(row: dict[str, object]) -> None:
    assert row["payback_from_symbol"] == "KEMB"
    # Decimal STRINGS on the wire, exactly like every other money field of the row — and at
    # the replay's own exponent: 12,023.00 × 0.18 − 10,523.00 × 0.18 keeps four decimals,
    # the same shape the fixture's `dividend_portion` already had before this change.
    assert row["payback_carried_dividends"] == "270.0000"
    assert row["payback_own_dividends"] == "0"
    # The ratio itself is D21's untouched arithmetic: still the parent's, byte for byte.
    assert row["payback_ratio"] == "0.1247608749896032604175330616"
    assert row["dividend_portion"] == "270.0000"


def _assert_untouched(row: dict[str, object]) -> None:
    for key in _PROVENANCE_KEYS:
        assert row[key] is None, (row["symbol"], key, row[key])


def test_dashboard_holdings_carry_the_provenance_only_on_the_child(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    rows = _rows(dashboard_client_factory(_seed_kemb_spinoff))
    _assert_child(rows["KEMG"])
    # The parent is the SOURCE — identical ratio, no label.
    assert rows["KEMB"]["payback_ratio"] == rows["KEMG"]["payback_ratio"]
    _assert_untouched(rows["KEMB"])


def test_symbol_detail_aggregate_and_per_account_rows_carry_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The drawer's 部位摘要 reads ``detail.position`` (the cross-account aggregate) and the
    breakdown table reads ``position_accounts`` — both are server Decimal strings."""
    client = dashboard_client_factory(_seed_kemb_spinoff)
    r = client.get("/api/symbol/KEMG/detail")
    assert r.status_code == 200
    body = r.json()
    _assert_child(body["position"])
    (acct,) = body["position_accounts"]
    _assert_child(acct)
    parent = client.get("/api/symbol/KEMB/detail").json()
    _assert_untouched(parent["position"])
    _assert_untouched(parent["position_accounts"][0])


def test_holdings_csv_carries_the_three_columns_in_the_api_wire_form(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_kemb_spinoff)
    r = client.post("/api/export/holdings")
    assert r.status_code == 200
    text = r.content[3:].decode("utf-8")
    lines = [ln for ln in text.split("\r\n") if ln and not ln.startswith("#")]
    by_symbol = {row["symbol"]: row for row in csv.DictReader(lines)}
    child, parent = by_symbol["KEMG"], by_symbol["KEMB"]
    assert child["payback_from_symbol"] == "KEMB"
    assert child["payback_carried_dividends"] == "270.0000"
    assert child["payback_own_dividends"] == "0"
    for key in _PROVENANCE_KEYS:
        assert parent[key] == ""      # None -> empty cell, the CSV's existing convention


def test_llm_symbol_detail_variable_carries_it_beside_the_ratio() -> None:
    """llm-insight.md hard rule 3: the narrator may only believe what it is handed. Handing
    it ``payback_ratio`` alone hands it 「KEMG 已回收 12%」; the provenance rides alongside."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_kemb_spinoff(conn)
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    out, _used = V.render_prompt("{{symbol_detail_json}}", V.VarContext(data=data, symbol="KEMG"))
    value = json.loads(out)
    assert value["payback_from_symbol"] == "KEMB"
    assert Decimal(str(value["payback_carried_dividends"])) == Decimal("270.00")
    assert Decimal(str(value["payback_own_dividends"])) == Decimal("0")
    out, _used = V.render_prompt("{{symbol_detail_json}}", V.VarContext(data=data, symbol="KEMB"))
    parent = json.loads(out)
    for key in _PROVENANCE_KEYS:
        assert parent[key] is None
    conn.close()
