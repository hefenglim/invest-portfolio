from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.shared.enums import Market

_OrderKey = tuple[DataType, Market | None]


class Registry:
    """Config-ordered, capability-aware fallback chain over providers.

    Constructed with a name->provider map and an order map
    ``(DataType, Market | None) -> [provider_name, ...]``. For each request,
    walks the configured, ``supports``-filtered providers in order; each
    provider fills whatever items are still missing, exceptions/empty results
    fall through to the next provider, and leftovers are recorded as failed.
    Records the winning provider name per item.
    """

    def __init__(self, providers: dict[str, ProviderBase],
                 order: dict[_OrderKey, list[str]]) -> None:
        self._providers = providers
        self._order = order

    def _chain(self, data_type: DataType, market: Market | None) -> list[ProviderBase]:
        out: list[ProviderBase] = []
        for name in self._order.get((data_type, market), []):
            p = self._providers.get(name)
            if p is not None and p.supports(data_type, market):
                out.append(p)
        return out

    def fetch_quote_latest(
        self, instruments: list[InstrumentRef],
    ) -> tuple[list[PriceRow], dict[str, str], list[str]]:
        rows: list[PriceRow] = []
        sources: dict[str, str] = {}
        failed: list[str] = []
        by_market: dict[Market, dict[str, InstrumentRef]] = {}
        for ref in instruments:
            by_market.setdefault(ref.market, {})[ref.symbol] = ref
        for market, remaining in by_market.items():
            for provider in self._chain(DataType.QUOTE_LATEST, market):
                if not remaining:
                    break
                try:
                    got = provider.fetch_quote_latest(list(remaining.values()))
                except Exception:  # noqa: BLE001 - any provider failure -> fall back
                    continue
                for row in got:
                    if row.instrument in remaining:
                        rows.append(row)
                        sources[row.instrument] = provider.name
                        del remaining[row.instrument]
            failed.extend(remaining.keys())
        return rows, sources, failed

    def fetch_fx(
        self, pairs: list[FxPair],
    ) -> tuple[list[FxRow], dict[str, str], list[str]]:
        rows: list[FxRow] = []
        sources: dict[str, str] = {}
        remaining: dict[str, FxPair] = {f"{p.base.value}{p.quote.value}": p for p in pairs}
        for provider in self._chain(DataType.FX, None):
            if not remaining:
                break
            try:
                got = provider.fetch_fx(list(remaining.values()))
            except Exception:  # noqa: BLE001
                continue
            for row in got:
                key = f"{row.base.value}{row.quote.value}"
                if key in remaining:
                    rows.append(row)
                    sources[key] = provider.name
                    del remaining[key]
        return rows, sources, list(remaining.keys())
