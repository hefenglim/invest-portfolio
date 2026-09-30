import re
from datetime import date
from decimal import Decimal
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.enums import Market

_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"

# The STOCK_DAY title reads 「115年06月 2330 台積電           各日成交資訊」: the exchange's own
# short name sits between the code and the fixed suffix. Owner 2026-09-30, item 8: registration
# keeps it as an alias (大立光 for a 3008 registered as LARGAN), read from the SAME response the
# board probe already fetched — never a second request.
_TITLE = re.compile(r"^\s*\d+年\d+月\s+(\S+)\s+(.+?)\s*各日成交資訊\s*$")


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

    def _parse_name(self, payload: dict[str, Any], *, instrument: str) -> str | None:
        """The exchange's short name from the payload title, or None when absent.

        The code inside the title must be *instrument* — a title for another code (or a
        re-worded title) yields None rather than a name that belongs to something else.
        """
        title = payload.get("title")
        if not isinstance(title, str):
            return None
        m = _TITLE.match(title)
        if m is None or m.group(1) != instrument:
            return None
        return m.group(2).strip() or None

    def _fetch(self, symbol: str, day: str) -> dict[str, Any]:
        resp = requests.get(
            _URL,
            params={"response": "json", "date": day, "stockNo": symbol},
            timeout=15,
        )
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload

    def fetch_quote_named(self, instrument: InstrumentRef) -> tuple[PriceRow | None, str | None]:
        """ONE request → the latest close and the exchange's short name (board probe)."""
        payload = self._fetch(instrument.symbol, app_now().date().strftime("%Y%m%d"))
        row = self._parse(payload, instrument=instrument.symbol)
        return row, (self._parse_name(payload, instrument=instrument.symbol)
                     if row is not None else None)

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        today = app_now().date().strftime("%Y%m%d")
        for ref in instruments:
            parsed = self._parse(self._fetch(ref.symbol, today), instrument=ref.symbol)
            if parsed is not None:
                out.append(parsed)
        return out
