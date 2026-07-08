from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import yfinance as yf

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.shared.enums import Currency, Market

_SUFFIX = {Market.US: "", Market.TW: ".TW", Market.MY: ".KL"}
_DIV_CCY = {Market.US: Currency.USD, Market.TW: Currency.TWD, Market.MY: Currency.MYR}


def _finite(value: object) -> Decimal | None:
    """Return Decimal(value) if finite, else None — filters yfinance NaN/inf/None gaps."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _volume(value: object) -> Decimal | None:
    """Volume as an integer Decimal — NOT money, so the 2-dp rule never applies.

    Reuses ``_finite`` to reject NaN/inf/None gaps, then normalizes to a whole
    integer (yfinance emits volume as float64, e.g. ``3323800.0``). A genuine 0
    (a no-trade session) stays 0; only missing/NaN becomes None.
    """
    d = _finite(value)
    return Decimal(int(d)) if d is not None else None


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
        rows: list[PriceRow] = []
        volumes = payload.get("Volume", {})
        for ts_ms, close in payload.get("Close", {}).items():
            fc = _finite(close)
            if fc is None:
                continue
            d = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date()
            rows.append(PriceRow(instrument=instrument, market=market, as_of=d,
                                 close=fc, volume=_volume(volumes.get(ts_ms)),
                                 source=self.name))
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            df = yf.Ticker(yf_symbol(ref)).history(period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            volumes = df["Volume"] if "Volume" in df.columns else None
            # most recent row with a finite close (today's row can be NaN intraday)
            for ts, close in reversed(list(df["Close"].items())):
                fc = _finite(close)
                if fc is not None:
                    vol = _volume(volumes.get(ts)) if volumes is not None else None
                    out.append(PriceRow(instrument=ref.symbol, market=ref.market,
                                        as_of=ts.date(), close=fc, volume=vol,
                                        source=self.name))
                    break
        return out

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        df = yf.Ticker(yf_symbol(instrument)).history(start=start.isoformat(), auto_adjust=False)
        if df is None or df.empty:
            return []
        volumes = df["Volume"] if "Volume" in df.columns else None
        rows: list[PriceRow] = []
        for ts, close in df["Close"].items():
            fc = _finite(close)
            if fc is None:
                continue
            vol = _volume(volumes.get(ts)) if volumes is not None else None
            rows.append(PriceRow(instrument=instrument.symbol, market=instrument.market,
                                 as_of=ts.date(), close=fc, volume=vol, source=self.name))
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        out: list[FxRow] = []
        for p in pairs:
            df = yf.Ticker(f"{p.base.value}{p.quote.value}=X").history(
                period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            for ts, rate in reversed(list(df["Close"].items())):
                fr = _finite(rate)
                if fr is not None:
                    out.append(FxRow(base=p.base, quote=p.quote, as_of=ts.date(),
                                     rate=fr, source=self.name))
                    break
        return out

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        df = yf.Ticker(f"{pair.base.value}{pair.quote.value}=X").history(
            start=start.isoformat(), auto_adjust=False)
        if df is None or df.empty:
            return []
        rows: list[FxRow] = []
        for ts, rate in df["Close"].items():
            fr = _finite(rate)
            if fr is None:
                continue
            rows.append(FxRow(base=pair.base, quote=pair.quote, as_of=ts.date(),
                              rate=fr, source=self.name))
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        out: list[DividendEvent] = []
        for ref in instruments:
            series = yf.Ticker(yf_symbol(ref)).dividends
            for ex, amount in series.items():
                amt = _finite(amount)
                if amt is None:
                    continue
                out.append(DividendEvent(instrument=ref.symbol, market=ref.market,
                                         ex_date=ex.date(), cash_amount=amt,
                                         currency=_DIV_CCY[ref.market], source=self.name))
        return out
