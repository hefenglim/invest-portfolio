"""R2 counter-evidence: three ways the return figures answered the wrong question.

All three fail on the pre-fix code.

* **⑤** — a dividend the replay REFUSED (it lands on an open short, which this ledger has no
  debit row for) was still counted as a positive cashflow by XIRR.  Three surfaces, three
  answers for one payment: 總報酬 excluded it, XIRR counted it, and the trend flagged the whole
  day 待釐清.
* **⑥** — ``total_return`` applies FX to each currency's net P&L and therefore never to the
  PRINCIPAL, while the trend's ``total_value − net_invested`` (flows at their own trade-date
  rates) is the FX-complete figure and was labelled 浮動損益.
* **⑦** — ``xirr_reporting`` had no ``cash_movements`` parameter at all, so a broker rebate,
  a broker fee and margin interest reached no return metric (AI-D42, superseding D1=A).
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.returns import xirr_reporting
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction

D = Decimal
_TSLA = Instrument(symbol="TSLA", market=Market.US, quote_ccy=Currency.USD,
                   sector="Auto", name="Tesla")
_INSTR = {"TSLA": _TSLA}


def _tx(side: Side, qty: str, price: str, d: date, *, short: bool = False) -> Transaction:
    return Transaction(account_id="schwab", symbol="TSLA", side=side, quantity=D(qty),
                       price=D(price), fees=D("0"), tax=D("0"), trade_date=d,
                       short_sale=short)


def _one_usd(d: date, base: Currency, quote: Currency) -> Decimal:
    return D("1")


def _spot(base: Currency, quote: Currency) -> Decimal:
    return D("1")


def _cash(kind: str, amount: str, d: date, ccy: Currency = Currency.USD
          ) -> StoredCashMovement:
    return StoredCashMovement(id=0, account_id="schwab", date=d, kind=kind,
                              ccy=ccy, amount=D(amount))


# --- ⑤ a refused dividend must not reach XIRR either ------------------------------------

def _short_with_dividend() -> LedgerBundle:
    """Open a declared short, then a dividend lands on it while the position is short.

    ``domain-ledger.md``: a short seller PAYS the dividend in lieu and this ledger has no
    debit row for that, so the replay refuses the event and flags the position.
    """
    return LedgerBundle(
        [_tx(Side.SELL, "100", "260", date(2026, 3, 1), short=True)],
        dividends=[Dividend(account_id="schwab", symbol="TSLA", date=date(2026, 5, 1),
                            type=DividendType.CASH, gross=D("500"), withholding=D("0"),
                            net=D("500"))],
        instruments=_INSTR,
    )


def test_the_replay_reports_which_dividends_it_refused() -> None:
    book = build_book(_short_with_dividend(), allow_oversell=True)
    assert [d.net for d in book.refused_dividends] == [D("500")]
    assert any(h.unbookable_dividend for h in book.holdings)


def test_a_refused_dividend_is_excluded_from_the_xirr_flows() -> None:
    """The payment 總報酬 refuses must not be an inflow to XIRR — one ledger, one answer."""
    bundle = _short_with_dividend()
    book = build_book(bundle, allow_oversell=True)
    out = xirr_reporting(
        bundle.transactions, bundle.dividends, [], book.holdings, _INSTR,
        _one_usd, {"TSLA": D("200")}, _spot, date(2026, 12, 31), Currency.USD,
        refused_dividends=book.refused_dividends, cash_movements=[])
    with_refusal = out.rate
    # The same ledger with the unbookable payment deleted must give the SAME rate: if the
    # refusal is honoured, the two series are identical.
    clean = LedgerBundle(bundle.transactions, dividends=[], instruments=_INSTR)
    clean_book = build_book(clean, allow_oversell=True)
    out2 = xirr_reporting(
        clean.transactions, [], [], clean_book.holdings, _INSTR,
        _one_usd, {"TSLA": D("200")}, _spot, date(2026, 12, 31), Currency.USD,
        refused_dividends=clean_book.refused_dividends, cash_movements=[])
    assert with_refusal == out2.rate


def test_a_BOOKABLE_dividend_still_reaches_xirr() -> None:
    """The guard must be surgical: only the refused row drops out, never the ordinary one."""
    bundle = LedgerBundle(
        [_tx(Side.BUY, "100", "100", date(2026, 1, 5))],
        dividends=[Dividend(account_id="schwab", symbol="TSLA", date=date(2026, 5, 1),
                            type=DividendType.CASH, gross=D("500"), withholding=D("0"),
                            net=D("500"))],
        instruments=_INSTR,
    )
    book = build_book(bundle, allow_oversell=True)
    assert book.refused_dividends == []
    kw = dict(fx_at=_one_usd, current_prices={"TSLA": D("120")}, current_fx=_spot,
              as_of=date(2026, 12, 31), reporting=Currency.USD,
              refused_dividends=book.refused_dividends, cash_movements=[])
    with_div = xirr_reporting(bundle.transactions, bundle.dividends, [], book.holdings,
                              _INSTR, **kw)                      # type: ignore[arg-type]
    without = xirr_reporting(bundle.transactions, [], [], book.holdings,
                             _INSTR, **kw)                       # type: ignore[arg-type]
    assert with_div.rate is not None and without.rate is not None
    assert with_div.rate > without.rate, "the cash dividend must still lift the return"


# --- ⑦ the three trading/financing cash kinds (AI-D42) ----------------------------------

def _rate_with(movements: list[StoredCashMovement]) -> Decimal | None:
    bundle = LedgerBundle([_tx(Side.BUY, "100", "100", date(2026, 1, 5))],
                          instruments=_INSTR)
    book = build_book(bundle, allow_oversell=True)
    return xirr_reporting(
        bundle.transactions, [], [], book.holdings, _INSTR, _one_usd,
        {"TSLA": D("120")}, _spot, date(2026, 12, 31), Currency.USD,
        refused_dividends=[], cash_movements=movements).rate


def test_a_broker_rebate_raises_the_return() -> None:
    """FE-D1's charge-first model refunds 77% next month — 0.229% of capital per round trip."""
    base = _rate_with([])
    with_rebate = _rate_with([_cash("REBATE", "300", date(2026, 3, 1))])
    assert base is not None and with_rebate is not None
    assert with_rebate > base


def test_a_broker_fee_and_margin_interest_lower_the_return() -> None:
    base = _rate_with([])
    for kind in ("BROKER_FEE", "INTEREST_EXPENSE"):
        worse = _rate_with([_cash(kind, "300", date(2026, 3, 1))])
        assert base is not None and worse is not None
        assert worse < base, kind


def test_capital_movements_and_idle_cash_interest_are_excluded() -> None:
    """AI-D42's line, stated as a test.

    ``DEPOSIT``/``WITHDRAW``/``OPENING`` would turn XIRR into an ACCOUNT return, and
    ``INTEREST`` is earned on principal that never entered the denominator — crediting its
    yield to the numerator is asymmetric. D12's 重組費 is a WITHDRAW, so that standing
    limitation is untouched by this change.
    """
    base = _rate_with([])
    for kind in ("DEPOSIT", "WITHDRAW", "OPENING", "INTEREST"):
        assert _rate_with([_cash(kind, "5000", date(2026, 3, 1))]) == base, kind


# --- ⑥ which figure actually embeds FX: A · B · B−A (AI-D41) ----------------------------

def test_the_kpi_band_carries_the_fx_complete_figure_and_names_the_difference() -> None:
    """A MYR-funded USD position: the principal's own FX move must be visible somewhere.

    ``total_return`` (A) applies today's spot to each currency's NET P&L, so the rate never
    touches the PRINCIPAL. The trend's ``total_value − net_invested`` (B) converts every flow
    at its own trade-date rate, so it does. B − A is the principal-FX effect — which is the
    換匯損益 card's content, and therefore must be PRESENTED beside A, never added to it
    (invariant 6: adding it double-counts the cross term).
    """
    from portfolio_dash.portfolio.dashboard import fx_complete_return
    from portfolio_dash.portfolio.dashboard_models import (
        KpiSummary,
        TrendPoint,
        TrendSeries,
    )

    # 100,000 invested at 30 TWD/USD; today the same position is worth 110,000 TWD-of-value
    # at 33 TWD/USD. A sees only the gain translated; B sees the principal move too.
    trend = TrendSeries(
        points=[TrendPoint(date=date(2026, 1, 5), total_value=D("100000"),
                           net_invested=D("100000")),
                TrendPoint(date=date(2026, 12, 31), total_value=D("121000"),
                           net_invested=D("100000"))],
        reporting_currency=Currency.TWD, available=True)
    b = fx_complete_return(trend)
    assert b == D("21000")

    kpis = KpiSummary(reporting_currency=Currency.TWD, total_return=D("11000"),
                      total_return_fx_complete=b,
                      principal_fx_effect=b - D("11000"))
    assert kpis.principal_fx_effect == D("10000")
    # The three are a DECOMPOSITION: B is its own measurement, and B - A is the name of
    # the gap between them. The identity below is what makes them safe to show together
    # and unsafe to add: B already CONTAINS the principal-FX effect.
    a, gap = kpis.total_return, kpis.principal_fx_effect
    assert a is not None and gap is not None
    assert kpis.total_return_fx_complete == a + gap


def test_the_fx_complete_figure_is_none_when_the_trend_is_unavailable() -> None:
    """Honest degradation: no trend, no B — never a fabricated stand-in."""
    from portfolio_dash.portfolio.dashboard import fx_complete_return
    from portfolio_dash.portfolio.dashboard_models import TrendSeries

    assert fx_complete_return(
        TrendSeries(points=[], reporting_currency=Currency.TWD, available=False)) is None


def test_an_incomplete_last_trend_day_yields_no_fx_complete_figure() -> None:
    """A day the trend itself marks 待釐清 cannot be the basis of a headline KPI."""
    from portfolio_dash.portfolio.dashboard import fx_complete_return
    from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries

    trend = TrendSeries(
        points=[TrendPoint(date=date(2026, 12, 31), total_value=D("121000"),
                           net_invested=D("100000"), incomplete=True)],
        reporting_currency=Currency.TWD, available=True)
    assert fx_complete_return(trend) is None
