"""Default per-(data_type, market) provider order and a ready-to-use registry.

The provider order lives here as a module constant (config-as-code): it is
small, rarely changes, and is far easier to read/edit/diff than a tuple-keyed
entry in pydantic ``Settings``. A settings-page override is a possible future
enhancement, not a need now (`stack.md` — default answer to new config surface
is no until it's justified).
"""

import sqlite3
from collections.abc import Callable

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.alphavantage_provider import AlphaVantageProvider
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.providers.finnhub_provider import FinnhubProvider
from portfolio_dash.pricing.providers.klsescreener_provider import KlseScreenerProvider
from portfolio_dash.pricing.providers.malaysiastock_provider import MalaysiaStockProvider
from portfolio_dash.pricing.providers.stockprices_dev_provider import StockPricesDevProvider
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.providers.twstock_provider import TwStockProvider
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.shared.enums import Market

DEFAULT_PROVIDER_ORDER: dict[tuple[DataType, Market | None], list[str]] = {
    # Free spec-20.8 fallbacks appended to each market's QUOTE_LATEST chain.
    (DataType.QUOTE_LATEST, Market.US): ["yfinance", "stockprices_dev"],
    (DataType.QUOTE_LATEST, Market.TW): ["twse", "tpex", "yfinance", "twstock"],
    (DataType.QUOTE_LATEST, Market.MY): ["yfinance", "klsescreener", "malaysiastock"],
    (DataType.QUOTE_HISTORY, Market.US): ["yfinance"],
    (DataType.QUOTE_HISTORY, Market.TW): ["yfinance"],
    (DataType.QUOTE_HISTORY, Market.MY): ["yfinance"],
    (DataType.FX, None): ["yfinance"],
    (DataType.DIVIDEND, Market.TW): ["finmind", "yfinance"],
    (DataType.DIVIDEND, Market.US): ["yfinance"],
    (DataType.DIVIDEND, Market.MY): ["yfinance"],
}


def default_registry(conn: sqlite3.Connection | None = None) -> Registry:
    """Build the production `Registry` wired with the default provider order.

    When ``conn`` is given (the live path — scheduler refresh jobs), keyed
    providers read their API key from the ``data_sources`` table at call time, so
    a key set on the settings page takes effect on the next fetch (spec 14.2,
    review I-3). FinMind is wired with a DB-backed ``token_getter`` here. When
    ``conn`` is ``None`` (standalone / tests), the providers keep their env/ctor
    fallback so zero-arg callers are unaffected.

    The ``datasources_store`` import is function-local to avoid a circular import
    (``datasources_store`` imports ``DEFAULT_PROVIDER_ORDER`` from this module).
    """
    if conn is not None:
        from portfolio_dash.pricing import datasources_store

        def _getter(source_id: str) -> Callable[[], str | None]:
            return lambda: datasources_store.get_api_key(conn, source_id)

        finmind = FinMindProvider(token_getter=_getter("finmind"))
        alphavantage = AlphaVantageProvider(token_getter=_getter("alphavantage"))
        finnhub = FinnhubProvider(token_getter=_getter("finnhub"))
    else:
        finmind = FinMindProvider()
        alphavantage = AlphaVantageProvider()
        finnhub = FinnhubProvider()
    providers: dict[str, ProviderBase] = {
        "yfinance": YFinanceProvider(),
        "twse": TwseProvider(),
        "tpex": TpexProvider(),
        "finmind": finmind,
        # Free spec-20.8 quote fallbacks (key-less; inert until their chain slot is hit).
        "twstock": TwStockProvider(),
        "stockprices_dev": StockPricesDevProvider(),
        "klsescreener": KlseScreenerProvider(),
        "malaysiastock": MalaysiaStockProvider(),
        # Pending spec-20.9 token-gated adapters: registered but NOT in the default
        # order, and key-gated (supports is False without a key) -> inert until a key
        # is set + the source is promoted into a chain.
        "alphavantage": alphavantage,
        "finnhub": finnhub,
    }
    return Registry(providers=providers, order=DEFAULT_PROVIDER_ORDER)
