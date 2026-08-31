"""QA-02 — an already-reported realized-FX figure must not move when a LATER
acquisition is entered.

``docs/accounting-formula-manual.md`` §8.2 (the activated accounting authority) defines the
cost side of a reconversion as 「回換前 Schwab USD pool avg_rate」 — the weighted average **as
it stood at the reconversion**. The engine applied ONE all-time average to every
reconversion regardless of date, so a March conversion silently restated a February figure
the owner had already read, and at a high enough March rate it FLIPPED ITS SIGN.

The same app is already date-aware for the identical problem class on the equity side
(``holdings.shares_through``, 2026-07-31) and on the cash side (``running_min``, audit C3).

What must NOT change (and is asserted here, not assumed):

* **UNREALIZED stays full-history** — it marks to market what is held *now*, so its rate is
  the pool average as of today.
* **``covered_ratio`` / F2 / F3** are untouched: coverage is a property of the pool, not of
  one disposal, and realized FX is never scaled by it (§8.3).
* **The None semantics survive, applied per row**: a reconversion with no acquisition on or
  before its own date yields NO realized row rather than being priced by a rate that did not
  exist yet.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.forex.fx_pnl import compute_account_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import FXConversion

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                 dividend_model="drip_us")
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}

# The QA reproduction, verbatim.
JAN_ACQUIRE = date(2026, 1, 15)    # TWD 320,000 -> USD 10,000 @ 32.00
FEB_RECONVERT = date(2026, 2, 20)  # USD 5,000 -> TWD 170,000  => realized +10,000
MAR_ACQUIRE = date(2026, 3, 10)    # a LATER acquisition, at any rate


def _conv(d: date, frm: Currency, famt: str, to: Currency, tamt: str) -> FXConversion:
    return FXConversion(account_id="schwab", date=d, from_ccy=frm,
                        from_amount=Decimal(famt), to_ccy=to, to_amount=Decimal(tamt))


def _jan_and_feb() -> list[FXConversion]:
    """The ledger as the owner saw it in February: one acquisition, one reconversion."""
    return [
        _conv(JAN_ACQUIRE, Currency.TWD, "320000", Currency.USD, "10000"),
        _conv(FEB_RECONVERT, Currency.USD, "5000", Currency.TWD, "170000"),
    ]


def _march(rate: str) -> FXConversion:
    """A March TWD->USD acquisition of 10,000 USD at *rate* TWD per USD."""
    return _conv(MAR_ACQUIRE, Currency.TWD, str(Decimal(rate) * 10000),
                 Currency.USD, "10000")


def _realized(
    convs: list[FXConversion], *, movements: list[StoredCashMovement] | None = None,
) -> Decimal | None:
    return compute_account_fx(
        SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
        spot=Decimal("33"), movements=movements,
    ).realized_fx


def test_february_realized_is_byte_identical_before_and_after_a_march_acquisition() -> None:
    """The headline regression: entering March must not restate February.

    170,000 - 5,000 x 32.00 = +10,000 — the figure as at the reconversion, and the only
    figure the ledger can honestly report for it.
    """
    before = _realized(_jan_and_feb())
    after = _realized([*_jan_and_feb(), _march("35")])
    assert before == Decimal("10000")
    # Byte-identical, not merely equal: a Decimal that changed exponent would still compare
    # equal while rendering differently on the wire (Decimal strings ARE the contract).
    assert str(after) == str(before)


@pytest.mark.parametrize("march_rate", ["30", "35", "40", "50"])
def test_no_march_rate_can_move_the_february_figure(march_rate: str) -> None:
    """The QA sweep: 30 gave +15,000, 40 gave -10,000, 50 gave -35,000 (both sign flips).

    The as-at-February value is +10,000 in every case.
    """
    assert _realized([*_jan_and_feb(), _march(march_rate)]) == Decimal("10000")


def test_a_reconversion_before_any_acquisition_is_not_priced_by_a_future_rate() -> None:
    """The worse half of QA-02: a rate that did not exist yet cannot price a disposal.

    ``avg_rate is None`` used to be evaluated ONCE for the whole account; applied per row it
    means this reconversion yields no realized row at all — the existing "never guess a rate"
    convention, at the granularity the problem actually has.
    """
    early = _conv(date(2025, 12, 1), Currency.USD, "1000", Currency.TWD, "33000")
    convs = [early, _conv(JAN_ACQUIRE, Currency.TWD, "320000", Currency.USD, "10000")]
    # The account HAS a basis, so the figure is 0 (no bookable row), not None.
    assert _realized(convs) == Decimal("0")


def test_a_same_day_acquisition_does_price_the_reconversion() -> None:
    """The bound is ``<= d``: this ledger has no intra-day ordering, so a same-day funding
    conversion is part of the pool the reconversion draws on. A strict ``<`` would leave a
    fund-then-convert day with no basis at all — a regression, not a fix."""
    convs = [
        _conv(FEB_RECONVERT, Currency.TWD, "320000", Currency.USD, "10000"),
        _conv(FEB_RECONVERT, Currency.USD, "5000", Currency.TWD, "170000"),
    ]
    assert _realized(convs) == Decimal("10000")


def test_a_later_rated_cash_movement_also_cannot_restate_february() -> None:
    """Movements are the second acquisition source (spec 2026-07-30 F1) and are bounded too.

    A rated USD deposit in March moves the pool average exactly as a conversion does, so
    date-bounding only the conversions would leave the same defect reachable through the
    other door.
    """
    later = StoredCashMovement(
        id=0, account_id="schwab", date=MAR_ACQUIRE, kind="DEPOSIT", ccy=Currency.USD,
        amount=Decimal("10000"), acq_home_amount=Decimal("350000"))
    assert _realized(_jan_and_feb(), movements=[later]) == Decimal("10000")


def test_N2_still_holds_filling_in_an_OLD_acquisition_cost_recomputes_history() -> None:
    """``domain-ledger.md`` N2 is PRESERVED, and it is the distinction the fix turns on.

    N2 authorises *filling in a missing* ``acq_home_amount`` on an acquisition that was
    always there, and says the resulting change to previously displayed realized FX "is
    intended". A date bound keeps that: the January opening is dated BEFORE the February
    disposal either way, so it enters the bounded basis the moment it acquires a cost.

    What N2 does not authorise — and what QA-02 was — is a **new** acquisition repricing a
    **past** disposal. Same ledger edit in kind, opposite direction in time.
    """
    def opening(acq: str | None) -> StoredCashMovement:
        return StoredCashMovement(
            id=0, account_id="schwab", date=date(2026, 1, 5), kind="OPENING",
            ccy=Currency.USD, amount=Decimal("10000"),
            acq_home_amount=None if acq is None else Decimal(acq))

    # No cost recorded -> the amount funds the balance but never the average (F1).
    assert _realized(_jan_and_feb(), movements=[opening(None)]) == Decimal("10000")
    # The owner fills the cost in later: (320,000 + 340,000) / 20,000 = 33.00
    # -> 170,000 - 5,000 x 33 = +5,000. History moves, exactly as N2 says it should.
    assert _realized(_jan_and_feb(), movements=[opening("340000")]) == Decimal("5000")


def test_unrealized_and_the_reported_average_stay_FULL_HISTORY() -> None:
    """Preserved deliberately: the mark-to-market rate is today's pool average.

    Realized answers "what did that disposal earn"; unrealized answers "what is the exposure
    I still hold worth" — the second question is asked as of today, so its rate must include
    every acquisition. Jan 320,000/10,000 + Mar 350,000/10,000 -> 33.5 over 20,000 USD.
    """
    convs = [*_jan_and_feb(), _march("35")]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("1000"), [], [], convs, INSTR,
                           spot=Decimal("33"))
    assert r.avg_rate == Decimal("33.5")
    # foreign cash = +10,000 - 5,000 + 10,000 = 15,000; spot 33 - avg 33.5 = -0.5
    assert r.foreign_cash == Decimal("15000")
    assert r.unrealized_fx_cash == Decimal("15000") * (Decimal("33") - Decimal("33.5"))
    assert r.unrealized_fx_stocks == Decimal("1000") * (Decimal("33") - Decimal("33.5"))
    assert r.realized_fx == Decimal("10000")   # ...and realized is still as-at-February


def test_covered_ratio_and_the_gap_flags_are_untouched_by_the_bound() -> None:
    """F2/F3 are properties of the POOL, not of one disposal — they stay full-history.

    An unbased March deposit dilutes the coverage of the exposure still held; it does not
    retro-flag the February disposal, and realized FX is never scaled by the ratio (§8.3).
    """
    unbased = StoredCashMovement(
        id=0, account_id="schwab", date=MAR_ACQUIRE, kind="DEPOSIT", ccy=Currency.USD,
        amount=Decimal("10000"), acq_home_amount=None)
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], _jan_and_feb(), INSTR,
                           spot=Decimal("33"), movements=[unbased])
    assert r.covered_ratio == Decimal("10000") / Decimal("20000")
    assert r.fx_basis_incomplete is True
    assert r.realized_fx == Decimal("10000")   # unscaled, and unmoved
