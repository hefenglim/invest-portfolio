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


# --- owner 2026-09-30, item 8: the exchange's own short name, from the recorded payloads -----


def test_twse_parse_name_from_the_title() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    assert payload["title"] == "115年06月 2330 台積電           各日成交資訊"
    assert TwseProvider()._parse_name(payload, instrument="2330") == "台積電"


def test_twse_parse_name_refuses_another_codes_title_or_no_title() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    assert TwseProvider()._parse_name(payload, instrument="2303") is None
    assert TwseProvider()._parse_name({"stat": "OK"}, instrument="2330") is None
    assert TwseProvider()._parse_name({"title": "奇怪的標題"}, instrument="2330") is None


def test_twse_parse_name_keeps_a_name_with_letters_and_digits() -> None:
    payload = {"title": "115年09月 0050 元大台灣50 各日成交資訊"}
    assert TwseProvider()._parse_name(payload, instrument="0050") == "元大台灣50"


def test_tpex_parse_name_from_company_name() -> None:
    rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    assert TpexProvider()._parse_name(rows, instrument="8299") == "群聯"
    assert TpexProvider()._parse_name(rows, instrument="9999") is None
    assert TpexProvider()._parse_name([{"SecuritiesCompanyCode": "8299"}],
                                      instrument="8299") is None
