"""QA-09 — the single PRICE write seam must refuse a non-positive close.

``upsert_fx`` refuses a non-positive rate 25 lines below (R3 / QA-13, and
``tests/pricing/test_r3_fx_rate_positivity.py`` locks it); ``upsert_prices`` stored whatever
it was handed. The asymmetry matters because of what a stored ``0`` DOES: the holding renders
``market_price=0``, ``market_value=0``, ``unrealized_pnl=-cost`` and ``price_stale=False`` —
a total loss **indistinguishable from a real quote**. A negative close is worse still: it
sign-flips the position's value and raises at no seam at all. Neither is a price, and
``data-and-pricing.md`` is explicit that a value the reader may not use is not a value the
writer may store ("never silently fabricate"; "a stale price is labelled, never guessed").

The refresh must nevertheless stay graceful (`data-and-pricing.md`: a failed/stale fetch
degrades, never crashes). ``refresh_quotes`` / ``refresh_history`` upsert a whole provider
batch in ONE call, so they pre-filter the offending rows and report them in
``RefreshSummary.failed`` — one bad row must never cost the other symbols their update, and
the refused symbol keeps its last-known price with the staleness flag.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import get_latest_price, upsert_prices
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_AS_OF = date(2026, 6, 8)


def _price(close: str, *, instrument: str = "AAPL") -> PriceRow:
    return PriceRow(instrument=instrument, market=Market.US, as_of=_AS_OF,
                    close=Decimal(close), source="test")


def _stored(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["instrument"], r["close"])
            for r in conn.execute("SELECT instrument, close FROM prices ORDER BY instrument")]


# --- the seam ----------------------------------------------------------------------

def test_upsert_prices_rejects_a_zero_close(conn: sqlite3.Connection) -> None:
    """The loud-looking one: 0 renders as a total loss with ``price_stale`` FALSE."""
    with pytest.raises(ValueError, match="close"):
        upsert_prices(conn, [_price("0")], fetched_at=_NOW)
    assert _stored(conn) == []


def test_upsert_prices_rejects_a_negative_close(conn: sqlite3.Connection) -> None:
    """The silent one: a negative close sign-flips the position and raises nowhere."""
    with pytest.raises(ValueError, match="close"):
        upsert_prices(conn, [_price("-5")], fetched_at=_NOW)
    assert _stored(conn) == []
    assert get_latest_price(conn, "AAPL", now=_NOW) is None


def test_upsert_prices_rejects_the_whole_batch_when_one_row_is_bad(
    conn: sqlite3.Connection,
) -> None:
    """Validated BEFORE the write, exactly like ``upsert_fx`` — no half-applied batch."""
    with pytest.raises(ValueError, match="close"):
        upsert_prices(conn, [_price("120", instrument="MSFT"), _price("0")], fetched_at=_NOW)
    assert _stored(conn) == []


def test_upsert_prices_names_the_instrument_the_date_and_the_value(
    conn: sqlite3.Connection,
) -> None:
    """A refusal the operator cannot act on is a crash with better manners."""
    with pytest.raises(ValueError) as exc:
        upsert_prices(conn, [_price("-5")], fetched_at=_NOW)
    message = str(exc.value)
    assert "AAPL" in message and "2026-06-08" in message and "-5" in message


def test_upsert_prices_still_stores_an_ordinary_close(conn: sqlite3.Connection) -> None:
    """Control: the guard rejects only what no reader could have used."""
    upsert_prices(conn, [_price("0.005")], fetched_at=_NOW)   # the sub-RM1 MY tick
    read = get_latest_price(conn, "AAPL", now=_NOW)
    assert read is not None and read.value == Decimal("0.005")


# --- the refresh stays graceful -----------------------------------------------------

class _OneZero(ProviderBase):
    """A provider that answers with one unusable close among good ones."""

    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [
            PriceRow(instrument=r.symbol, market=r.market, as_of=_AS_OF,
                     close=Decimal("0") if r.symbol == "HALT" else Decimal("100"),
                     source=self.name)
            for r in instruments
        ]

    def fetch_quote_history(
        self, instrument: InstrumentRef, start: date,
    ) -> list[PriceRow]:
        return self.fetch_quote_latest([instrument])

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        return [FxRow(base=p.base, quote=p.quote, as_of=_AS_OF, rate=Decimal("31.5"),
                      source=self.name) for p in pairs]


def _registry() -> Registry:
    provider = _OneZero()
    return Registry(
        providers={provider.name: provider},
        order={(DataType.QUOTE_LATEST, Market.US): [provider.name],
               (DataType.QUOTE_HISTORY, Market.US): [provider.name],
               (DataType.FX, None): [provider.name]},
    )


_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)
_HALT = InstrumentRef(symbol="HALT", market=Market.US)
_PAIR = FxPair(base=Currency.USD, quote=Currency.TWD)


def test_refresh_quotes_lands_the_good_rows_and_reports_the_bad_symbol(
    conn: sqlite3.Connection,
) -> None:
    """★ One unusable close must never cost the rest of the batch its update."""
    summary = refresh_quotes(conn, _registry(), [_AAPL, _HALT], [_PAIR], now=_NOW)
    good = get_latest_price(conn, "AAPL", now=_NOW)
    assert good is not None and good.value == Decimal("100")
    assert get_latest_price(conn, "HALT", now=_NOW) is None
    assert [f for f in summary.failed if f.startswith("HALT")], summary.failed
    assert "0" in "".join(summary.failed)


def test_refresh_quotes_failure_entry_is_written_for_the_owner(
    conn: sqlite3.Connection,
) -> None:
    """``failed`` is rendered verbatim into ``job_runs.detail``; English never reaches it."""
    summary = refresh_quotes(conn, _registry(), [_HALT], [_PAIR], now=_NOW)
    (entry,) = [f for f in summary.failed if f.startswith("HALT")]
    assert any("一" <= ch <= "鿿" for ch in entry), entry


def test_refresh_history_pre_filters_the_same_way(conn: sqlite3.Connection) -> None:
    summary = refresh_history(conn, _registry(), [_AAPL, _HALT], _AS_OF, now=_NOW)
    assert get_latest_price(conn, "AAPL", now=_NOW) is not None
    assert get_latest_price(conn, "HALT", now=_NOW) is None
    assert [f for f in summary.failed if f.startswith("HALT")], summary.failed


def test_a_clean_batch_reports_nothing_failed(conn: sqlite3.Connection) -> None:
    """Control: the pre-filter is invisible when every row is usable."""
    summary = refresh_quotes(conn, _registry(), [_AAPL], [_PAIR], now=_NOW)
    assert summary.failed == []
    assert summary.ok == {"AAPL": "fake", "USDTWD": "fake"}
