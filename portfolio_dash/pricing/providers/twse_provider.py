from datetime import date
from decimal import Decimal
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"


def _roc_to_date(roc: str) -> date:
    y, m, d = roc.split("/")
    return date(int(y) + 1911, int(m), int(d))


class TwseProvider(ProviderBase):
    name = "twse"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_LATEST and market is Market.TW

    def _parse(self, payload: dict[str, Any], *, instrument: str) -> PriceRow | None:
        if payload.get("stat") != "OK" or not payload.get("data"):
            return None
        row = payload["data"][-1]
        return PriceRow(
            instrument=instrument,
            market=Market.TW,
            as_of=_roc_to_date(row[0]),
            close=Decimal(str(row[6]).replace(",", "")),
            source=self.name,
        )

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        today = date.today().strftime("%Y%m%d")
        for ref in instruments:
            resp = requests.get(
                _URL,
                params={"response": "json", "date": today, "stockNo": ref.symbol},
                timeout=15,
            )
            resp.raise_for_status()
            parsed = self._parse(resp.json(), instrument=ref.symbol)
            if parsed is not None:
                out.append(parsed)
        return out
