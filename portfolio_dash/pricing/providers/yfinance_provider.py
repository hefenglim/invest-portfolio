from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import yfinance as yf

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.shared.enums import Market

_SUFFIX = {Market.US: "", Market.TW: ".TW", Market.MY: ".KL"}


def yf_symbol(ref: InstrumentRef) -> str:
    if ref.market is Market.TW and ref.board == "TPEx":
        return f"{ref.symbol}.TWO"
    return f"{ref.symbol}{_SUFFIX[ref.market]}"


class YFinanceProvider(ProviderBase):
    name = "yfinance"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type in {
            DataType.QUOTE_LATEST, DataType.QUOTE_HISTORY, DataType.FX, DataType.DIVIDEND,
        }

    def _parse_history_json(
        self, payload: dict[str, Any], *, instrument: str, market: Market,
    ) -> list[PriceRow]:
        closes = payload.get("Close", {})
        rows: list[PriceRow] = []
        for ts_ms, close in closes.items():
            if close is None:
                continue
            d = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date()
            rows.append(PriceRow(instrument=instrument, market=market, as_of=d,
                                 close=Decimal(str(close)), source=self.name))
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            df = yf.Ticker(yf_symbol(ref)).history(period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            out.append(PriceRow(instrument=ref.symbol, market=ref.market,
                                as_of=df.index[-1].date(),
                                close=Decimal(str(df["Close"].iloc[-1])), source=self.name))
        return out

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        out: list[FxRow] = []
        for p in pairs:
            df = yf.Ticker(f"{p.base.value}{p.quote.value}=X").history(
                period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            out.append(FxRow(base=p.base, quote=p.quote, as_of=df.index[-1].date(),
                             rate=Decimal(str(df["Close"].iloc[-1])), source=self.name))
        return out
