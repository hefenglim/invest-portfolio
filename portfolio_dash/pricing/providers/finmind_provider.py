import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import DividendEvent, PriceRow
from portfolio_dash.shared.enums import Currency, Market

_URL = "https://api.finmindtrade.com/api/v4/data"


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "" or v == 0:
        return None
    return Decimal(str(v))


def _vol(v: Any) -> Decimal | None:
    """Volume as an integer Decimal — NOT money (no 2-dp rule); a real 0 stays 0.

    Unlike ``_dec`` (which maps 0 → None for dividend/price fields), a zero
    ``Trading_Volume`` is a genuine no-trade session and is preserved; only a
    missing/blank/unparseable value degrades to None.
    """
    if v is None or v == "":
        return None
    try:
        return Decimal(int(v))
    except (ValueError, TypeError, InvalidOperation):
        return None


def _d(v: Any) -> date | None:
    if not v:
        return None
    return date.fromisoformat(str(v))


class FinMindProvider(ProviderBase):
    name = "finmind"

    def __init__(
        self,
        token: str | None = None,
        *,
        token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        """FinMind dividend provider.

        Token resolution (spec 14.2): in production the token is read at call time
        from the ``data_sources`` table via an injected ``token_getter`` (the
        DB-backed path), so a key set on the settings page takes effect on the next
        fetch without reconstructing the provider. For standalone use / back-compat,
        an explicit ``token`` arg or the ``FINMIND_TOKEN`` env var still works as a
        fallback when no getter is supplied (or it returns nothing).
        """
        self._token = token if token is not None else os.environ.get("FINMIND_TOKEN")
        self._token_getter = token_getter

    def _resolve_token(self) -> str | None:
        if self._token_getter is not None:
            token = self._token_getter()
            if token:
                return token
        return self._token

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        # TW-only, token-gated. Beyond dividends (spec 14.2), FinMind's
        # ``TaiwanStockPrice`` dataset is a QUOTE_HISTORY fallback behind yfinance
        # (batch P1-①②) — closing the "TW history is a yfinance single point" risk.
        return (
            self._resolve_token() is not None
            and market is Market.TW
            and data_type in {DataType.DIVIDEND, DataType.QUOTE_HISTORY}
        )

    def _parse_dividends(
        self, payload: dict[str, Any], *, instrument: str
    ) -> list[DividendEvent]:
        events: list[DividendEvent] = []
        for row in payload.get("data") or []:
            ex = _d(row.get("CashExDividendTradingDate"))
            if ex is None:
                continue
            events.append(
                DividendEvent(
                    instrument=instrument,
                    market=Market.TW,
                    ex_date=ex,
                    pay_date=_d(row.get("CashDividendPaymentDate")),
                    cash_amount=_dec(row.get("CashEarningsDistribution")),
                    stock_amount=_dec(row.get("StockEarningsDistribution")),
                    currency=Currency.TWD,
                    source=self.name,
                )
            )
        events.sort(key=lambda e: e.ex_date)
        return events

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        token = self._resolve_token()
        # Bearer auth (spec 20.15.1): the token is an Authorization header, not a query
        # param. ``supports`` already gated on a present token before the registry calls
        # us, so ``token`` is non-None here in practice.
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        out: list[DividendEvent] = []
        for ref in instruments:
            if ref.market is not Market.TW:
                continue
            resp = requests.get(
                _URL,
                params={
                    "dataset": "TaiwanStockDividend",
                    "data_id": ref.symbol,
                    "start_date": "2015-01-01",
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            out.extend(self._parse_dividends(resp.json(), instrument=ref.symbol))
        return out

    def _parse_quote_history(
        self, payload: dict[str, Any], *, instrument: str
    ) -> list[PriceRow]:
        """Map ``TaiwanStockPrice`` rows to ``PriceRow`` (close/open/high/low + volume).

        Live-probed shape (2026-07-08): each row carries ``date``, ``open``, ``max``,
        ``min``, ``close`` and an integer ``Trading_Volume``. FinMind's ``max``/``min``
        are the session high/low. A row without a parseable date or close is skipped
        (never fabricated).
        """
        rows: list[PriceRow] = []
        for row in payload.get("data") or []:
            d = _d(row.get("date"))
            close = _dec(row.get("close"))
            if d is None or close is None:
                continue
            rows.append(
                PriceRow(
                    instrument=instrument,
                    market=Market.TW,
                    as_of=d,
                    close=close,
                    open=_dec(row.get("open")),
                    high=_dec(row.get("max")),
                    low=_dec(row.get("min")),
                    volume=_vol(row.get("Trading_Volume")),
                    source=self.name,
                )
            )
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        """Daily TW price history from ``start`` via FinMind's ``TaiwanStockPrice``.

        Same Bearer auth + endpoint as the dividend path; ``data_id`` is the raw
        symbol (works for both TWSE and TPEx ids). Only TW instruments are served
        (``supports`` already gates this); anything else returns empty so the
        registry falls through cleanly.
        """
        if instrument.market is not Market.TW:
            return []
        token = self._resolve_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get(
            _URL,
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": instrument.symbol,
                "start_date": start.isoformat(),
            },
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        return self._parse_quote_history(resp.json(), instrument=instrument.symbol)
