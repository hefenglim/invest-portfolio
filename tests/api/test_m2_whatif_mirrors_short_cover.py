"""M2 / QA-04: the drawer 試算 must mirror the replay on a BUY against an OPEN SHORT.

``strategy/whatif.py``'s ``_holding_for`` matched only ``shares > 0``, so an OPEN DECLARED
SHORT read as *unheld* and the BUY arm fell straight through to fresh-position math. Measured
on the QA ledger (NVDA short 10 @ 200 with a 0.05 fee → average sale price 199.995, basis
−1,999.95): a 試算 of "BUY 6 @ 150" returned ``old_shares: null``, ``new_shares: "6"``,
``new_original_avg: "150"`` and no realized figure — a fresh 6-share long quoted on the very
position the drawer renders two rows above as ``shares: "-10", short_open: true``.

``POST /api/input/manual/preview`` answers the SAME trade correctly (``new_shares "-4"``, avg
``199.995``, ``covered_shares "6"``, ``realized_pnl "299.970"``), and the ledger books the
manual door's answer. One app, two answers — the failure ``domain-ledger.md`` pins: a preview
mirrors the replay's BRANCHES for every projected column, or states the fork and gives no
figure.

Every expectation below is ALSO cross-checked against ``build_book`` replaying the same trade
(:func:`_replay_with_buy`). The literals are stated because a cross-check alone can pass by
agreeing with a second wrong derivation; the replay is there because a literal alone can pin
a number the ledger never books.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.whatif import compute_whatif

D = Decimal
_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _db() -> sqlite3.Connection:
    """QA-04's exact ledger: schwab holds an OPEN declared short of 10 NVDA @ 200, fee 0.05.

    Average sale price = (10 × 200 − 0.05) / 10 = 199.995, so the reported holding is
    ``shares -10`` with ``original_cost_total -1999.950``.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="NVDA", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Semiconductors",
                                       name="NVIDIA"))
    insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.SELL,
                       quantity=D("10"), price=D("200"), fees=D("0.05"), tax=D("0"),
                       trade_date=date(2026, 2, 1), short_sale=True)
    upsert_prices(conn, [PriceRow(instrument="NVDA", market=Market.US,
                                  as_of=date(2026, 6, 9), close=D("160"),
                                  source="test")], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=D("32.5"), source="test")], fetched_at=_NOW)
    conn.commit()
    return conn


def _whatif(conn: sqlite3.Connection, **kw: object) -> dict[str, Any]:
    base: dict[str, object] = dict(now=_NOW, reporting=Currency.TWD, account_id="schwab",
                                   symbol="NVDA")
    base.update(kw)
    return compute_whatif(conn, **base)  # type: ignore[arg-type]


def _replay_with_buy(qty: str, px: str) -> tuple[Holding | None, Decimal, Decimal]:
    """Replay the SAME ledger WITH the hypothetical buy: (holding, realized, shares covered).

    Asked of ``build_book`` rather than hand-computed, for the reason
    ``test_review_r1_preview_mirrors_replay.py`` states: a hand-computed expectation can agree
    with a wrong preview.
    """
    conn = _db()
    insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.BUY,
                       quantity=D(qty), price=D(px), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 1))
    book = build_book(load_ledger_bundle(conn))
    held = next((h for h in book.holdings
                 if (h.account_id, h.symbol) == ("schwab", "NVDA")), None)
    covers = [r for r in book.realized.rows if r.kind == "short_cover"]
    conn.close()
    return (held,
            sum((r.realized for r in covers), D("0")),
            sum((r.shares_sold for r in covers), D("0")))


# --- QA-04 itself, figure by figure ------------------------------------------------------

def test_buy_against_an_open_short_projects_the_cover_not_a_fresh_long() -> None:
    """The QA-04 trade: BUY 6 @ 150 against a 10-share short averaged at 199.995."""
    conn = _db()
    r = _whatif(conn, side=Side.BUY, shares=D("6"), price=D("150"))
    # The pre-trade triple is the SIGNED short, not null: the position exists and the drawer
    # prints it two rows above.
    assert r["old_shares"] == "-10"
    assert Decimal(str(r["old_original_avg"])) == D("199.995")
    assert Decimal(str(r["old_adjusted_avg"])) == D("199.995")
    # cover = min(6, 10) = 6 at THIS buy's per-share cost 150 -> (199.995 - 150) * 6.
    assert r["covered_shares"] == "6"
    assert r["realized"] == "299.970"
    # Still short 4, still averaged at the sale price — NOT a 6-share long at 150.
    assert r["new_shares"] == "-4"
    assert r["new_original_avg"] == "199.995"
    assert r["new_adjusted_avg"] == "199.995"
    assert r["realized_note"] is not None and "回補" in str(r["realized_note"])
    conn.close()


def test_the_qa04_projection_equals_what_the_ledger_actually_books() -> None:
    """The same trade, replayed: every projected column against ``build_book``'s answer."""
    conn = _db()
    r = _whatif(conn, side=Side.BUY, shares=D("6"), price=D("150"))
    held, realized, covered = _replay_with_buy("6", "150")
    assert held is not None
    assert Decimal(str(r["new_shares"])) == held.shares
    assert Decimal(str(r["new_original_avg"])) == held.original_avg
    assert Decimal(str(r["new_adjusted_avg"])) == held.adjusted_avg
    assert Decimal(str(r["realized"])) == realized
    assert Decimal(str(r["covered_shares"])) == covered
    conn.close()


def test_exact_cover_states_no_average_because_there_is_no_position() -> None:
    """BUY 10 @ 150 covers the short exactly: flat, so no average — and 499.950 realized."""
    conn = _db()
    r = _whatif(conn, side=Side.BUY, shares=D("10"), price=D("150"))
    assert r["new_shares"] == "0"
    assert r["new_original_avg"] is None and r["new_adjusted_avg"] is None
    assert r["covered_shares"] == "10"
    assert Decimal(str(r["realized"])) == D("499.950")     # (199.995 - 150) * 10
    held, realized, covered = _replay_with_buy("10", "150")
    assert held is None                                    # build_book drops shares == 0
    assert Decimal(str(r["realized"])) == realized
    assert Decimal(str(r["covered_shares"])) == covered
    conn.close()


def test_over_cover_goes_long_at_this_buys_own_cost() -> None:
    """BUY 16 @ 150: 10 cover the short, the leftover 6 start their long life at 150."""
    conn = _db()
    r = _whatif(conn, side=Side.BUY, shares=D("16"), price=D("150"))
    assert r["new_shares"] == "6"
    assert Decimal(str(r["new_original_avg"])) == D("150")
    assert Decimal(str(r["new_adjusted_avg"])) == D("150")
    assert Decimal(str(r["realized"])) == D("499.950")
    held, realized, covered = _replay_with_buy("16", "150")
    assert held is not None
    assert Decimal(str(r["new_shares"])) == held.shares
    assert Decimal(str(r["new_original_avg"])) == held.original_avg
    assert Decimal(str(r["new_adjusted_avg"])) == held.adjusted_avg
    assert Decimal(str(r["realized"])) == realized
    assert Decimal(str(r["covered_shares"])) == covered
    conn.close()


def test_an_ordinary_buy_still_carries_no_cover_figures() -> None:
    """Detection power the other way: the long branch must not grow a phantom cover."""
    conn = _db()
    insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.BUY,
                       quantity=D("30"), price=D("150"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 1))          # 20 long after the cover
    conn.commit()
    r = _whatif(conn, side=Side.BUY, shares=D("10"), price=D("100"))
    assert r["covered_shares"] is None and r["realized"] is None
    assert r["realized_note"] is None
    assert Decimal(str(r["new_shares"])) == D("30")
    conn.close()


# --- the SELL side of the same holding: state the fork, but count the shares --------------

def test_sell_into_an_open_short_reports_the_signed_remainder() -> None:
    """A SELL of 5 against a 10-share short leaves −15 either way; only the BASIS forks.

    A declared short extends the short lot (−15) and an undeclared oversell nets to −15 with
    the basis discarded — the share count is the ONE figure both readings agree on, and the
    drawer used to print −5 because the short read as "unheld" (held_shares 0).
    """
    conn = _db()
    r = _whatif(conn, side=Side.SELL, shares=D("5"), price=D("210"))
    assert r["remaining_shares"] == "-15"
    assert r["old_shares"] == "-10"
    assert r["oversell"] is True
    # The fork is stated, and no figure is given for what depends on the unasked question.
    assert r["realized"] is None and r["adjusted_cost_removed"] is None
    assert r["new_original_avg"] is None and r["new_adjusted_avg"] is None
    assert "放空" in str(r["realized_note"]) and "賣超" in str(r["realized_note"])
    conn.close()


@pytest.mark.parametrize(("qty", "price"), [("5", "210"), ("12", "210")])
def test_sell_signed_remainder_equals_both_forks_share_count(qty: str, price: str) -> None:
    """Whichever way the user later declares it, the ledger holds the projected share count."""
    conn = _db()
    r = _whatif(conn, side=Side.SELL, shares=D(qty), price=D(price))
    shown = Decimal(str(r["remaining_shares"]))
    for declared in (True, False):
        c2 = _db()
        insert_transaction(c2, account_id="schwab", symbol="NVDA", side=Side.SELL,
                           quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 3, 1), short_sale=declared)
        book = build_book(load_ledger_bundle(c2), allow_oversell=True)
        held = next(h for h in book.holdings
                    if (h.account_id, h.symbol) == ("schwab", "NVDA"))
        assert shown == held.shares, f"declared={declared}: {shown} != {held.shares}"
        c2.close()
    conn.close()
