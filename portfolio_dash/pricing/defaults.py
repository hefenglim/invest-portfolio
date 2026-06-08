"""Default per-(data_type, market) provider order and a ready-to-use registry.

The provider order lives here as a module constant (config-as-code): it is
small, rarely changes, and is far easier to read/edit/diff than a tuple-keyed
entry in pydantic ``Settings``. A settings-page override is a possible future
enhancement, not a need now (`stack.md` — default answer to new config surface
is no until it's justified).
"""

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.shared.enums import Market

DEFAULT_PROVIDER_ORDER: dict[tuple[DataType, Market | None], list[str]] = {
    (DataType.QUOTE_LATEST, Market.US): ["yfinance"],
    (DataType.QUOTE_LATEST, Market.TW): ["twse", "tpex", "yfinance"],
    (DataType.QUOTE_LATEST, Market.MY): ["yfinance"],
    (DataType.QUOTE_HISTORY, Market.US): ["yfinance"],
    (DataType.QUOTE_HISTORY, Market.TW): ["yfinance"],
    (DataType.QUOTE_HISTORY, Market.MY): ["yfinance"],
    (DataType.FX, None): ["yfinance"],
    (DataType.DIVIDEND, Market.TW): ["finmind", "yfinance"],
    (DataType.DIVIDEND, Market.US): ["yfinance"],
    (DataType.DIVIDEND, Market.MY): ["yfinance"],
}


def default_registry() -> Registry:
    """Build the production `Registry` wired with the default provider order."""
    providers = {
        "yfinance": YFinanceProvider(),
        "twse": TwseProvider(),
        "tpex": TpexProvider(),
        "finmind": FinMindProvider(),
    }
    return Registry(providers=providers, order=DEFAULT_PROVIDER_ORDER)
