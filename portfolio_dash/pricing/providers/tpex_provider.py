from datetime import date
from decimal import Decimal
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _roc_compact_to_date(roc: str) -> date:
    """Parse a compact ROC date string like "1150608" (YYYMMDD, no separators)."""
    y, m, d = int(roc[:-4]), int(roc[-4:-2]), int(roc[-2:])
    return date(y + 1911, m, d)


class TpexProvider(ProviderBase):
    name = "tpex"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_LATEST and market is Market.TW

    def _parse(self, rows: list[dict[str, Any]], *, instrument: str) -> PriceRow | None:
        for row in rows:
            if row.get("SecuritiesCompanyCode") == instrument:
                return PriceRow(
                    instrument=instrument,
                    market=Market.TW,
                    as_of=_roc_compact_to_date(str(row["Date"])),
                    close=Decimal(str(row["Close"]).replace(",", "")),
                    source=self.name,
                )
        return None

    def _parse_name(self, rows: list[dict[str, Any]], *, instrument: str) -> str | None:
        """The exchange's short name (``CompanyName``, e.g. 群聯) for *instrument*, or None.

        Owner 2026-09-30, item 8: registration keeps it as an alias, read from the SAME list
        the board probe already fetched — never a second request.
        """
        for row in rows:
            if row.get("SecuritiesCompanyCode") == instrument:
                name = row.get("CompanyName")
                if not isinstance(name, str):
                    return None
                return name.strip() or None
        return None

    def _fetch(self) -> list[dict[str, Any]]:
        resp = requests.get(_URL, timeout=15)
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()
        return rows

    def fetch_quote_named(self, instrument: InstrumentRef) -> tuple[PriceRow | None, str | None]:
        """ONE request → the latest close and the exchange's short name (board probe)."""
        rows = self._fetch()
        row = self._parse(rows, instrument=instrument.symbol)
        return row, (self._parse_name(rows, instrument=instrument.symbol)
                     if row is not None else None)

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        rows = self._fetch()
        out: list[PriceRow] = []
        for ref in instruments:
            parsed = self._parse(rows, instrument=ref.symbol)
            if parsed is not None:
                out.append(parsed)
        return out
