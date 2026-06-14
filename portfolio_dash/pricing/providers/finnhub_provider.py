"""Finnhub US quote/dividend provider — pending (spec 20.9).

pending — validated when a key is entered (spec 20.9). Catalogued + wired into
``default_registry`` but **key-gated** (``supports`` is False without a key, like
``FinMindProvider``), so it stays inert until a key is set. Numbers parse via
``Decimal(str(x))``; HTTP I/O goes through ``requests.get`` and is never exercised
without a key.
"""

import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_TIMEOUT_S = 15


class FinnhubProvider(ProviderBase):
    name = "finnhub"

    def __init__(
        self,
        token: str | None = None,
        *,
        token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._token = token if token is not None else os.environ.get("FINNHUB_KEY")
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
            and data_type in {DataType.QUOTE_LATEST, DataType.DIVIDEND}
            and market is Market.US
        )

    @staticmethod
    def _dec(value: Any) -> Decimal | None:
        if value in (None, "", 0):
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
                _QUOTE_URL, params={"symbol": ref.symbol, "token": token}, timeout=_TIMEOUT_S
            )
            resp.raise_for_status()
            close = self._dec(resp.json().get("c"))  # c = current price
            if close is None:
                continue
            out.append(PriceRow(
                instrument=ref.symbol, market=Market.US, as_of=date.today(),
                close=close, source=self.name,
            ))
        return out
