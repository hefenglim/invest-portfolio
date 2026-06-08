import json
from decimal import Decimal
from pathlib import Path

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider, yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market


def test_yf_symbol_suffix() -> None:
    assert yf_symbol(InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")) == "2330.TW"
    assert yf_symbol(InstrumentRef(symbol="8299", market=Market.TW, board="TPEx")) == "8299.TWO"
    assert yf_symbol(InstrumentRef(symbol="3182", market=Market.MY, board=".KL")) == "3182.KL"
    assert yf_symbol(InstrumentRef(symbol="AAPL", market=Market.US)) == "AAPL"


def test_supports() -> None:
    p = YFinanceProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.US)
    assert p.supports(DataType.FX, None)


def test_parse_history_json_to_pricerows() -> None:
    raw = Path("tests/pricing/fixtures/yfinance/3182.KL.json").read_text("utf-8")
    rows = YFinanceProvider()._parse_history_json(json.loads(raw), instrument="3182",
                                                  market=Market.MY)
    assert rows and all(isinstance(r.close, Decimal) for r in rows)
    assert rows[-1].source == "yfinance"
    assert rows == sorted(rows, key=lambda r: r.as_of)  # ascending


def test_finite_filters_nan_inf_none() -> None:
    from portfolio_dash.pricing.providers.yfinance_provider import _finite

    assert _finite(2.5) == Decimal("2.5")
    assert _finite("2.260") == Decimal("2.260")
    assert _finite(None) is None
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None


def test_parse_history_json_skips_nan_close() -> None:
    # yfinance gaps arrive as null/NaN -> must be skipped, not raised (Money is finite-only)
    payload = {"Close": {"1700000000000": None, "1700086400000": 2.5}}
    rows = YFinanceProvider()._parse_history_json(payload, instrument="X", market=Market.MY)
    assert len(rows) == 1 and rows[0].close == Decimal("2.5")
