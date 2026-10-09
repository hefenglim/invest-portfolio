"""QA-19 — 當沖 outranks ETF, and the drawer says which rate it used.

Owner ruling 2026-09-01: **當沖優先**. Two halves, and only the second changed any code.

**The precedence** was already what ``fees._tw`` computes
(``tax_daytrade if daytrade else tax_etf if is_etf else tax_normal``). What it was NOT was a
decision: no repository artefact stated the real-world answer, so the ordering of a ternary
was carrying it, and any future edit could reverse it in good faith. It is now written down in
``markets-and-fees.md`` and pinned here.

**The drawer** is the half that was broken. ``strategy/whatif.py`` calls ``compute_fees``
without ``daytrade``, so the 試算 always quoted 現股 0.3% while the manual form honoured its
當沖 checkbox and booked 0.15% — one trade, two screens, two answers, which is exactly the
failure ``domain-ledger.md`` pins. The drawer has no 當沖 input to read, and that rule's own
remedy for a surface that cannot know which branch applies is to SAY so, not to pick one. So
it discloses, the way ``etf_flag_note`` already does for the ETF flag.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.whatif import compute_whatif

D = Decimal
_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_NOTIONAL = D("100000")


def _db(*, is_etf: bool) -> sqlite3.Connection:
    """A TW account holding 1,000 shares of a symbol registered as ETF or not."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="0050", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="ETF",
                                       name="元大台灣50", is_etf=is_etf))
    insert_transaction(conn, account_id="tw_broker", symbol="0050", side=Side.BUY,
                       quantity=D("1000"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1))
    upsert_prices(conn, [PriceRow(instrument="0050", market=Market.TW,
                                  as_of=date(2026, 6, 9), close=D("100"),
                                  source="test")], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=D("32.5"), source="test")], fetched_at=_NOW)
    conn.commit()
    return conn


def _rules(conn: sqlite3.Connection) -> Any:
    return get_fee_rule_set("tw", conn)


# --- the precedence itself -----------------------------------------------------------------

def test_daytrade_outranks_etf_on_a_tw_sell() -> None:
    """The ruling, pinned on the engine rather than on the shape of a ternary.

    0050 is an ETF (0.1%) sold as a same-day round trip (0.15%). The ruling says 當沖 wins.
    """
    conn = _db(is_etf=True)
    rules = _rules(conn)
    both = compute_fees(rules, Side.SELL, D("1000"), D("100"),
                        is_etf=True, daytrade=True)
    assert both.snapshot["tax_rate"] == str(rules.tax_daytrade)
    assert both.tax == int(rules.tax_daytrade * _NOTIONAL)   # floor(0.0015 × 100,000) = 150
    conn.close()


def test_etf_still_wins_when_it_is_not_a_daytrade() -> None:
    """The counter-example: without the daytrade flag the ETF rate is the answer."""
    conn = _db(is_etf=True)
    rules = _rules(conn)
    etf_only = compute_fees(rules, Side.SELL, D("1000"), D("100"),
                            is_etf=True, daytrade=False)
    assert etf_only.snapshot["tax_rate"] == str(rules.tax_etf)
    assert etf_only.tax == int(rules.tax_etf * _NOTIONAL)    # floor(0.001 × 100,000) = 100
    conn.close()


def test_the_three_tw_sell_rates_are_actually_distinct() -> None:
    """Otherwise both tests above would pass on a rule set that cannot tell them apart."""
    conn = _db(is_etf=True)
    r = _rules(conn)
    assert len({r.tax_normal, r.tax_etf, r.tax_daytrade}) == 3
    assert r.tax_daytrade != r.tax_etf, "the precedence would be unobservable"
    conn.close()


# --- the drawer's disclosure ---------------------------------------------------------------

def test_the_drawer_discloses_that_it_assumed_non_daytrade() -> None:
    """The half that was broken: it quoted one rate as though it were the only one."""
    conn = _db(is_etf=False)
    r = compute_whatif(conn, now=_NOW, reporting=Currency.TWD, account_id="tw_broker",
                       symbol="0050", side=Side.SELL, shares=D("1000"), price=D("100"))
    note = r["daytrade_note"]
    assert note is not None, "a surface that cannot know the branch must say so"
    assert "當沖" in str(note)
    # G-02 (2026-09-02): the rate is now quoted as a PERCENTAGE, the same notation the
    # 費稅規則 summary this sentence is appended to already used ("證交稅 0.3%"). Naming the
    # rate is still the requirement — the raw Decimal "0.0015" was never the requirement.
    pct = f"{(Decimal(str(_rules(conn).tax_daytrade)) * 100).normalize():f}%"
    assert pct in str(note), "name the rate, not just the doubt"
    conn.close()


def test_a_buy_carries_no_tw_tax_so_the_note_stays_quiet() -> None:
    """Noise is how a real warning gets clicked through — the same discipline as
    ``etf_flag_issue_applies``: disclose only when it would change the number."""
    conn = _db(is_etf=False)
    r = compute_whatif(conn, now=_NOW, reporting=Currency.TWD, account_id="tw_broker",
                       symbol="0050", side=Side.BUY, shares=D("1000"), price=D("100"))
    assert r["daytrade_note"] is None
    conn.close()


def test_the_note_is_present_for_an_etf_sell_too() -> None:
    """An ETF sell quotes 0.1%; the daytrade rate 0.15% is still a different answer."""
    conn = _db(is_etf=True)
    r = compute_whatif(conn, now=_NOW, reporting=Currency.TWD, account_id="tw_broker",
                       symbol="0050", side=Side.SELL, shares=D("1000"), price=D("100"))
    assert r["daytrade_note"] is not None
    conn.close()
