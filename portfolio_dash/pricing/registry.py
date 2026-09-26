from datetime import date

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.shared.enums import Market

_OrderKey = tuple[DataType, Market | None]


def _failure_phrase(provider: str, exc: Exception | None) -> str:
    """「<provider> 逾時／連線失敗／回應錯誤／無配息資料」 — why ONE provider gave nothing.

    Classified by type, never by printing the exception: its text is English and written for
    a developer, and this phrase is read by the owner (DEF-015).
    """
    if exc is None:
        return f"{provider} 無配息資料"
    kind = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in kind:
        return f"{provider} 逾時"
    if isinstance(exc, ConnectionError) or "connection" in kind:
        return f"{provider} 連線失敗"
    return f"{provider} 回應錯誤"


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

    def capable_ids(self, data_type: DataType, market: Market | None) -> list[str]:
        """Provider ids whose ``supports`` says yes — the settings page's pick list
        for the per-market order editor (capability probe only, no network)."""
        return sorted(
            name for name, p in self._providers.items() if p.supports(data_type, market)
        )

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

    def fetch_quote_history(
        self, instruments: list[InstrumentRef], start: date,
    ) -> tuple[list[PriceRow], dict[str, str], list[str]]:
        """Routed daily history; ``failed`` = every symbol no provider returned bars for.

        Unchanged contract: an empty answer and an exception both land in ``failed``. A
        caller that must tell them apart reads :meth:`fetch_quote_history_explained`.
        """
        rows, sources, failed, empty = self.fetch_quote_history_explained(instruments, start)
        return rows, sources, [r.symbol for r in instruments if r.symbol in {*failed, *empty}]

    def fetch_quote_history_explained(
        self, instruments: list[InstrumentRef], start: date,
    ) -> tuple[list[PriceRow], dict[str, str], list[str], list[str]]:
        """:meth:`fetch_quote_history` with the empty answers set apart (DEF-067, 2026-09-26).

        Returns ``(rows, sources, failed, empty)``. Same rule as
        :meth:`fetch_dividends_explained` (DEF-047): the chain still falls through on an
        empty answer, a symbol is ``empty`` only when NO provider returned bars and AT LEAST
        ONE answered without raising, and ``failed`` keeps the rest (every provider raised,
        or the market has no history provider at all).

        ⚠ An empty answer is not proof the provider was reached: yfinance logs a network
        failure and returns an empty frame instead of raising. Whether an ``empty`` symbol
        may be trusted as "no bars in the window" is therefore the CALLER's question, with
        the evidence of the whole run (``scheduler.jobs.history_daily``).
        """
        rows: list[PriceRow] = []
        sources: dict[str, str] = {}
        failed: list[str] = []
        empty: list[str] = []
        for ref in instruments:
            filled = False
            answered = False
            for provider in self._chain(DataType.QUOTE_HISTORY, ref.market):
                try:
                    got = provider.fetch_quote_history(ref, start)
                except Exception:  # noqa: BLE001 - any provider failure -> fall back
                    continue
                if got:
                    rows.extend(got)
                    sources[ref.symbol] = provider.name
                    filled = True
                    break
                answered = True
            if filled:
                continue
            (empty if answered else failed).append(ref.symbol)
        return rows, sources, failed, empty

    def fetch_dividends(
        self, instruments: list[InstrumentRef],
    ) -> tuple[list[DividendEvent], dict[str, str], list[str]]:
        events, sources, failed, _reasons, _empty = self.fetch_dividends_explained(instruments)
        return events, sources, failed

    def fetch_dividends_explained(
        self, instruments: list[InstrumentRef],
    ) -> tuple[list[DividendEvent], dict[str, str], list[str], dict[str, str], list[str]]:
        """:meth:`fetch_dividends` plus a zh REASON per failed symbol (DEF-015, 2026-09-23)
        and the symbols a source answered for with NO dividend records (DEF-047).

        Returns ``(events, sources, failed, reasons, empty)``.

        The fall-through used to swallow every provider exception and every empty answer
        into one bare list, so 「1 檔失敗」 was all anyone could ever say — not which symbol,
        and not whether the source timed out or simply had no dividend history. DEF-015 gave
        each failure its reason; DEF-047 (owner ruling 2026-09-24) takes the second case out
        of ``failed`` altogether: a provider that ANSWERED — no exception — with an empty
        series has told us the symbol pays no dividend (TSLA via yfinance), and that is a
        normal outcome, reported as 「無配息紀錄」 with no warning face. The chain still falls
        through on an empty answer (a later provider may have the series), so a symbol is
        ``empty`` only when NO provider returned events and AT LEAST ONE answered cleanly.
        ``failed`` keeps exactly the real failures: every provider raised, or the market has
        no dividend provider at all (「無可用的配息資料來源」 — a configuration gap, not an
        answer).
        """
        events: list[DividendEvent] = []
        sources: dict[str, str] = {}
        failed: list[str] = []
        reasons: dict[str, str] = {}
        empty: list[str] = []
        for ref in instruments:
            filled = False
            answered = False
            tried: list[str] = []
            for provider in self._chain(DataType.DIVIDEND, ref.market):
                try:
                    got = provider.fetch_dividends([ref])
                except Exception as exc:  # noqa: BLE001 - any provider failure -> fall back
                    tried.append(_failure_phrase(provider.name, exc))
                    continue
                if got:
                    events.extend(got)
                    sources[ref.symbol] = provider.name
                    filled = True
                    break
                answered = True
                tried.append(_failure_phrase(provider.name, None))
            if filled:
                continue
            if answered:
                empty.append(ref.symbol)
            else:
                failed.append(ref.symbol)
                reasons[ref.symbol] = "、".join(tried) if tried else "無可用的配息資料來源"
        return events, sources, failed, reasons, empty

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

    def fetch_fx_history(
        self, pairs: list[FxPair], start: date,
    ) -> tuple[list[FxRow], dict[str, str], list[str]]:
        """Historical daily FX rates from ``start`` per pair (mirrors quote history).

        Same graceful-degradation contract: per-pair provider fall-through, failed
        pairs recorded, never raised.
        """
        rows: list[FxRow] = []
        sources: dict[str, str] = {}
        failed: list[str] = []
        for pair in pairs:
            key = f"{pair.base.value}{pair.quote.value}"
            filled = False
            for provider in self._chain(DataType.FX, None):
                try:
                    got = provider.fetch_fx_history(pair, start)
                except Exception:  # noqa: BLE001 - any provider failure -> fall back
                    continue
                if got:
                    rows.extend(got)
                    sources[key] = provider.name
                    filled = True
                    break
            if not filled:
                failed.append(key)
        return rows, sources, failed
