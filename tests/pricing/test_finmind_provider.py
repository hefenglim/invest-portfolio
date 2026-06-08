import json
from decimal import Decimal
from pathlib import Path

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.shared.enums import Currency, Market

_FIX = Path("tests/pricing/fixtures/finmind/TaiwanStockDividend_2330.json")


def test_supports_requires_token() -> None:
    assert FinMindProvider(token="x").supports(DataType.DIVIDEND, Market.TW)
    assert not FinMindProvider(token=None).supports(DataType.DIVIDEND, Market.TW)
    assert not FinMindProvider(token="x").supports(DataType.DIVIDEND, Market.US)
    assert not FinMindProvider(token="x").supports(DataType.QUOTE_LATEST, Market.TW)


def test_parse_dividends_from_fixture() -> None:
    payload = json.loads(_FIX.read_text("utf-8"))
    events = FinMindProvider(token="x")._parse_dividends(payload, instrument="2330")
    assert events, "expected at least one dividend event with an ex-date"
    assert all(e.source == "finmind" and e.currency is Currency.TWD for e in events)
    assert all(e.market is Market.TW for e in events)
    assert all(isinstance(e.cash_amount, Decimal) for e in events if e.cash_amount is not None)
    assert events == sorted(events, key=lambda e: e.ex_date)  # ascending
