"""M2 / QA-01 + QA-02 — the reporting rollup degrades PER ACCOUNT, and says so.

Two failure modes of ``compute_fx_summary``, one mechanism:

* **QA-01** — the ``home -> reporting`` rate was resolved UNGUARDED for every account in
  ``foreign_exposure``, and ``portfolio/dashboard.py`` puts every ``settlement_ccy !=
  funding_ccy`` account there whether or not it holds anything. One EMPTY account with no
  ``MYR/TWD`` row therefore raised, and the caller's ``except KeyError`` dropped the WHOLE
  ``fx`` section — taking a correct 33,000 TWD with it, with no reason string anywhere.
* **QA-02** — an account whose ``foreign -> home`` spot is missing gets ``unrealized_* =
  None``; the ``is not None`` guards then skipped it SILENTLY and the remainder was
  presented as the portfolio's complete FX figure (22,000 where the truth is 53,240).

The fix is the house C6 pattern already used by ``GET /api/cash``
(``api/routers/cash.py``): skip what genuinely carries nothing, EXCLUDE what carries
something that cannot be expressed, and label the figure that is therefore partial.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from portfolio_dash.forex.fx_pnl import compute_fx_summary
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                 dividend_model="drip_us")
MOOMOO = Account(account_id="moomoo_my", name="Moomoo", broker="Moomoo",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.MYR,
                 dividend_model="drip_us")
ACCTS = {"schwab": SCHWAB, "moomoo_my": MOOMOO}
INSTR = {"AAPL": Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                            sector="Tech", name="Apple")}
ZERO = Decimal("0")

SpotFn = Callable[[Currency, Currency], Decimal]


def _spot_for(rates: dict[tuple[Currency, Currency], str]) -> SpotFn:
    """A ``current_spot`` that behaves EXACTLY like the production callable.

    ``portfolio/dashboard.py::RateResolver.rate`` returns the identity for a same-currency
    pair and raises ``KeyError("尚無 X/Y 匯率資料")`` for a pair with no stored rows.
    """

    def spot(frm: Currency, to: Currency) -> Decimal:
        if frm is to:
            return Decimal("1")
        key = (frm, to)
        if key not in rates:
            raise KeyError(f"尚無 {frm.value}/{to.value} 匯率資料")
        return Decimal(rates[key])

    return spot


def _buy(account_id: str, qty: str, on: date) -> Transaction:
    return Transaction(account_id=account_id, symbol="AAPL", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal("100"), fees=ZERO, tax=ZERO,
                       trade_date=on)


def _conv(account_id: str, on: date, frm: Currency, frm_amt: str,
          to: Currency, to_amt: str) -> FXConversion:
    return FXConversion(account_id=account_id, date=on, from_ccy=frm,
                        from_amount=Decimal(frm_amt), to_ccy=to, to_amount=Decimal(to_amt))


# --- QA-01 -----------------------------------------------------------------------------


def test_an_empty_accounts_missing_reporting_rate_does_not_void_the_section() -> None:
    """schwab's 33,000 TWD survives an EMPTY moomoo_my that has no MYR/TWD rate.

    Hand-derived: pool avg = 300,000 / 10,000 = **30** TWD/USD; the buy spends
    100 x 100 = 10,000 USD so foreign cash is **0**; stock exposure 11,000 USD (100 sh
    @ 110); spot 33 -> unrealized FX = 11,000 x (33 - 30) = **33,000 TWD**. moomoo_my
    holds nothing at all, so skipping it loses nothing and is NOT an exclusion.
    """
    convs = [_conv("schwab", date(2025, 6, 11), Currency.TWD, "300000",
                   Currency.USD, "10000")]
    txs = [_buy("schwab", "100", date(2025, 6, 11))]
    exposure = {"schwab": (Currency.USD, Decimal("11000")),
                "moomoo_my": (Currency.USD, ZERO)}
    spot = _spot_for({(Currency.USD, Currency.TWD): "33"})

    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, exposure, spot, Currency.TWD)

    sch = summary.by_account["schwab"]
    assert sch.avg_rate == Decimal("30")
    assert sch.foreign_cash == ZERO
    assert sch.unrealized_fx_total == Decimal("33000")
    assert summary.reporting_unrealized_fx == Decimal("33000")
    assert summary.reporting_realized_fx == ZERO
    # The empty account is still REPORTED (its card renders the null glyph), it simply
    # contributes nothing — and contributing nothing is not an exclusion.
    moo = summary.by_account["moomoo_my"]
    assert moo.unrealized_fx_total is None
    assert summary.excluded_accounts == []
    assert summary.reporting_unavailable_reason is None


# --- QA-02 -----------------------------------------------------------------------------


def _two_funded_pools() -> tuple[list[Transaction], list[FXConversion],
                                 dict[str, tuple[Currency, Decimal]]]:
    """schwab (TWD-anchored, avg 32) + moomoo_my (MYR-anchored, avg 4.4), both funded.

    Each: 10,000 USD acquired, 50 sh @ 100 bought (5,000 USD cash left), 6,000 USD of
    stock at a 120 close -> 11,000 USD of exposure per account.
    """
    convs = [_conv("schwab", date(2026, 1, 5), Currency.TWD, "320000", Currency.USD, "10000"),
             _conv("moomoo_my", date(2026, 1, 5), Currency.MYR, "44000",
                   Currency.USD, "10000")]
    txs = [_buy("schwab", "50", date(2026, 1, 10)), _buy("moomoo_my", "50", date(2026, 1, 10))]
    exposure = {"schwab": (Currency.USD, Decimal("6000")),
                "moomoo_my": (Currency.USD, Decimal("6000"))}
    return txs, convs, exposure


def test_a_missing_foreign_spot_excludes_that_account_and_labels_the_figure() -> None:
    """22,000 is only schwab's half — and it must SAY so (QA-02).

    Hand-derived: schwab avg = 320,000 / 10,000 = 32, spot 34, exposure = cash 5,000 +
    stock 6,000 = 11,000 USD -> 11,000 x (34 - 32) = **22,000 TWD**. moomoo_my has the
    same 11,000 USD of exposure but no USD/MYR rate, so its unrealized legs are None and
    the rollup cannot include it.
    """
    txs, convs, exposure = _two_funded_pools()
    spot = _spot_for({(Currency.USD, Currency.TWD): "34",
                      (Currency.MYR, Currency.TWD): "7.1"})

    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, exposure, spot, Currency.TWD)

    assert summary.by_account["schwab"].unrealized_fx_total == Decimal("22000")
    assert summary.by_account["moomoo_my"].unrealized_fx_total is None
    assert summary.reporting_unrealized_fx == Decimal("22000")
    assert summary.excluded_accounts == ["moomoo_my"]
    reason = summary.reporting_unavailable_reason
    assert reason is not None
    assert "moomoo_my" in reason          # WHICH account
    assert "USD/MYR" in reason            # WHICH pair is missing
    assert "未實現" in reason              # WHICH figure is partial
    assert "已實現" not in reason          # realized rolled up completely (0 either way)


def test_seeding_the_missing_pair_completes_the_rollup() -> None:
    """The same ledger with USD/MYR present: 53,240 TWD and no reason at all.

    Hand-derived: moomoo_my 11,000 USD x (4.8 - 4.4) = 4,400 MYR; 4,400 x 7.1 =
    **31,240 TWD**; + schwab's 22,000 = **53,240 TWD**.
    """
    txs, convs, exposure = _two_funded_pools()
    spot = _spot_for({(Currency.USD, Currency.TWD): "34",
                      (Currency.MYR, Currency.TWD): "7.1",
                      (Currency.USD, Currency.MYR): "4.8"})

    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, exposure, spot, Currency.TWD)

    assert summary.by_account["moomoo_my"].unrealized_fx_total == Decimal("4400")
    assert summary.reporting_unrealized_fx == Decimal("53240")
    assert summary.excluded_accounts == []
    assert summary.reporting_unavailable_reason is None


def test_an_active_account_without_a_reporting_rate_is_excluded_not_dropped() -> None:
    """The QA-01 mechanism on a FUNDED account: exclude + label, never void the section.

    moomoo_my reconverts 1,000 USD -> 4,600 MYR on 2026-03-01: realized = 4,600 -
    1,000 x 4.4 = **200 MYR**. Remaining cash 10,000 - 5,000 - 1,000 = 4,000 USD; with
    the 6,000 USD stock leg and spot 4.8 the unrealized total is 10,000 x 0.4 =
    **4,000 MYR**. Neither can reach TWD without a MYR/TWD rate, so BOTH figures are
    partial and both must be labelled — while schwab's 22,000 still rolls up.
    """
    txs, convs, exposure = _two_funded_pools()
    convs.append(_conv("moomoo_my", date(2026, 3, 1), Currency.USD, "1000",
                       Currency.MYR, "4600"))
    spot = _spot_for({(Currency.USD, Currency.TWD): "34",
                      (Currency.USD, Currency.MYR): "4.8"})

    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, exposure, spot, Currency.TWD)

    moo = summary.by_account["moomoo_my"]
    assert moo.realized_fx == Decimal("200")
    assert moo.foreign_cash == Decimal("4000")
    assert moo.unrealized_fx_total == Decimal("4000")
    # schwab is unaffected; the whole section is NOT voided (QA-01's blast radius).
    assert summary.reporting_unrealized_fx == Decimal("22000")
    assert summary.reporting_realized_fx == ZERO
    assert summary.excluded_accounts == ["moomoo_my"]
    reason = summary.reporting_unavailable_reason
    assert reason is not None
    assert "MYR/TWD" in reason
    assert "已實現" in reason and "未實現" in reason
