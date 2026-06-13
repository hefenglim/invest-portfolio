import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.shared.enums import Currency, Market

_URL = "https://api.finmindtrade.com/api/v4/data"


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "" or v == 0:
        return None
    return Decimal(str(v))


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
        return (
            self._resolve_token() is not None
            and data_type is DataType.DIVIDEND
            and market is Market.TW
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
                    "token": token,
                },
                timeout=20,
            )
            resp.raise_for_status()
            out.extend(self._parse_dividends(resp.json(), instrument=ref.symbol))
        return out
