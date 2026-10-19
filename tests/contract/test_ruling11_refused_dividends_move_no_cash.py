"""⑪ / K2 (owner rulings 2026-10-05): a dividend the replay REFUSES moves no cash figure.

``build_book`` refuses a dividend in three cases — no position in that account by its effective
date (CR-02, ``Book.orphan_dividends``), an OPEN declared short (the short seller pays it), and a
ticker an EXCHANGE vacated (E24) — and lists every one in ``Book.refused_dividends``. 總報酬,
XIRR and 股利總覽 (R6) left them out. Every CASH figure still added them, because the cash pool
was built from the raw dividend ledger: an orphan CASH 18,200 TWD was excluded from 總報酬 and
still raised the TWD balance on 資金管理 and the net-worth trend — one payout, two answers — and
the withdraw / 換匯 guards let money out against a balance no other figure contained.

The oracle here is the ledger WITHOUT the refused row (the "control"): for every surface, the
ledger with the refused payout must read exactly what the control reads. Before the ruling each
surface read the control + 300. Three refusal classes, each through every surface family:

* the 資金管理 balances (``GET /api/cash``) and the manual-entry draft's 帳戶現金 line;
* the 現金收支明細 statement — the refused row is LISTED (a payout must not vanish from a list
  the owner reconciles) but marked 「未入帳（待釐清）」 and moves no running balance — and its
  reconciliation CSV and printed report, which say the same thing;
* the net-worth series, the trend's ``net_invested`` (K2), the index counterfactual's flows
  (K2) and B (K2 — withheld or equal, never counting the payout);
* the guards: the hard withdraw guard, the CSV import door (``data_ingestion`` judges the pool
  only through the injected ``cash_pool_fn``, so the BINDER filters), the ack-able movement
  delete, and the import-batch undo — one pool definition, so nothing can leave against money
  the page does not show;

plus the FX pool (a refused USD payout in an FX-exposed account) and the 換匯 hard guard, and a
batch undo whose removal makes a payout NEWLY refused (the "after" pool is the would-be
ledger's). The structural test at the end holds the set of callers of the pool arithmetic to
the reviewed seams that hand it the booked rows.
"""

from __future__ import annotations

import ast
import csv
import io
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.provenance import close_batch, open_batch, stamp_row
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, _seed_golden

D = Decimal
_TODAY = date(2026, 6, 11)          # GOLDEN_NOW's date
_NOTE = "未入帳（待釐清）"
_REFUSED_NET = "300"
Seed = Callable[[sqlite3.Connection], None]


# --- the ledgers ---------------------------------------------------------------------------


def _tw(conn: sqlite3.Connection, *symbols: str) -> None:
    for sym in symbols:
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=f"N{sym}", board="TWSE"))


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           day: date, *, short: bool = False, account: str = "tw_broker") -> int:
    return insert_transaction(conn, account_id=account, symbol=symbol, side=side,
                              quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                              trade_date=day, short_sale=short)


def _cash_div(conn: sqlite3.Connection, symbol: str, day: date, net: str,
              *, account: str = "tw_broker") -> None:
    insert_dividend(conn, account_id=account, symbol=symbol, div_date=day, div_type="CASH",
                    gross=D(net), withholding=D("0"), net=D(net))


def _prices(conn: sqlite3.Connection, **closes: str) -> None:
    upsert_prices(conn, [
        PriceRow(instrument=sym.lstrip("_"), market=Market.TW, as_of=date(2026, 6, 9),
                 close=D(close), source="test")
        for sym, close in closes.items()
    ], fetched_at=GOLDEN_NOW)


def _base(conn: sqlite3.Connection) -> None:
    """tw_broker TWD: 1,000,000 in, a 500,000 buy of 2330, a BOOKED 500 dividend on it."""
    seed_accounts(conn)
    _tw(conn, "2330", "2317", "2454", "2603", "2609")
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("1000000"))
    _trade(conn, "2330", Side.BUY, "1000", "500", date(2026, 1, 5))
    _cash_div(conn, "2330", date(2026, 3, 1), "500")
    _prices(conn, _2330="600")
    # The TW benchmark (0050), on both sides of every flow, so the counterfactual is computed.
    upsert_prices(conn, [
        PriceRow(instrument="0050", market=Market.TW, as_of=day, close=D(close),
                 source="test")
        for day, close in ((date(2025, 12, 31), "100"), (date(2026, 6, 9), "120"))
    ], fetched_at=GOLDEN_NOW)


def _orphan(conn: sqlite3.Connection, refused: bool) -> None:
    """2317 was never held in tw_broker — the payout has no position (CR-02)."""
    if refused:
        _cash_div(conn, "2317", date(2026, 3, 2), _REFUSED_NET)


def _open_short(conn: sqlite3.Connection, refused: bool) -> None:
    """2454 is an open DECLARED short when its payout lands (the short seller pays it)."""
    _trade(conn, "2454", Side.SELL, "100", "800", date(2026, 2, 1), short=True)
    _prices(conn, _2454="790")
    if refused:
        _cash_div(conn, "2454", date(2026, 3, 3), _REFUSED_NET)


def _vacated(conn: sqlite3.Connection, refused: bool) -> None:
    """2603 was EXCHANGED into 2609 before its payout (E24)."""
    _trade(conn, "2603", Side.BUY, "100", "50", date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2603",
                            to_symbol="2609", ratio_to=D("1"), ratio_from=D("1"))
    _prices(conn, _2609="55")
    if refused:
        _cash_div(conn, "2603", date(2026, 3, 4), _REFUSED_NET)


#: class → (its shape, the refused row's symbol, the control's tw_broker TWD balance today).
_CLASSES: dict[str, tuple[Callable[[sqlite3.Connection, bool], None], str, D]] = {
    "orphan": (_orphan, "2317", D("500500")),
    "open_short": (_open_short, "2454", D("580500")),      # + 100 × 800 short proceeds
    "vacated": (_vacated, "2603", D("495500")),            # − 100 × 50
}


def _pair(factory: DashboardClientFactory, cls: str,
          extra: Seed | None = None) -> tuple[TestClient, TestClient]:
    """(the ledger WITH the refused payout, the same ledger WITHOUT it)."""
    shape = _CLASSES[cls][0]

    def seed(refused: bool) -> Seed:
        def _seed(conn: sqlite3.Connection) -> None:
            _base(conn)
            shape(conn, refused)
            if extra is not None:
                extra(conn)
        return _seed

    return factory(seed(True)), factory(seed(False))


@pytest.fixture(params=sorted(_CLASSES))
def cls(request: pytest.FixtureRequest) -> str:
    name: str = request.param
    return name


def _json(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()


def _tw_pool(client: TestClient) -> str:
    body = _json(client.get("/api/cash", params={"limit": 500}))
    return str(next(b["amount"] for b in body["balances"]
                    if b["account_id"] == "tw_broker" and b["ccy"] == "TWD"))


# --- the 資金管理 page and the draft line --------------------------------------------------


def test_the_cash_page_balance_leaves_a_refused_payout_out(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    want = str(_CLASSES[cls][2])
    got, base = (_json(c.get("/api/cash", params={"limit": 500})) for c in (refused, control))
    assert _tw_pool(control) == want
    assert _tw_pool(refused) == want, f"{cls}: the pool counts the refused {_REFUSED_NET}"
    for key in ("balances", "negative_pools", "reporting_total", "reporting_total_excluded"):
        assert got[key] == base[key], (cls, key, got[key], base[key])


def test_the_draft_cash_line_quotes_the_same_pool(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    """The manual-entry draft's 帳戶現金 line (and the cash_after beside it) is the 資金 page's
    figure — so it leaves the refused payout out too."""
    refused, control = _pair(dashboard_client_factory, cls)
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
            "date": _TODAY.isoformat(), "shares": "1000", "price": "600"}
    got, base = (_json(c.post("/api/input/manual/preview", json=body))
                 for c in (refused, control))
    assert base["account_cash"] == {"ccy": "TWD", "balance": str(_CLASSES[cls][2])}
    assert got["account_cash"] == base["account_cash"], cls
    assert got["cash_after"] == base["cash_after"], cls


# --- the 現金收支明細 statement, its CSV and its printed report ------------------------------


def _statement(client: TestClient, ccy: str | None = "TWD") -> Any:
    params: dict[str, Any] = {"account": "tw_broker", "limit": 500}
    if ccy is not None:
        params["ccy"] = ccy
    return _json(client.get("/api/cash/statement", params=params))


def _booked_rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(r["date"], r["ccy"], r["kind"], r["ref"], r["delta"], r["balance"])
            for r in rows if r["booked"]]


def test_the_statement_lists_a_refused_payout_and_moves_no_balance(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    _shape, symbol, want = _CLASSES[cls]
    got, base = _statement(refused), _statement(control)
    # 目前餘額 is the control's — the payout counts in no balance …
    assert base["current_balance"] == str(want)
    assert got["current_balance"] == base["current_balance"], cls
    assert got["balances"] == base["balances"], cls
    # … but it is LISTED, marked, with the amount it records.
    marked = [r for r in got["rows"] if r["booked"] is False]
    assert len(marked) == 1, (cls, got["rows"])
    (row,) = marked
    assert (row["kind"], row["symbol"], row["delta"]) == ("dividend", symbol, _REFUSED_NET)
    assert row["unbooked_note"] == _NOTE and row["unbooked_reason"], row
    assert got["unbooked_count"] == 1 and base["unbooked_count"] == 0
    assert got["total_count"] == base["total_count"] + 1
    # Its running balance is the one before it (newest-first: the next row down) …
    i = got["rows"].index(row)
    assert row["balance"] == got["rows"][i + 1]["balance"], (cls, got["rows"])
    # … and every other row reads exactly what the control reads.
    assert _booked_rows(got["rows"]) == _booked_rows(base["rows"]), cls
    assert all(r["unbooked_note"] is None for r in base["rows"])
    # The account-wide (all-currency) view says the same.
    got_all, base_all = _statement(refused, None), _statement(control, None)
    assert got_all["balances"] == base_all["balances"], cls
    assert got_all["unbooked_count"] == 1
    assert _booked_rows(got_all["rows"]) == _booked_rows(base_all["rows"]), cls


def _csv(client: TestClient) -> list[list[str]]:
    r = client.post("/api/export/cash-statement", json={"account": "tw_broker", "ccy": "TWD"})
    assert r.status_code == 200, r.text
    return [row for row in csv.reader(io.StringIO(r.content.decode("utf-8-sig")))
            if row and not row[0].startswith("#")]


def test_the_reconciliation_csv_marks_the_refused_payout(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    got, base = _csv(refused), _csv(control)
    head = got[0]
    assert head == base[0]
    # ``unbooked`` explains the balance beside it; ``counts_from`` stays the last column.
    assert head[-2:] == ["unbooked", "counts_from"], head
    col = {name: i for i, name in enumerate(head)}
    marked = [r for r in got[1:] if r[col["unbooked"]]]
    assert len(marked) == 1 and marked[0][col["unbooked"]] == _NOTE, (cls, got)
    (row,) = marked
    assert row[col["kind"]] == "dividend" and row[col["delta"]] == _REFUSED_NET
    # Chronological: the refused row's balance is the previous row's.
    i = got.index(row)
    assert row[col["balance"]] == got[i - 1][col["balance"]], (cls, got)
    unmarked = [r for r in got[1:] if not r[col["unbooked"]]]
    assert unmarked == base[1:], cls
    assert unmarked[-1][col["balance"]] == str(_CLASSES[cls][2])


_TITLE = re.compile(r"<h2>TWD 資金池 · 目前餘額 (?P<amount>[-0-9,.]+) TWD(?P<rest>[^<]*)</h2>")


def _printed(client: TestClient) -> str:
    r = client.post("/api/export/cash-statement-report",
                    json={"account": "tw_broker", "ccy": "TWD"})
    assert r.status_code == 200, r.text
    return str(r.content.decode("utf-8"))


def test_the_printed_statement_marks_the_refused_payout(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    got, base = _printed(refused), _printed(control)
    g, b = _TITLE.search(got), _TITLE.search(base)
    assert g is not None and b is not None
    assert g.group("amount") == b.group("amount") == f"{_CLASSES[cls][2]:,}", (cls, g, b)
    assert "另有 1 筆股利未入帳，不計入餘額" in g.group("rest")
    assert f"・{_NOTE}" in got and '<tr class="unbooked">' in got
    assert _NOTE not in base and "unbooked" not in base


def test_the_tax_package_never_declares_a_refused_payout(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    """Found by the ⑪ scan, already right, pinned here: the tax package replays STRICTLY, so a
    refused payout refuses the package (422 naming the row) rather than reaching the dividends
    sheet as income. The control's package is built."""
    refused, control = _pair(dashboard_client_factory, cls)
    r = refused.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 422, (cls, r.status_code)
    assert r.json()["error"]["code"] == "unbookable_ledger"
    assert _CLASSES[cls][1] in r.json()["error"]["message"]
    assert control.post("/api/export/tax-package", json={"year": 2026}).status_code == 200


# --- the dashboard: net worth, the flow series (K2), the counterfactual, B ----------------


def test_net_worth_and_the_flow_series_leave_a_refused_payout_out(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    got, base = (_json(c.get("/api/dashboard")) for c in (refused, control))
    gp, bp = got["trend"]["points"], base["trend"]["points"]
    assert [p["date"] for p in gp] == [p["date"] for p in bp] and bp, cls

    def cash(p: dict[str, Any]) -> D | None:
        # net_worth = total_value + that day's cash: the cash half, isolated from valuation
        # (a refused payout also flags its position 待釐清, which moves total_value).
        return None if p["net_worth"] is None else D(p["net_worth"]) - D(p["total_value"])

    assert [cash(p) for p in gp] == [cash(p) for p in bp], cls
    # K2: the money put in is the same money — the payout is no flow.
    assert [p["net_invested"] for p in gp] == [p["net_invested"] for p in bp], cls
    # …and the index counterfactual spends the same stream.
    bench, bench0 = got["benchmark"], base["benchmark"]
    assert bench0["available"] is True, bench0
    for key in ("available", "net_invested", "terminal_value", "benchmark_return"):
        assert bench[key] == bench0[key], (cls, key, bench[key], bench0[key])
    # B is withheld (a 待釐清 day) or the control's — never B + the payout.
    b, b0 = got["kpis"]["total_return_fx_complete"], base["kpis"]["total_return_fx_complete"]
    assert b is None or b == b0, (cls, b, b0)


def _covered_short(conn: sqlite3.Connection, refused: bool) -> None:
    """K2's first edge: the payout lands on an open short, which is COVERED before today —
    the position is flat, so no day is marked incomplete and B is computed."""
    _base(conn)
    _trade(conn, "2454", Side.SELL, "100", "800", date(2026, 2, 1), short=True)
    if refused:
        _cash_div(conn, "2454", date(2026, 3, 3), _REFUSED_NET)
    _trade(conn, "2454", Side.BUY, "100", "780", date(2026, 4, 1))


def _vacated_successor_flat(conn: sqlite3.Connection, refused: bool) -> None:
    """K2's second edge: the payout lands on a ticker an EXCHANGE vacated, whose successor was
    sold out before it — nothing is held to flag."""
    _base(conn)
    _trade(conn, "2603", Side.BUY, "100", "50", date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2603",
                            to_symbol="2609", ratio_to=D("1"), ratio_from=D("1"))
    _trade(conn, "2609", Side.SELL, "100", "55", date(2026, 2, 15))
    if refused:
        _cash_div(conn, "2603", date(2026, 3, 4), _REFUSED_NET)


@pytest.mark.parametrize("shape", [_covered_short, _vacated_successor_flat],
                         ids=["covered_short", "vacated_successor_flat"])
def test_b_never_counts_a_refused_payout(
    dashboard_client_factory: DashboardClientFactory,
    shape: Callable[[sqlite3.Connection, bool], None],
) -> None:
    """B = total_value − net_invested with every flow at its own date's FX. Before K2 it was
    withheld only on an incomplete last day; these two ledgers leave the day complete, so B
    counted the payout as money taken out."""
    refused = dashboard_client_factory(lambda c: shape(c, True))
    control = dashboard_client_factory(lambda c: shape(c, False))
    got, base = (_json(c.get("/api/dashboard")) for c in (refused, control))
    b0 = base["kpis"]["total_return_fx_complete"]
    assert b0 is not None, base["kpis"]
    b = got["kpis"]["total_return_fx_complete"]
    # A book-level 待釐清 flag may withhold B on these shapes (a parallel fix adds one for the
    # second): None is an honest answer; B + 300 — what both read before K2 — is not.
    assert b is None or b == b0, (b, b0)
    assert got["trend"]["points"][-1]["net_invested"] == base["trend"]["points"][-1][
        "net_invested"]


# --- the guards: one pool definition ------------------------------------------------------


def test_a_withdrawal_cannot_spend_a_refused_payout(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    """The hard withdraw guard reads the pool the page shows (FU-D43a — no ack, no
    overdraft): 100 more than the page's balance is refused, the balance itself is not."""
    refused, _control = _pair(dashboard_client_factory, cls)
    pool = _CLASSES[cls][2]
    body = {"account_id": "tw_broker", "date": _TODAY.isoformat(), "kind": "withdraw",
            "ccy": "TWD"}
    over = refused.post("/api/cash/movements", json={**body, "amount": str(pool + 100)})
    assert over.status_code == 422, (cls, over.text)
    assert over.json()["error"]["code"] == "withdraw_insufficient_balance"
    assert refused.post("/api/cash/movements",
                        json={**body, "amount": str(pool)}).status_code == 201


def test_the_csv_door_judges_the_same_pool(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    """``data_ingestion`` sees the pool only through the injected ``cash_pool_fn`` — the binder
    filters, so the bulk door refuses what the form refuses, with the form's sentence."""
    refused, _control = _pair(dashboard_client_factory, cls)
    amount = str(_CLASSES[cls][2] + 100)
    manual = refused.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": _TODAY.isoformat(), "kind": "withdraw",
        "ccy": "TWD", "amount": amount})
    assert manual.status_code == 422, (cls, manual.text)
    preview = _json(refused.post("/api/import/preview", json={
        "kind": "cash", "csv_text": "account,date,kind,ccy,amount\n"
                                    f"tw_broker,{_TODAY.isoformat()},WITHDRAW,TWD,{amount}\n"}))
    assert preview["summary"]["error"] == 1, (cls, preview)
    assert preview["rows"][0]["reason"] == manual.json()["error"]["message"]


def _late_withdrawal(pool: D) -> Seed:
    """A 1,000 deposit on 06-01 and a hand withdrawal of pool + 100 on 06-10: the pool ends at
    900. Without the deposit it is −100 — or +200 if a refused payout of 300 counted."""
    def seed(conn: sqlite3.Connection) -> None:
        insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 6, 1),
                             kind="DEPOSIT", ccy=Currency.TWD, amount=D("1000"),
                             note="late deposit")
        insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 6, 10),
                             kind="WITHDRAW", ccy=Currency.TWD, amount=pool + 100)
    return seed


def test_deleting_a_deposit_asks_when_only_a_refused_payout_covered_it(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls, _late_withdrawal(_CLASSES[cls][2]))
    assert _tw_pool(control) == "900"
    for client in (refused, control):
        moves = _json(client.get("/api/cash", params={"limit": 500}))["movements"]["rows"]
        (dep,) = [m for m in moves if m["note"] == "late deposit"]
        r = client.delete(f"/api/cash/movements/{dep['id']}")
        assert r.status_code == 422, (cls, r.text)
        assert r.json()["error"]["code"] == "negative_cash"
    assert _tw_pool(refused) == _tw_pool(control)


def test_the_batch_undo_asks_when_only_a_refused_payout_covered_it(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    pool = _CLASSES[cls][2]
    refused, _control = _pair(dashboard_client_factory, cls)
    r = refused.post("/api/import/commit", json={
        "kind": "cash", "csv_text": "account,date,kind,ccy,amount\n"
                                    "tw_broker,2026-06-01,DEPOSIT,TWD,1000\n",
        "ack_warnings": True, "source_name": "t.csv"})
    assert r.status_code == 200, r.text
    batch = r.json()["import_batch_id"]
    w = refused.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-10", "kind": "withdraw", "ccy": "TWD",
        "amount": str(pool + 100)})
    assert w.status_code == 201, w.text
    undo = refused.delete(f"/api/import/batches/{batch}")
    assert undo.status_code == 422, (cls, undo.text)
    assert undo.json()["error"]["code"] == "negative_cash"


def _newly_refused_by_the_undo(conn: sqlite3.Connection) -> int:
    """The batch holds a COVER and a deposit. Undoing it leaves a later payout on an OPEN short
    (newly refused) and takes 500 out: the "after" pool is the would-be ledger's."""
    seed_accounts(conn)
    _tw(conn, "2454")
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("1000"))
    _trade(conn, "2454", Side.SELL, "1", "1", date(2026, 2, 1), short=True)
    batch = open_batch(conn, kind="transactions", csv_text="cover + deposit")
    cover = _trade(conn, "2454", Side.BUY, "1", "1", date(2026, 2, 10))
    stamp_row(conn, kind="transactions", row_id=cover, batch_id=batch, source_hash="a")
    dep = insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 2, 10),
                               kind="DEPOSIT", ccy=Currency.TWD, amount=D("500"))
    stamp_row(conn, kind="cash", row_id=dep, batch_id=batch, source_hash="b")
    close_batch(conn, batch, row_count=2)
    # Flat after the cover: the payout books (post-close income, H2) …
    _cash_div(conn, "2454", date(2026, 3, 3), _REFUSED_NET)
    # … and a later withdrawal spends it: 1,800 − 1,200 = 600.
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 4, 1),
                         kind="WITHDRAW", ccy=Currency.TWD, amount=D("1200"))
    _prices(conn, _2454="1")
    return batch


def test_the_batch_undo_judges_the_pool_the_would_be_ledger_books(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Without the cover the payout lands on an open short: the replay refuses it, so after the
    undo the pool reads 1,001 − 1,200 = −199 on 04-01 and the undo must ask. Read with the
    CURRENT ledger's refusals (the payout booked) it would read +101 and pass in silence."""
    batches: list[int] = []
    client = dashboard_client_factory(lambda c: batches.append(_newly_refused_by_the_undo(c)))
    assert _tw_pool(client) == "600"
    undo = client.delete(f"/api/import/batches/{batches[0]}")
    assert undo.status_code == 422, undo.text
    assert undo.json()["error"]["code"] == "negative_cash"
    assert "−199" in undo.json()["error"]["message"], undo.json()


# --- the FX pool and the 換匯 guard --------------------------------------------------------


def _golden_with_a_foreign_orphan(refused: bool) -> Seed:
    """The golden ledger (schwab: 32,000 TWD → 1,000 USD at 32, AAPL 10 × 100 — USD pool 0)
    plus a 30 USD payout on MSFT, which schwab never held."""
    def seed(conn: sqlite3.Connection) -> None:
        _seed_golden(conn)
        upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name="Microsoft"))
        if refused:
            _cash_div(conn, "MSFT", date(2026, 3, 5), "30", account="schwab")
    return seed


def test_the_fx_pool_and_the_fx_guard_leave_a_refused_foreign_payout_out(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    refused = dashboard_client_factory(_golden_with_a_foreign_orphan(True))
    control = dashboard_client_factory(_golden_with_a_foreign_orphan(False))
    got, base = (_json(c.get("/api/dashboard")) for c in (refused, control))
    # The FX pool's foreign cash is the funds view's (manual §8.3 fx.pool_equals_funds) — the
    # 30 USD would have been marked to spot as unrealized FX on money no balance holds.
    assert got["fx"] == base["fx"]
    assert got["kpis"]["fx_unrealized"] == base["kpis"]["fx_unrealized"]
    pools = [_json(c.get("/api/cash"))["balances"] for c in (refused, control)]
    assert pools[0] == pools[1]
    # 換匯 may never overdraft (FU-D34): the USD pool is 0, whatever the refused row says.
    r = refused.post("/api/cash/fx", json={
        "account_id": "schwab", "date": _TODAY.isoformat(), "from_ccy": "USD",
        "from_amt": "20", "to_ccy": "TWD", "to_amt": "660"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "fx_insufficient_balance"


# --- structure: every reader of the pool arithmetic is a reviewed seam -------------------

_PKG = Path(__file__).resolve().parents[2] / "portfolio_dash"

#: Who calls the pool arithmetic, and where its dividends come from. Each one hands it the
#: BOOKED rows (⑪), and none re-derives a refusal:
#:   * api/routers/cash.py — ``load_cash_ledgers`` splits the stored rows once
#:     (``returns.booked_dividends`` over ``replay_refusals`` or a guard's own replay);
#:     ``pool_balances``, ``bind_cash_pool`` (and its ``probe``), ``_pool_dip``,
#:     ``cash_statement`` all read that snapshot;
#:   * portfolio/dashboard.py — its own book's ``refused_dividends``;
#:   * portfolio/networth.py — the dashboard's booked rows, passed through;
#:   * portfolio/cash.py — ``account_statement`` over its caller's rows;
#:   * export/cash_statement.py — ``_load`` splits by ``replay_refusals``.
#: A NEW caller fails here until it is reviewed and added — the pool must not grow a reader
#: that counts a payout the replay refused.
_POOL_READERS = {
    "cash_balances": {
        "api/routers/cash.py::bind_cash_pool", "api/routers/cash.py::pool_balances",
        "api/routers/cash.py::probe", "portfolio/dashboard.py::_build_dashboard",
        "portfolio/networth.py::daily_cash_series",
        # the settlement (PERF 2026-10-18): month-end cash and the stored net-worth series,
        # both fed `booked_dividends(…, book.refused_dividends)` — the dashboard's rows
        "portfolio/period_close.py::cash_at", "portfolio/settle_worker.py::_valuate",
    },
    "pool_lines": {
        "api/routers/cash.py::_pool_dip", "api/routers/cash.py::bind_cash_pool",
        "api/routers/cash.py::probe", "portfolio/cash.py::account_statement",
        "portfolio/networth.py::daily_cash_series",
    },
    "account_statement": {
        "api/routers/cash.py::cash_statement",
        "export/cash_statement.py::build_cash_statement_csv",
        "export/cash_statement.py::build_cash_statement_report_html",
    },
    "daily_cash_series": {"portfolio/dashboard.py::_build_dashboard",
                          "portfolio/settle_worker.py::_valuate"},
    "compute_fx_summary": {"portfolio/dashboard.py::_build_dashboard"},
    "build_reporting_flows": {
        "portfolio/dashboard.py::_benchmark_comparison",
        "portfolio/timeseries.py::daily_value_series",
        # the settlement's trend flows and its search for the first unconvertible day, each
        # passed the replay's `refused_dividends`
        "portfolio/settle_worker.py::_valuate",
        "portfolio/settle_worker.py::_first_unconvertible_day",
        "portfolio/settle_worker.py::bad",
    },
}


def _callers_of(name: str) -> set[str]:
    out: set[str] = set()
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == name):
                    out.add(f"{path.relative_to(_PKG).as_posix()}::{fn.name}")
                    break
    return out


@pytest.mark.parametrize("name", sorted(_POOL_READERS))
def test_every_reader_of_the_pool_arithmetic_is_a_reviewed_seam(name: str) -> None:
    assert _callers_of(name) == _POOL_READERS[name], name
