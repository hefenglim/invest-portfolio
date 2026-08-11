"""Contract tests for GET /api/symbol/{symbol}/detail (spec 01).

Read-only: price_history comes from STORED prices (no live backfill). cost_basis binds
to the account holding the most shares (Q1); null for a non-held / watchlist symbol.
"""

import sqlite3
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, _seed_dual_account


def test_symbol_detail_held_symbol_full_shape(api_client: TestClient) -> None:
    r = api_client.get("/api/symbol/2330/detail")
    assert r.status_code == 200
    body = r.json()

    assert body["symbol"] == "2330"
    # as_of is the frozen clock's date.
    assert body["as_of"] == "2026-06-11"

    # Registry enrichment (FU-D24): name + market for the drawer title.
    assert body["name"] == "TSMC"
    assert body["market"] == "TW"

    # cost_basis -> Q1 most-shares account; money as Decimal strings.
    cb = body["cost_basis"]
    assert cb["account_id"] == "tw_broker"
    assert cb["original_avg"] == "500"
    assert cb["adjusted_avg"] == "495"  # 5000 cash div reduced adj cost over 1000 sh

    # price_history from stored prices (read-only), latest stored date 2026-06-09.
    ph = body["price_history"]
    assert ph["available"] is True
    assert ph["points"]  # non-empty
    assert ph["last_date"] == "2026-06-09"
    assert ph["partial"] is False
    assert all(isinstance(p["close"], str) for p in ph["points"])

    # dividend_events: the 2026-03-01 cash div, lowercase type, UPPER ccy.
    cash = [d for d in body["dividend_events"] if d["type"] == "cash"]
    assert len(cash) == 1
    assert cash[0]["net"] == "5000"
    assert cash[0]["ccy"] == "TWD"

    # trade_events: the buy, lowercase side, money as strings.
    buys = [t for t in body["trade_events"] if t["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["shares"] == "1000"
    assert buys[0]["price"] == "500"

    # realized_rows filtered to this symbol (no sells -> empty).
    assert body["realized_rows"] == []


def test_symbol_detail_us_account_resolution(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/AAPL/detail").json()
    assert body["cost_basis"]["account_id"] == "schwab"
    assert body["cost_basis"]["original_avg"] == "100"


def test_symbol_detail_non_held_symbol_null_cost_basis(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # A registered watchlist instrument with no holdings/transactions.
    upsert_instrument(
        golden_db,
        Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                   sector="Tech", name="NVIDIA"),
    )
    golden_db.commit()

    body = api_client.get("/api/symbol/NVDA/detail").json()
    assert body["symbol"] == "NVDA"
    # Registry enrichment is present for a registered-but-unheld watchlist symbol.
    assert body["name"] == "NVIDIA"
    assert body["market"] == "US"
    assert body["cost_basis"] is None
    # No stored prices for NVDA -> price_history unavailable, with a note.
    assert body["price_history"]["available"] is False
    assert body["price_history"]["note"] is not None
    assert body["dividend_events"] == []
    assert body["trade_events"] == []
    assert body["realized_rows"] == []


def test_symbol_detail_days_window(api_client: TestClient) -> None:
    # days controls the lower bound of the stored-price window; the 2026-06-09 point
    # is within 180 days of 2026-06-11 but outside a 1-day window.
    far = api_client.get("/api/symbol/2330/detail?days=1").json()
    assert far["price_history"]["available"] is False

    near = api_client.get("/api/symbol/2330/detail?days=180").json()
    assert near["price_history"]["available"] is True
    assert any(p["date"] == "2026-06-09" for p in near["price_history"]["points"])


def test_symbol_detail_unregistered_symbol_null_name_market(api_client: TestClient) -> None:
    # A symbol with no instrument row: name/market degrade to null (FU-D24).
    body = api_client.get("/api/symbol/ZZZZ/detail").json()
    assert body["symbol"] == "ZZZZ"
    assert body["name"] is None
    assert body["market"] is None
    assert body["cost_basis"] is None
    assert body["price_history"]["available"] is False


def test_symbol_detail_money_and_date_field(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/2330/detail").json()
    # as_of is a plain date string (not a datetime).
    assert date.fromisoformat(body["as_of"]) == date(2026, 6, 11)
    # adjusted_avg parses as a Decimal.
    assert Decimal(body["cost_basis"]["adjusted_avg"]) == Decimal("495")


# --- round-8.1 Wave A: unified activity + reconciliation (owner #2a) ----------------

def _seed_full_activity(conn: sqlite3.Connection) -> None:
    """A single-account symbol exercising EVERY share-affecting event kind (schwab / AAPL):

      opening 5 sh (cost 400) + buy 10 @ 100 + sell 3 @ 120 + DRIP reinvest 0.5 sh @ 130
      → book shares 5 + 10 − 3 + 0.5 = 12.5, so 交易明細 must show 4 event kinds and the
        reconciliation footer must balance (net == book).
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("5"),
                   original_cost_total=Decimal("400"), build_date=date(2026, 1, 1))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("3"), price=Decimal("120"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 3, 1),
                    div_type="DRIP", gross=Decimal("65"), withholding=Decimal("0"),
                    net=Decimal("65"), reinvest_shares=Decimal("0.5"),
                    reinvest_price=Decimal("130"))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def test_symbol_detail_activity_all_event_kinds_present(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_full_activity)
    body = client.get("/api/symbol/AAPL/detail").json()

    activity = body["activity"]
    sides = [a["side"] for a in activity]
    # opening, buy, sell, and the DRIP reinvest row are ALL present (owner #2a).
    assert set(sides) == {"open", "buy", "sell", "drip"}
    # chronological (open 01-01 → buy 01-05 → sell 02-01 → drip 03-01).
    assert [a["date"] for a in activity] == [
        "2026-01-01", "2026-01-05", "2026-02-01", "2026-03-01"]
    # every row is account-tagged with id + display name.
    assert all(a["account_id"] == "schwab" and a["account"] for a in activity)
    # opening carries no fee/tax and uses original_avg (400/5 = 80) as its price.
    opening = next(a for a in activity if a["side"] == "open")
    assert opening["price"] == "80" and opening["fee"] is None and opening["tax"] is None
    # the DRIP reinvest row carries its reinvest price and zero cash total.
    drip = next(a for a in activity if a["side"] == "drip")
    assert drip["shares"] == "0.5" and drip["price"] == "130" and Decimal(drip["total"]) == 0
    # signed cash total: buy −(10×100) = −1000; sell +(3×120) = +360.
    buy = next(a for a in activity if a["side"] == "buy")
    sell = next(a for a in activity if a["side"] == "sell")
    assert Decimal(buy["total"]) == Decimal("-1000")
    assert Decimal(sell["total"]) == Decimal("360")


def test_symbol_detail_activity_reconciles_with_position(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_full_activity)
    body = client.get("/api/symbol/AAPL/detail").json()

    rec = body["activity_reconcile"]["total"]
    assert rec["opening_shares"] == "5"
    assert rec["buy_shares"] == "10"
    assert rec["sell_shares"] == "3"
    assert rec["reinvest_shares"] == "0.5"
    # 5 + 10 − 3 + 0.5 = 12.5 = the book (部位摘要) shares → the identity balances.
    assert Decimal(rec["net_shares"]) == Decimal("12.5")
    assert Decimal(rec["book_shares"]) == Decimal("12.5")
    assert rec["balances"] is True
    # the aggregate 部位摘要 share count matches the reconciliation's book figure.
    assert Decimal(body["position"]["shares"]) == Decimal(rec["book_shares"])
    # single account → per-account reconcile mirrors the total.
    assert body["activity_reconcile"]["by_account"]["schwab"]["balances"] is True


# --- round-8.1 Wave A: cross-account aggregate position (owner #2c) ------------------

def test_symbol_detail_position_multi_account_aggregate(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """AAPL held in schwab (30 @100) + moomoo_my (10 @110): 部位摘要 is the AGGREGATE, and
    the aggregate money equals the sum of the per-account rows (server-computed Decimal)."""
    client = dashboard_client_factory(_seed_dual_account)
    body = client.get("/api/symbol/AAPL/detail").json()

    pos = body["position"]
    accts = body["position_accounts"]
    assert pos["account_count"] == 2
    assert len(accts) == 2
    # aggregate shares = 30 + 10.
    assert Decimal(pos["shares"]) == Decimal("40")
    # blended original average = (3000 + 1100) / 40 = 102.5 (shares-weighted, on read).
    assert Decimal(pos["original_avg"]) == Decimal("102.5")
    # AGGREGATE == Σ per-account, proven for every money field (no JS could do this safely).
    for field in ("market_value", "unrealized_pnl", "capital_gain",
                  "original_cost_total", "adjusted_cost_total"):
        agg = Decimal(pos[field])
        per = sum(Decimal(a[field]) for a in accts)
        assert agg == per, f"{field}: aggregate {agg} != Σ per-account {per}"
    # concrete: 30×120 + 10×120 = 4800 market value; (120−100)×30 + (120−110)×10 = 700 unreal.
    assert Decimal(pos["market_value"]) == Decimal("4800")
    assert Decimal(pos["unrealized_pnl"]) == Decimal("700")
    # weight aggregate is the Σ of per-account weights (reporting-currency ratio, server-side).
    if pos["weight"] is not None:
        assert Decimal(pos["weight"]) == sum(Decimal(a["weight"]) for a in accts)
    # cost_basis still binds to the most-shares account (schwab, 30 > 10).
    assert body["cost_basis"]["account_id"] == "schwab"


def test_symbol_detail_position_single_account(api_client: TestClient) -> None:
    """A single-account symbol: 部位摘要 aggregate == that one account (unchanged behaviour)."""
    body = api_client.get("/api/symbol/2330/detail").json()
    pos = body["position"]
    assert pos["account_count"] == 1
    assert Decimal(pos["shares"]) == Decimal("1000")
    assert Decimal(pos["original_avg"]) == Decimal("500")
    assert Decimal(pos["adjusted_avg"]) == Decimal("495")  # dividend-adjusted
    assert Decimal(pos["market_value"]) == Decimal("600000")  # 600 × 1000
    assert len(body["position_accounts"]) == 1


# --- share-reconciliation tolerance (owner ruling 2026-08-06) ------------------------
#
# The three reinvest_shares below are the EXACT values measured on the demo site's AAPL on
# 2026-08-05, where the drawer footer rendered "期初 0 ＋買 95 −賣 0 ＋配股/DRIP 0 ＝ 部位摘要
# 95 股 ⚠ 對帳不一致" — a visually perfect equation next to a red flag. They are `net / price`
# quotients that do not terminate, so each carries ~28 significant digits.
_DRIP_SCHWAB_FEB = Decimal("0.01988201878960017478697837011")
_DRIP_MOOMOO_MAY = Decimal("0.006457564575645756457564575646")
_DRIP_SCHWAB_MAY = Decimal("0.01937269372693726937269372694")


def _seed_drip_association_noise(conn: sqlite3.Connection) -> None:
    """AAPL bought in two accounts, then three DRIP reinvests of non-terminating share counts.

    `_reconcile` sums the three tiny quotients together FIRST and adds 95 once; `build_book`
    folds each one into a ~60-share running position. Decimal at the default 28-digit context
    is not associative across that magnitude gap, so the two orders disagree in the 26th
    decimal place — on a ledger that is entirely consistent.
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("60"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("35"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    for acct, when, shares in (("schwab", date(2026, 2, 9), _DRIP_SCHWAB_FEB),
                               ("moomoo_my", date(2026, 5, 11), _DRIP_MOOMOO_MAY),
                               ("schwab", date(2026, 5, 11), _DRIP_SCHWAB_MAY)):
        insert_dividend(conn, account_id=acct, symbol="AAPL", div_date=when,
                        div_type="DRIP", gross=Decimal("3"), withholding=Decimal("0.9"),
                        net=Decimal("2.1"), reinvest_shares=shares,
                        reinvest_price=Decimal("105.63"))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def test_reconcile_tolerates_decimal_association_noise(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A consistent ledger must NOT be flagged 對帳不一致 over sub-1e-6 share arithmetic."""
    client = dashboard_client_factory(_seed_drip_association_noise)
    rec = client.get("/api/symbol/AAPL/detail").json()["activity_reconcile"]["total"]

    gap = Decimal(rec["net_shares"]) - Decimal(rec["book_shares"])
    # DETECTION POWER: assert the fixture actually exercises the tolerance. If the two sides
    # ever agree exactly, this test would pass trivially under `==` and prove nothing.
    assert gap != 0, "fixture no longer reproduces the association-order gap"
    assert abs(gap) < Decimal("0.000001")
    # ... and that the gap is exactly what the wire reports, so the drawer can name it.
    assert Decimal(rec["diff_shares"]) == gap
    assert rec["balances"] is True
    assert Decimal(rec["buy_shares"]) == Decimal("95")
    # Per-account footers get the same treatment (the drawer's account filter uses them).
    for per in client.get("/api/symbol/AAPL/detail").json()[
            "activity_reconcile"]["by_account"].values():
        assert per["balances"] is True


def _seed_unregistered_symbol_break(conn: sqlite3.Connection) -> None:
    """A ledger row whose symbol has no Instrument: build_dashboard excludes it from the book
    (dashboard.py 1b) while the raw-ledger activity list still shows it — a REAL 95-share
    reconciliation break the flag must keep catching."""
    seed_accounts(conn)
    insert_transaction(conn, account_id="schwab", symbol="ZZTOP", side=Side.BUY,
                       quantity=Decimal("95"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    conn.commit()


def test_reconcile_tolerance_does_not_mask_a_real_break(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The 1e-6 tolerance absorbs arithmetic noise ONLY — a genuine gap still flags."""
    client = dashboard_client_factory(_seed_unregistered_symbol_break)
    rec = client.get("/api/symbol/ZZTOP/detail").json()["activity_reconcile"]["total"]

    assert Decimal(rec["buy_shares"]) == Decimal("95")
    assert Decimal(rec["book_shares"]) == Decimal("0")
    assert rec["balances"] is False
    assert Decimal(rec["diff_shares"]) == Decimal("95")


def test_symbol_detail_position_null_for_unheld(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/ZZZZ/detail").json()
    assert body["position"] is None
    assert body["position_accounts"] == []
    assert body["activity"] == []
    # an empty ledger still reconciles (0 == 0).
    assert body["activity_reconcile"]["total"]["balances"] is True


# --- 待釐清: a SKIPPED corporate action reaches the wire (audit F-05) -----------------


def _seed_unbookable_action(conn: sqlite3.Connection) -> None:
    """Flag 2330 (tw_broker, 1,000 sh) with ``unbookable_action`` via rule E18.

    An EXCHANGE whose DESTINATION holds an open declared short has no honest booking, so
    ``build_book`` skips it on the dashboard path and flags the SOURCE. 2330 therefore stays
    an ordinary, priced long position that happens to carry a share count nobody can trust —
    which is precisely the state the wire has to be able to express. Written through the
    store helpers because there is no API that will accept an incoherent action row (and
    should not be one); this test is about the READ side.
    """
    upsert_instrument(conn, Instrument(symbol="2331", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="Ghost Semi", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2331", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 1), short_sale=True)
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 15),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="2331", ratio_to=Decimal("1"), ratio_from=Decimal("1"))


def test_unbookable_action_reaches_the_dashboard_wire(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The flag must be on the public read surface, or the UI cannot warn about the row.

    Also asserts the dashboard still answers 200: a rejected action degrades, never 500s.
    """
    _seed_unbookable_action(golden_db)
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    h = next(x for x in r.json()["holdings"]
             if x["symbol"] == "2330" and x["account_id"] == "tw_broker")
    assert h["unbookable_action"] is True
    # Distinct from every other 待釐清 state — the UI renders each of them differently.
    assert h["oversold"] is False and h["short_open"] is False
    assert h["unbookable_dividend"] is False


def test_unbookable_action_reaches_both_drawer_shapes(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Per-account row AND cross-account aggregate: the drawer reads both, and the
    aggregate's shares/market value are a SUM, so one tainted account taints the total."""
    _seed_unbookable_action(golden_db)
    body = api_client.get("/api/symbol/2330/detail").json()

    accts = body["position_accounts"]
    assert [a["account_id"] for a in accts] == ["tw_broker"]
    assert accts[0]["unbookable_action"] is True

    assert body["position"]["unbookable_action"] is True
    # A clean symbol in the same payload keeps the flag False — the wire is not just
    # echoing True everywhere.
    clean = api_client.get("/api/symbol/AAPL/detail").json()
    assert clean["position"]["unbookable_action"] is False
    assert all(a["unbookable_action"] is False for a in clean["position_accounts"])


# --- W5: §6.3's `＋公司行動` reconciliation term (spec §7.3) -------------------------
#
# A corporate action adds shares OUTSIDE the four ledger buckets the drawer footer sums
# (opening / buy / sell / reinvest) — no transaction, no opening, no dividend row. Before
# the term existed EVERY affected symbol reported ⚠ 對帳不一致 while being perfectly
# consistent; measured against the demo corpus 2026-08-11: ORBT −60/−40 (two accounts),
# VRTA −40, VRTB +40, KEMG −1,000 — each of them exactly `shares × (ratio − 1)`.
#
# `corporate_delta` is `shares_action_aware − shares_naive`, both from
# `data_ingestion/holdings.py`, NEITHER of them `build_book` — so the identity stays a
# cross-check of two INDEPENDENT implementations rather than proving a number equals itself.


def _seed_split(conn: sqlite3.Connection, *, account_id: str = "tw_broker",
                symbol: str = "2330", to: str = "2", frm: str = "1",
                on: date = date(2026, 3, 15)) -> None:
    insert_corporate_action(conn, account_id=account_id, action_date=on,
                            kind=CorporateActionKind.SPLIT, from_symbol=symbol,
                            to_symbol=symbol, ratio_to=Decimal(to), ratio_from=Decimal(frm))


def test_split_symbol_reconciles_with_the_corporate_term(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """§7.3 bullet 1: `balances: true` and `diff_shares == 0` for a symbol carrying an action.

    2330 is 1,000 shares bought once; a 2-for-1 split makes the book 2,000. The four ledger
    buckets still say 1,000 — that is the whole point — so the identity closes only because
    the corporate term carries the other 1,000.
    """
    _seed_split(golden_db)
    rec = api_client.get("/api/symbol/2330/detail").json()["activity_reconcile"]["total"]
    assert rec["book_shares"] == "2000"
    assert rec["buy_shares"] == "1000"
    assert rec["corporate_delta_shares"] == "1000"
    assert Decimal(rec["diff_shares"]) == Decimal("0")
    assert rec["balances"] is True
    # net_shares is the WHOLE left-hand side, corporate term included, so the printed
    # equation sums to the printed total.
    assert rec["net_shares"] == "2000"


def test_a_deliberately_wrong_corporate_delta_makes_the_footer_red(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.3's DETECTION-POWER half — and it is the half that matters.

    The mutation is applied to the REAL code path the router sources the delta from
    (`current_shares`, the action-aware term of §6.3's definition), not to a local variable in
    the test: audit F-11 found the sibling test in `test_corporate_actions.py` ending in a
    tautology on a local, which is a test that cannot fail. Everything downstream — the
    subtraction, the identity, the tolerance, the HTTP response — runs unmodified, so a green
    footer here would mean the footer genuinely cannot detect a wrong corporate term.
    """
    from portfolio_dash.data_ingestion.holdings import current_shares as real_current_shares

    _seed_split(golden_db)
    ok = api_client.get("/api/symbol/2330/detail").json()["activity_reconcile"]["total"]
    assert ok["balances"] is True, "precondition: the unmutated path reconciles"

    def wrong(conn: sqlite3.Connection, account_id: str, symbol: str,
              *, index: ActionIndex | None = None) -> Decimal:
        return real_current_shares(conn, account_id, symbol, index=index) + Decimal("1")

    # Patched by DOTTED PATH, which names the exact binding the production code path reads —
    # `symbol.py` imported the function, so rebinding the router's own name is what a wrong
    # `corporate_delta` would actually look like at run time.
    monkeypatch.setattr("portfolio_dash.api.routers.symbol.current_shares", wrong)
    bad = api_client.get("/api/symbol/2330/detail").json()["activity_reconcile"]["total"]
    assert bad["balances"] is False
    assert Decimal(bad["diff_shares"]) == Decimal("1")
    assert bad["corporate_delta_shares"] == "1001"


def test_multi_account_split_reconciles_in_every_account(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """D13's all-or-nothing shape: one symbol, two accounts, two action rows, two footers.

    The per-account breakdown is what the drawer's account filter renders, so an aggregate
    that balances while a per-account slice does not would be invisible.
    """
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("40"), price=Decimal("110"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 12))
    for acct in ("schwab", "moomoo_my"):
        _seed_split(golden_db, account_id=acct, symbol="AAPL", to="3", frm="1")
    rec = api_client.get("/api/symbol/AAPL/detail").json()["activity_reconcile"]
    assert rec["total"]["balances"] is True
    by = rec["by_account"]
    assert set(by) == {"schwab", "moomoo_my"}
    assert all(a["balances"] for a in by.values())
    # 10 -> 30 and 40 -> 120: the delta is per-account, and the total is their sum.
    assert by["schwab"]["corporate_delta_shares"] == "20"
    assert by["moomoo_my"]["corporate_delta_shares"] == "80"
    assert rec["total"]["corporate_delta_shares"] == "100"


def test_exchange_reconciles_on_both_the_emptied_source_and_the_new_destination(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The kind that breaks the identity in BOTH directions.

    The source keeps its buy row and loses its position (delta negative, book 0); the
    destination has no ledger row of its own at all and holds the whole position (delta
    positive, buckets 0). Measured on the demo corpus before this term existed: VRTB +40 and
    VRTA −40 — one symbol red in each direction, from one action.
    """
    upsert_instrument(golden_db, Instrument(symbol="2338", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Semiconductors",
                                            name="TSMC (renamed)", board="TWSE"))
    insert_corporate_action(golden_db, account_id="tw_broker", action_date=date(2026, 3, 15),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="2338", ratio_to=Decimal("1"), ratio_from=Decimal("1"))

    src = api_client.get("/api/symbol/2330/detail").json()["activity_reconcile"]["total"]
    assert src["buy_shares"] == "1000" and src["book_shares"] == "0"
    assert src["corporate_delta_shares"] == "-1000"
    assert src["balances"] is True

    dest = api_client.get("/api/symbol/2338/detail").json()["activity_reconcile"]["total"]
    assert dest["buy_shares"] == "0" and dest["book_shares"] == "1000"
    assert dest["corporate_delta_shares"] == "1000"
    assert dest["balances"] is True


def test_a_symbol_whose_whole_life_is_two_actions_still_gets_a_per_account_footer(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Audit F-45. `MIDC` is created by one EXCHANGE and consumed by the next, so it owns no
    transaction, no opening and no surviving holding — and `acct_ids` (activity ∪ holdings)
    was therefore EMPTY, giving an empty per-account breakdown beside an activity list showing
    two events. The action rows now carry their account into that set.
    """
    for sym, name in (("MIDC", "Interim Co"), ("2340", "Final Co")):
        upsert_instrument(golden_db, Instrument(symbol=sym, market=Market.TW,
                                                quote_ccy=Currency.TWD,
                                                sector="Semiconductors",
                                                name=name, board="TWSE"))
    insert_corporate_action(golden_db, account_id="tw_broker", action_date=date(2026, 3, 15),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="MIDC", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    insert_corporate_action(golden_db, account_id="tw_broker", action_date=date(2026, 4, 15),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="MIDC",
                            to_symbol="2340", ratio_to=Decimal("1"), ratio_from=Decimal("1"))

    body = api_client.get("/api/symbol/MIDC/detail").json()
    assert [(a["kind"], a["role"]) for a in body["activity"]] == [
        ("EXCHANGE", "destination"), ("EXCHANGE", "source")]
    assert set(body["activity_reconcile"]["by_account"]) == {"tw_broker"}
    assert body["activity_reconcile"]["by_account"]["tw_broker"]["balances"] is True
    # The end of the chain is where the shares actually are — the transitive walk found them
    # without this router ever loading another symbol's ledger.
    final = api_client.get("/api/symbol/2340/detail").json()["activity_reconcile"]["total"]
    assert final["corporate_delta_shares"] == "1000" and final["balances"] is True


def test_activity_carries_the_action_rows_between_openings_and_trades(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The `＋公司行動` term has to be traceable to the events that produced it (§6.3).

    Ordering is the replay's own `EventPriority`: an action is effective at the START of its
    date, so it sorts AFTER a same-day opening (D3) and BEFORE that day's trades, whose
    quantities are already quoted in post-action terms.
    """
    upsert_opening(golden_db, account_id="tw_broker", symbol="2330",
                   shares=Decimal("500"), original_cost_total=Decimal("200000"),
                   build_date=date(2026, 3, 15))
    insert_transaction(golden_db, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 15))
    _seed_split(golden_db)
    body = api_client.get("/api/symbol/2330/detail").json()
    same_day = [a["side"] for a in body["activity"] if a["date"] == "2026-03-15"]
    assert same_day == ["open", "action", "buy"]
    act = next(a for a in body["activity"] if a["side"] == "action")
    assert act["kind"] == "SPLIT" and act["role"] == "self"
    assert act["ratio_to"] == "2" and act["ratio_from"] == "1"
    assert act["account_id"] == "tw_broker"
    # No per-row share delta: re-deriving §4's ratio algebra in a router would be a THIRD
    # implementation of it, and the only public approximation gets F-18's same-day opening
    # wrong. The exact figure is the footer's term, sourced from the walker itself.
    assert act["shares"] is None and act["total"] == "0"
    assert body["activity_reconcile"]["total"]["balances"] is True


def test_exactly_one_action_index_is_built_per_request(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit F-24 — and it is a CORRECTNESS gate, not only a speed one.

    `_reconcile` runs once for the total plus once per account, and each needs the
    action-aware share count. A per-call index would re-read and re-group the whole action
    ledger every time (D23 rule 2 / trap #21), AND — the part that silently breaks — each
    throwaway index would carry its own copy of D31's depth-cap sink and D33's
    negative-side-skip sink, so the 待釐清 chip would be written to an object nobody reads.

    Counted at `ActionIndex.from_stored`, the ONE constructor that reads ledger rows, so the
    assertion holds however the router chooses to thread it.
    """
    from portfolio_dash.shared import corporate_actions as ca

    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("40"), price=Decimal("110"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 12))
    for acct in ("schwab", "moomoo_my"):
        _seed_split(golden_db, account_id=acct, symbol="AAPL", to="3", frm="1")

    calls = 0
    real = ca.ActionIndex.from_stored

    def counting(rows: Iterable[Any]) -> ActionIndex:
        nonlocal calls
        calls += 1
        return real(rows)

    monkeypatch.setattr(ca.ActionIndex, "from_stored", counting)
    body = api_client.get("/api/symbol/AAPL/detail").json()
    # Two accounts -> total + 2 per-account footers = 3 `_reconcile` calls, all served by ONE
    # index. (Guards the shape, not just the count: a green assertion with 0 would mean the
    # walk never ran.)
    assert len(body["activity_reconcile"]["by_account"]) == 2
    assert calls == 1, f"ActionIndex.from_stored built {calls} times in one request"


# --- W5 / audit F-17: a red footer never renders without its cause ------------------


def test_a_flagged_position_fails_to_reconcile_and_says_why(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """§6.3: "a position whose basis was discarded genuinely does not reconcile; reporting
    ⚠ 對帳不一致 on it is the correct answer, not a false alarm."

    E3 STICKY is the whole permitted divergence between the two paths (§7.2): `ever_oversold`
    outlives the negative share count, so the replay refuses the split while the share-only
    path — which cannot see basis state — applies it. The footer therefore goes RED, which is
    correct, and `action_issues.unapplied` is what stops it being an unexplained red: it names
    the account, the date, the kind and the reason. Note this position is NOT special-cased
    green anywhere.
    """
    upsert_instrument(golden_db, Instrument(symbol="ZOMB", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Semiconductors",
                                            name="Zombie Co", board="TWSE"))
    # An UNDECLARED sell with nothing to sell -> 賣超, basis discarded, flag STICKY.
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    # A later buy nets it positive again; the flag does NOT clear (2026-07-31 ruling).
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.BUY,
                       quantity=Decimal("300"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 5))
    _seed_split(golden_db, symbol="ZOMB")

    body = api_client.get("/api/symbol/ZOMB/detail").json()
    rec = body["activity_reconcile"]["total"]
    assert rec["balances"] is False, "a discarded basis genuinely does not reconcile"
    assert rec["book_shares"] == "200"          # the replay refused the split
    assert rec["corporate_delta_shares"] == "200"  # the share path applied it
    assert Decimal(rec["diff_shares"]) == Decimal("200")

    unapplied = body["action_issues"]["unapplied"]
    assert len(unapplied) == 1
    row = unapplied[0]
    assert row["account_id"] == "tw_broker" and row["account"]
    assert row["date"] == "2026-03-15" and row["kind"] == "SPLIT"
    assert row["from_symbol"] == "ZOMB" and row["to_symbol"] == "ZOMB"
    assert "賣超" in row["reason"]
    # And the per-holding flag, which is the OTHER of the three channels.
    assert body["position"]["unbookable_action"] is True
    assert body["position"]["oversold"] is True


def test_action_issues_is_scoped_to_the_symbol_and_empty_on_a_clean_one(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The block is ALWAYS present (three empty lists) so the frontend needs no existence
    check, and it never leaks another symbol's problem into this drawer."""
    upsert_instrument(golden_db, Instrument(symbol="ZOMB", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Semiconductors",
                                            name="Zombie Co", board="TWSE"))
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.BUY,
                       quantity=Decimal("300"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 5))
    _seed_split(golden_db, symbol="ZOMB")

    clean = api_client.get("/api/symbol/AAPL/detail").json()["action_issues"]
    assert clean == {"unapplied": [], "depth_capped": [], "negative_side_skipped": []}
    dirty = api_client.get("/api/symbol/ZOMB/detail").json()["action_issues"]
    assert len(dirty["unapplied"]) == 1


def test_d33_negative_side_skip_reaches_the_wire(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """D33's "skip AND flag" — the flag half, which had no channel to the wire before W5.

    The share path refuses an EXCHANGE whose destination is an open declared short (E18), so
    BOTH paths skip it and the footer legitimately reads ✓. That is exactly why the skip has
    to be visible: a green footer over a position whose share count is frozen pre-action is
    the "confidently wrong" reading this package exists to prevent. The sink is read off the
    same per-request `ActionIndex` the walk wrote it into.
    """
    _seed_unbookable_action(golden_db)
    body = api_client.get("/api/symbol/2330/detail").json()
    skipped = body["action_issues"]["negative_side_skipped"]
    assert [s["account_id"] for s in skipped] == ["tw_broker"]
    assert skipped[0]["account"]  # a display name, not a bare id
    assert body["activity_reconcile"]["total"]["balances"] is True
    # …and the per-holding 待釐清 flag is still raised, so the drawer has both halves.
    assert body["position"]["unbookable_action"] is True


def test_unapplied_actions_reach_the_dashboard_payload(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """`Book.unapplied_actions` on the public read surface (audit F-17).

    NOT a duplicate of `HoldingRow.unbookable_action`: two of the three refusal shapes leave
    no surviving position to flag, so the flag is False while an action was silently ignored.
    This one has a survivor precisely so the two can be compared in a single payload.
    """
    upsert_instrument(golden_db, Instrument(symbol="ZOMB", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Semiconductors",
                                            name="Zombie Co", board="TWSE"))
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(golden_db, account_id="tw_broker", symbol="ZOMB", side=Side.BUY,
                       quantity=Decimal("300"), price=Decimal("40"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 5))
    _seed_split(golden_db, symbol="ZOMB")

    r = api_client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["unapplied_actions"]) == 1
    row = body["unapplied_actions"][0]
    assert row["account_id"] == "tw_broker" and row["kind"] == "SPLIT"
    assert row["date"] == "2026-03-15" and row["from_symbol"] == "ZOMB"
    assert "賣超" in row["reason"]
