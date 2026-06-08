import json
from decimal import Decimal
from pathlib import Path

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.shared.enums import Market


def test_twse_parse_close() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    r = TwseProvider()._parse(payload, instrument="2330")
    assert r is not None and r.close == Decimal("2295.00") and r.source == "twse"
    assert r.market is Market.TW


def test_tpex_parse_close() -> None:
    rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    r = TpexProvider()._parse(rows, instrument="8299")
    assert r is not None and r.close == Decimal("2250.00")


def test_supports_tw_only() -> None:
    assert TwseProvider().supports(DataType.QUOTE_LATEST, Market.TW)
    assert not TwseProvider().supports(DataType.QUOTE_LATEST, Market.US)
    assert not TwseProvider().supports(DataType.FX, None)
    assert TpexProvider().supports(DataType.QUOTE_LATEST, Market.TW)
