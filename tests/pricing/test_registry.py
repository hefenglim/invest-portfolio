from datetime import date
from decimal import Decimal

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.shared.enums import Currency, Market


def _row(sym: str, src: str) -> PriceRow:
    return PriceRow(instrument=sym, market=Market.US, as_of=date(2026, 6, 8),
                    close=Decimal("100"), source=src)


class Unsupported(ProviderBase):
    name = "unsupported"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return False

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise AssertionError("must not be called")


class Boom(ProviderBase):
    name = "boom"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise RuntimeError("provider down")


class OnlyAAPL(ProviderBase):
    name = "only_aapl"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [_row(r.symbol, self.name) for r in instruments if r.symbol == "AAPL"]


class All(ProviderBase):
    name = "all"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [_row(r.symbol, self.name) for r in instruments]


_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)
_MSFT = InstrumentRef(symbol="MSFT", market=Market.US)


def test_skips_unsupported_and_falls_back_on_exception() -> None:
    reg = Registry(
        providers={"unsupported": Unsupported(), "boom": Boom(), "all": All()},
        order={(DataType.QUOTE_LATEST, Market.US): ["unsupported", "boom", "all"]},
    )
    rows, sources, failed = reg.fetch_quote_latest([_AAPL, _MSFT])
    assert {r.instrument for r in rows} == {"AAPL", "MSFT"}
    assert sources == {"AAPL": "all", "MSFT": "all"} and failed == []


def test_partial_then_fallback_fills_remainder() -> None:
    reg = Registry(
        providers={"only_aapl": OnlyAAPL(), "all": All()},
        order={(DataType.QUOTE_LATEST, Market.US): ["only_aapl", "all"]},
    )
    rows, sources, failed = reg.fetch_quote_latest([_AAPL, _MSFT])
    assert sources == {"AAPL": "only_aapl", "MSFT": "all"} and failed == []


def test_all_fail_records_failed() -> None:
    reg = Registry(providers={"boom": Boom()},
                   order={(DataType.QUOTE_LATEST, Market.US): ["boom"]})
    rows, sources, failed = reg.fetch_quote_latest([_AAPL])
    assert rows == [] and sources == {} and failed == ["AAPL"]


def test_no_configured_order_means_all_failed() -> None:
    reg = Registry(providers={}, order={})
    rows, sources, failed = reg.fetch_quote_latest([_AAPL])
    assert failed == ["AAPL"]


def test_fetch_fx_chain() -> None:
    class FxAll(ProviderBase):
        name = "fxall"

        def supports(self, data_type: DataType, market: Market | None) -> bool:
            return data_type is DataType.FX

        def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
            return [FxRow(base=p.base, quote=p.quote, as_of=date(2026, 6, 8),
                          rate=Decimal("31.5"), source=self.name) for p in pairs]

    reg = Registry(providers={"fxall": FxAll()},
                   order={(DataType.FX, None): ["fxall"]})
    pair = FxPair(base=Currency.USD, quote=Currency.TWD)
    rows, sources, failed = reg.fetch_fx([pair])
    assert len(rows) == 1 and sources == {"USDTWD": "fxall"} and failed == []
