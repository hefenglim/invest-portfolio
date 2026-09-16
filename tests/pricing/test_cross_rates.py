"""Derived cross rates (owner ruling 2026-09-16, demo audit M1 option (b)).

MYR/TWD is never fetched: it is USD/TWD ÷ USD/MYR, written by `pricing/cross.py` right
after every FX write, with source "derived:USD". The audit's own figures are the fixture:
USD/TWD 31.834999 and USD/MYR 4.082500 on 2026-09-15 gave a provider MYR/TWD of 7.803300
and a +0.0690% triangle gap; derived, the gap is zero by construction.
"""

import sqlite3
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.pricing.cross import (
    CROSS_RATES,
    derive_cross_rates,
    derived_source,
    fetched_pairs,
    is_derived_pair,
)
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_fx_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import _no_factor, get_fx, upsert_fx
from portfolio_dash.scheduler.jobs import REPORTING_FX_PAIRS
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_D = date(2026, 9, 15)
_USDTWD = Decimal("31.834999")
_USDMYR = Decimal("4.082500")
_Q6 = Decimal("0.000001")


def _seed_legs(conn: sqlite3.Connection, as_of: date = _D) -> None:
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=as_of, rate=_USDTWD, source="p"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=as_of, rate=_USDMYR, source="p"),
    ], fetched_at=_NOW)


def test_the_table_names_myr_twd_via_usd() -> None:
    assert [(c.base, c.quote, c.via) for c in CROSS_RATES] == [
        (Currency.MYR, Currency.TWD, Currency.USD)]
    assert is_derived_pair(Currency.MYR, Currency.TWD)
    assert not is_derived_pair(Currency.USD, Currency.TWD)
    assert derived_source(Currency.USD) == "derived:USD"


def test_derives_myr_twd_from_the_two_usd_legs_and_closes_the_triangle(
    conn: sqlite3.Connection,
) -> None:
    _seed_legs(conn)
    written = derive_cross_rates(conn, fetched_at=_NOW)
    assert [(r.base, r.quote, r.as_of) for r in written] == [(Currency.MYR, Currency.TWD, _D)]
    read = get_fx(conn, Currency.MYR, Currency.TWD, now=_NOW)
    assert read is not None
    assert read.source == "derived:USD"
    assert read.rate == (_USDTWD / _USDMYR).quantize(_Q6, rounding=ROUND_HALF_UP)
    # The guard the dashboard now applies: implied == direct to the stored precision.
    implied = (_USDMYR * read.rate).quantize(_Q6, rounding=ROUND_HALF_UP)
    assert abs(implied - _USDTWD) <= Decimal("0.000005")


def test_a_provider_row_for_the_derived_pair_is_replaced(conn: sqlite3.Connection) -> None:
    """The audit's 7.803300 (yfinance MYRTWD=X) must not survive the first refresh."""
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=_D,
                           rate=Decimal("7.803300"), source="yfinance")], fetched_at=_NOW)
    _seed_legs(conn)
    derive_cross_rates(conn, fetched_at=_NOW)
    read = get_fx(conn, Currency.MYR, Currency.TWD, now=_NOW)
    assert read is not None and read.source == "derived:USD"
    assert read.rate != Decimal("7.803300")


def test_a_missing_leg_derives_nothing_and_never_fabricates(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=_D, rate=_USDTWD,
                           source="p")], fetched_at=_NOW)
    assert derive_cross_rates(conn, fetched_at=_NOW) == []
    assert get_fx(conn, Currency.MYR, Currency.TWD, now=_NOW) is None


def test_whole_history_is_derived_not_just_today(conn: sqlite3.Connection) -> None:
    for d in (date(2026, 9, 12), date(2026, 9, 15)):
        _seed_legs(conn, d)
    written = derive_cross_rates(conn, fetched_at=_NOW)
    assert sorted(r.as_of for r in written) == [date(2026, 9, 12), date(2026, 9, 15)]


def test_reporting_pairs_never_ask_a_provider_for_the_derived_pair() -> None:
    assert (Currency.MYR, Currency.TWD) not in {(p.base, p.quote) for p in REPORTING_FX_PAIRS}
    assert fetched_pairs([FxPair(base=Currency.MYR, quote=Currency.TWD)]) == []


class _UsdLegs(ProviderBase):
    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return []

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        return [FxRow(base=p.base, quote=p.quote, as_of=_D,
                      rate=_USDTWD if p.quote is Currency.TWD else _USDMYR, source="fake")
                for p in pairs]

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        return self.fetch_fx([pair])


def _reg() -> Registry:
    p = _UsdLegs()
    return Registry(
        providers={"fake": p},
        order={(DataType.FX, None): ["fake"],
               **{(DataType.QUOTE_LATEST, m): ["fake"] for m in Market}},
    )


def test_refresh_quotes_derives_after_the_fetch_and_reports_it(conn: sqlite3.Connection) -> None:
    summary = refresh_quotes(conn, _reg(), [], REPORTING_FX_PAIRS, now=_NOW,
                             factor_of=_no_factor)
    assert summary.ok.get("MYRTWD") == "derived:USD"
    read = get_fx(conn, Currency.MYR, Currency.TWD, now=_NOW)
    assert read is not None and read.source == "derived:USD"


def test_refresh_fx_history_derives_too(conn: sqlite3.Connection) -> None:
    summary = refresh_fx_history(conn, _reg(), REPORTING_FX_PAIRS, date(2026, 9, 1), now=_NOW)
    assert summary.ok.get("MYRTWD") == "derived:USD"
    assert get_fx(conn, Currency.MYR, Currency.TWD, now=_NOW) is not None
