from datetime import date

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.shared.enums import Market


class ProviderBase:
    name: str = "base"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return False

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise NotImplementedError

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        raise NotImplementedError

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        raise NotImplementedError

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        raise NotImplementedError

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        raise NotImplementedError
