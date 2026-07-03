"""Unit: FX-history backfill (registry routing + idempotent upsert), R4 item 2."""

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_fx_history
from portfolio_dash.pricing.refs import FxPair
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import get_fx_history
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 7, 3, tzinfo=UTC)
_PAIR = FxPair(base=Currency.USD, quote=Currency.TWD)


class _FxHistProvider(ProviderBase):
    name = "fakefx"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.FX

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        return [
            FxRow(base=pair.base, quote=pair.quote, as_of=date(2026, 6, d),
                  rate=Decimal("32") + Decimal(d) / 100, source=self.name)
            for d in (1, 2, 3)
        ]


def _registry() -> Registry:
    return Registry(providers={"fakefx": _FxHistProvider()},
                    order={(DataType.FX, None): ["fakefx"]})


def test_refresh_fx_history_upserts_and_summarizes(conn: sqlite3.Connection) -> None:
    s = refresh_fx_history(conn, _registry(), [_PAIR], date(2026, 6, 1), now=_NOW)
    assert s.ok == {"USDTWD": "fakefx"} and s.failed == []
    hist = get_fx_history(conn, Currency.USD, Currency.TWD,
                          date(2026, 6, 1), date(2026, 6, 30))
    assert [r.as_of.day for r in hist] == [1, 2, 3]
    assert hist[0].rate == Decimal("32.01")
    # idempotent: a second run does not duplicate rows
    refresh_fx_history(conn, _registry(), [_PAIR], date(2026, 6, 1), now=_NOW)
    hist2 = get_fx_history(conn, Currency.USD, Currency.TWD,
                           date(2026, 6, 1), date(2026, 6, 30))
    assert len(hist2) == 3


def test_refresh_fx_history_records_failed_pairs(conn: sqlite3.Connection) -> None:
    empty = Registry(providers={}, order={(DataType.FX, None): []})
    s = refresh_fx_history(conn, empty, [_PAIR], date(2026, 6, 1), now=_NOW)
    assert s.ok == {} and s.failed == ["USDTWD"]
