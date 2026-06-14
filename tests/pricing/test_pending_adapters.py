"""Tests for the spec-20.9 pending token-gated adapters.

Alpha Vantage + Finnhub (quote/dividend providers) and FRED (macro client). Each is
constructible and **key-gated**: ``supports`` is True only for its declared data
types AND only when a key is present (mirrors FinMind). With no key, ``supports`` is
False so the registry never calls them. No network is touched in any test.
"""

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.fred_source import FredSource
from portfolio_dash.pricing.providers.alphavantage_provider import AlphaVantageProvider
from portfolio_dash.pricing.providers.finnhub_provider import FinnhubProvider
from portfolio_dash.shared.enums import Market


def test_alphavantage_key_gated_supports() -> None:
    keyed = AlphaVantageProvider(token="av-key")
    assert keyed.name == "alphavantage"
    assert keyed.supports(DataType.QUOTE_LATEST, Market.US)
    assert keyed.supports(DataType.QUOTE_HISTORY, Market.US)
    assert keyed.supports(DataType.FX, None)
    assert not keyed.supports(DataType.DIVIDEND, Market.US)
    assert not keyed.supports(DataType.QUOTE_LATEST, Market.TW)
    # No key -> inert (registry never calls it).
    assert not AlphaVantageProvider(token=None).supports(DataType.QUOTE_LATEST, Market.US)


def test_finnhub_key_gated_supports() -> None:
    keyed = FinnhubProvider(token="fh-key")
    assert keyed.name == "finnhub"
    assert keyed.supports(DataType.QUOTE_LATEST, Market.US)
    assert keyed.supports(DataType.DIVIDEND, Market.US)
    assert not keyed.supports(DataType.QUOTE_HISTORY, Market.US)
    assert not keyed.supports(DataType.QUOTE_LATEST, Market.MY)
    assert not FinnhubProvider(token=None).supports(DataType.QUOTE_LATEST, Market.US)


def test_token_getter_resolves_at_call_time() -> None:
    box: dict[str, str | None] = {"k": None}
    p = AlphaVantageProvider(token_getter=lambda: box["k"])
    assert not p.supports(DataType.QUOTE_LATEST, Market.US)  # no key yet
    box["k"] = "set-later"
    assert p.supports(DataType.QUOTE_LATEST, Market.US)  # DB write seen on next resolve


def test_fred_source_key_gated() -> None:
    assert FredSource(token="fred-key").available()
    assert not FredSource(token=None).available()


def test_pending_adapters_no_network_without_key() -> None:
    # Unkeyed fetches must raise/return empty without making a request (no monkeypatch
    # needed because the key gate short-circuits before any I/O).
    av = AlphaVantageProvider(token=None)
    assert av.fetch_quote_latest([]) == []
    fh = FinnhubProvider(token=None)
    assert fh.fetch_quote_latest([]) == []
    assert FredSource(token=None).fetch_series("CPIAUCSL") is None
