"""Contract tests for GET /api/symbol/{symbol}/detail (spec 01).

Read-only: price_history comes from STORED prices (no live backfill). cost_basis binds
to the account holding the most shares (Q1); null for a non-held / watchlist symbol.
"""

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
    upsert_opening,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
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
