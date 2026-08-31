"""F-2 — one unusable provider RATE must not abort the refresh sweep.

The QA-09 wave closed this on the PRICE side only: ``upsert_prices`` refuses a non-positive
close and ``refresh_quotes`` / ``refresh_history`` pre-filter with ``storable_close`` so the
other symbols still update. ``upsert_fx``'s older R3 guard was left raising straight out of
the refresh, and both FX callers hand it the WHOLE provider batch:

* ``refresh_quotes`` — the prices have ALREADY been upserted when ``upsert_fx`` raises, so the
  run half-lands and the ``RefreshSummary`` is never returned. The scheduler job records the
  raise, and nothing anywhere names the pair that caused it;
* ``refresh_fx_history`` — the whole backfill is lost over one bad day.

That is the exact opposite of this module's own stated contract ("failed keys are recorded in
the summary rather than raised") and of ``data-and-pricing.md``'s "a failed/stale fetch
degrades gracefully ... never crash". The refused pair simply keeps its last-known rate, which
the read already labels stale.

The seam itself is UNCHANGED: a direct caller still gets the loud ``ValueError``. What is new
is that ``storable_rate`` is the one owner of the predicate, so the pre-filter and the seam
cannot drift into disagreeing about what a rate is.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_fx_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import (
    get_fx,
    get_latest_price,
    storable_rate,
    upsert_fx,
)
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_AS_OF = date(2026, 6, 8)

_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)
_USDTWD = FxPair(base=Currency.USD, quote=Currency.TWD)
_USDMYR = FxPair(base=Currency.USD, quote=Currency.MYR)


class _OneZeroRate(ProviderBase):
    """A provider that answers USD/TWD with an unusable 0 and every other pair normally.

    Deliberately shaped like the real failure: the provider does not raise and does not omit
    the pair, so the registry counts it as a SUCCESS and hands the row on. Nothing between the
    provider and the write seam looks at the value.
    """

    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [PriceRow(instrument=r.symbol, market=r.market, as_of=_AS_OF,
                         close=Decimal("100"), source=self.name) for r in instruments]

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        return self.fetch_quote_latest([instrument])

    def _rate(self, pair: FxPair) -> Decimal:
        return Decimal("0") if pair.quote is Currency.TWD else Decimal("4.4")

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        return [FxRow(base=p.base, quote=p.quote, as_of=_AS_OF, rate=self._rate(p),
                      source=self.name) for p in pairs]

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        """Two days per pair, so a pair can land one day and lose another."""
        return [
            FxRow(base=pair.base, quote=pair.quote, as_of=date(2026, 6, 5),
                  rate=Decimal("31.5"), source=self.name),
            FxRow(base=pair.base, quote=pair.quote, as_of=_AS_OF, rate=self._rate(pair),
                  source=self.name),
        ]


def _registry() -> Registry:
    provider = _OneZeroRate()
    return Registry(
        providers={provider.name: provider},
        order={(DataType.QUOTE_LATEST, Market.US): [provider.name],
               (DataType.FX, None): [provider.name]},
    )


def _rates(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [(r["base"], r["quote"], r["rate"]) for r in conn.execute(
        "SELECT base, quote, rate FROM fx_rates ORDER BY base, quote, as_of_date")]


# --- the predicate has ONE owner -----------------------------------------------------

@pytest.mark.parametrize(
    ("rate", "storable"),
    [("32.5", True), ("0.000001", True), ("0", False), ("-32.5", False)],
    ids=["ordinary", "tiny", "zero", "negative"],
)
def test_storable_rate_answers_the_seams_own_question(rate: str, storable: bool) -> None:
    assert storable_rate(Decimal(rate)) is storable


def test_the_seam_still_raises_for_a_direct_caller(conn: sqlite3.Connection) -> None:
    """Behaviour unchanged: routing the guard through ``storable_rate`` is a refactor.

    The refresh is the caller that must not raise; every OTHER caller of the write seam is
    still told loudly, which is what ``tests/pricing/test_r3_fx_rate_positivity.py`` locks.
    """
    bad = FxRow(base=Currency.USD, quote=Currency.TWD, as_of=_AS_OF, rate=Decimal("0"),
                source="test")
    with pytest.raises(ValueError, match="FX rate"):
        upsert_fx(conn, [bad], fetched_at=_NOW)
    assert _rates(conn) == []


# --- refresh_quotes ------------------------------------------------------------------

def test_refresh_quotes_lands_the_good_pair_and_reports_the_bad_one(
    conn: sqlite3.Connection,
) -> None:
    """★ One unusable rate must never cost the other pairs their update."""
    summary = refresh_quotes(conn, _registry(), [_AAPL], [_USDTWD, _USDMYR], now=_NOW)
    good = get_fx(conn, Currency.USD, Currency.MYR, now=_NOW)
    assert good is not None and good.rate == Decimal("4.4")
    assert get_fx(conn, Currency.USD, Currency.TWD, now=_NOW) is None
    assert "USDMYR" in summary.ok
    # Reporting a winning source beside its own refusal would contradict itself.
    assert "USDTWD" not in summary.ok
    assert [f for f in summary.failed if "USD/TWD" in f], summary.failed


def test_refresh_quotes_still_returns_the_summary_and_keeps_the_prices(
    conn: sqlite3.Connection,
) -> None:
    """★ The abort this fixes: the prices had ALREADY landed when ``upsert_fx`` raised.

    The run was left half-applied with no summary at all — so the scheduler could not record
    which pair failed, and the next run had no way to know the prices were already current.
    """
    summary = refresh_quotes(conn, _registry(), [_AAPL], [_USDTWD], now=_NOW)
    price = get_latest_price(conn, "AAPL", now=_NOW)
    assert price is not None and price.value == Decimal("100")
    assert summary.ok == {"AAPL": "fake"}
    assert len(summary.failed) == 1


def test_the_fx_refusal_is_written_for_the_owner(conn: sqlite3.Connection) -> None:
    """``failed`` is joined verbatim into ``job_runs.detail``; English never reaches it."""
    summary = refresh_quotes(conn, _registry(), [], [_USDTWD], now=_NOW)
    (entry,) = summary.failed
    assert any("一" <= ch <= "鿿" for ch in entry), entry
    assert entry.startswith("USD/TWD"), entry      # which pair, or the line is useless
    assert "0" in entry                            # and what value was refused


def test_a_clean_fx_batch_reports_nothing_failed(conn: sqlite3.Connection) -> None:
    """Control: the pre-filter is invisible when every rate is usable."""
    summary = refresh_quotes(conn, _registry(), [_AAPL], [_USDMYR], now=_NOW)
    assert summary.failed == []
    assert summary.ok == {"AAPL": "fake", "USDMYR": "fake"}


# --- refresh_fx_history --------------------------------------------------------------

def test_refresh_fx_history_pre_filters_the_same_way(conn: sqlite3.Connection) -> None:
    summary = refresh_fx_history(conn, _registry(), [_USDTWD, _USDMYR], date(2026, 6, 1),
                                 now=_NOW)
    assert _rates(conn) == [
        ("USD", "MYR", "31.5"), ("USD", "MYR", "4.4"), ("USD", "TWD", "31.5"),
    ]
    assert [f for f in summary.failed if "USD/TWD" in f], summary.failed


def test_a_pair_that_landed_its_other_days_stays_in_ok(conn: sqlite3.Connection) -> None:
    """It DID update, partially — calling that a total failure would be its own wrong number.

    Same rule the price pre-filter already applies to a history backfill with one bad day.
    """
    summary = refresh_fx_history(conn, _registry(), [_USDTWD], date(2026, 6, 1), now=_NOW)
    on_the_good_day = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW, max_age_days=30)
    assert on_the_good_day is not None and on_the_good_day.rate == Decimal("31.5")
    assert summary.ok == {"USDTWD": "fake"}
    assert len(summary.failed) == 1
