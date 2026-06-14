"""Alpha Vantage US quote/history/FX provider — pending (spec 20.9).

pending — validated when a key is entered (spec 20.9). This adapter is catalogued
and wired into ``default_registry`` but, being **key-gated** (``supports`` is False
without a key — exactly like ``FinMindProvider``), it stays inert until a key is set
on the settings page. The future validation workflow (no frontend change): enter a
key -> run the probe -> promote ``status`` to ``live`` + slot it into the order.

Numbers are parsed via ``Decimal(str(x))`` (no float into the money chain). All HTTP
I/O goes through ``requests.get`` so it is never exercised without a key.
"""

import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://www.alphavantage.co/query"
_TIMEOUT_S = 20


class AlphaVantageProvider(ProviderBase):
    name = "alphavantage"

    def __init__(
        self,
        token: str | None = None,
        *,
        token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._token = token if token is not None else os.environ.get("ALPHAVANTAGE_KEY")
        self._token_getter = token_getter

    def _resolve_token(self) -> str | None:
        if self._token_getter is not None:
            token = self._token_getter()
            if token:
                return token
        return self._token

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        if self._resolve_token() is None:
            return False
        if data_type is DataType.FX:
            return True
        return (
            data_type in {DataType.QUOTE_LATEST, DataType.QUOTE_HISTORY}
            and market is Market.US
        )

    @staticmethod
    def _dec(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        token = self._resolve_token()
        if not token:
            return []
        out: list[PriceRow] = []
        for ref in instruments:
            resp = requests.get(
                _URL,
                params={"function": "GLOBAL_QUOTE", "symbol": ref.symbol, "apikey": token},
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            quote = resp.json().get("Global Quote") or {}
            close = self._dec(quote.get("05. price"))
            if close is None:
                continue
            out.append(PriceRow(
                instrument=ref.symbol, market=Market.US, as_of=date.today(),
                close=close, source=self.name,
            ))
        return out

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        token = self._resolve_token()
        if not token:
            return []
        out: list[FxRow] = []
        for pair in pairs:
            resp = requests.get(
                _URL,
                params={
                    "function": "CURRENCY_EXCHANGE_RATE",
                    "from_currency": pair.base.value,
                    "to_currency": pair.quote.value,
                    "apikey": token,
                },
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            block = resp.json().get("Realtime Currency Exchange Rate") or {}
            rate = self._dec(block.get("5. Exchange Rate"))
            if rate is None:
                continue
            out.append(FxRow(
                base=pair.base, quote=pair.quote, as_of=date.today(),
                rate=rate, source=self.name,
            ))
        return out
