import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from scripts.probe.adapters.finmind_src import (
    DATASET_TIER,
    FINMIND_DATASETS,
    parse_dataset_rows,
    parse_finmind_close,
    tier_from_limit,
)
from scripts.probe.adapters.my_src import parse_klse_price, parse_malaysiastock_price
from scripts.probe.adapters.sentiment_src import parse_fng
from scripts.probe.adapters.tw_gov import parse_twse_close, tpex_close_for
from scripts.probe.adapters.twstock_src import parse_twstock_price
from scripts.probe.adapters.us_alt import (
    parse_alpha_close,
    parse_finnhub_close,
    parse_stockprices_close,
)
from scripts.probe.adapters.yfinance_src import (
    has_raw_and_adj,
    max_decimals,
    parse_latest_close,
)

_FX = Path("tests/pricing/fixtures/yfinance/3182.KL.json")
_KLSE = Path("tests/pricing/fixtures/klse/3182.html")
_FM = Path("tests/pricing/fixtures/finmind/2330.json")
_TW = Path("tests/pricing/fixtures/twstock/2330.json")
_SP = Path("tests/pricing/fixtures/stockprices/AAPL.json")
_AV = Path("tests/pricing/fixtures/alphavantage/AAPL.json")
_FH = Path("tests/pricing/fixtures/finnhub/AAPL.json")


def test_yf_parser_against_recorded_my_fixture() -> None:
    df = pd.read_json(_FX)
    close = parse_latest_close(df)
    assert close is None or isinstance(close, Decimal)
    assert close == Decimal(str(df["Close"].iloc[-1]))
    assert has_raw_and_adj(df)
    assert max_decimals(df) >= 0


def test_twse_parser_against_fixture() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    assert parse_twse_close(payload) is not None


def test_tpex_parser_against_fixture() -> None:
    rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    assert tpex_close_for(rows, "8299") is not None


@pytest.mark.skipif(not _FM.exists(), reason="FinMind fixture needs a token to record")
def test_finmind_parser() -> None:
    assert parse_finmind_close(json.loads(_FM.read_text("utf-8"))) is not None


@pytest.mark.skipif(not _TW.exists(), reason="twstock fixture not recorded")
def test_twstock_parser_tolerant() -> None:
    payload = json.loads(_TW.read_text("utf-8"))
    # market may be closed -> price string or None are both acceptable structurally
    result = parse_twstock_price(payload)
    assert result is None or isinstance(result, str)


@pytest.mark.skipif(not _SP.exists(), reason="stockprices.dev fixture not recorded")
def test_stockprices_parser() -> None:
    payload = json.loads(_SP.read_text("utf-8"))
    # discovered shape: {"Ticker": "AAPL", "Name": "Apple Inc.", "Price": 307.34, ...}
    close = parse_stockprices_close(payload)
    assert close is not None
    assert isinstance(close, int | float)


@pytest.mark.skipif(not _AV.exists(), reason="AlphaVantage fixture needs a key")
def test_alpha_parser() -> None:
    assert parse_alpha_close(json.loads(_AV.read_text("utf-8"))) is not None


@pytest.mark.skipif(not _FH.exists(), reason="Finnhub fixture needs a key")
def test_finnhub_parser() -> None:
    assert parse_finnhub_close(json.loads(_FH.read_text("utf-8"))) is not None


@pytest.mark.skipif(not _KLSE.exists(), reason="klsescreener fixture not recorded")
def test_klse_parser() -> None:
    html = _KLSE.read_text("utf-8")
    price = parse_klse_price(html)
    assert price is not None
    assert isinstance(price, str)
    # discovered shape: klsescreener preserves full decimal precision as text
    # (e.g. "2.260", 3 dp) -- unlike yfinance's float64 columns.
    assert "." in price
    assert len(price.split(".")[-1]) >= 2


# --- spec-20 additions: FinMind datasets, Malaysiastock, CNN F&G ---------------


def test_finmind_dataset_mapping_complete() -> None:
    assert set(FINMIND_DATASETS) == {
        "institutional", "margin", "valuation", "monthly_revenue", "financials"
    }
    assert FINMIND_DATASETS["institutional"] == "TaiwanStockInstitutionalInvestorsBuySell"


def test_finmind_tier_from_limit() -> None:
    # all 5 datasets stay Free under our data_id query mode (spec 20.15.2).
    assert set(DATASET_TIER) == set(FINMIND_DATASETS)
    assert all(t == "free" for t in DATASET_TIER.values())
    # api_request_limit reveals the tier (spec 20.15.5).
    assert tier_from_limit(600) == "free"
    assert tier_from_limit(1600) == "backer"
    assert tier_from_limit(6000) == "sponsor"
    assert tier_from_limit(20000) == "sponsorpro"
    assert tier_from_limit(None) is None
    assert tier_from_limit("nope") is None


def test_parse_dataset_rows_tolerant() -> None:
    assert parse_dataset_rows({"data": [{"x": 1}]}) == [{"x": 1}]
    assert parse_dataset_rows({"msg": "success"}) == []
    assert parse_dataset_rows({"data": None}) == []


def test_malaysiastock_parser_3dp() -> None:
    html = '<html><body><span id="SharePrice">0.075</span></body></html>'
    assert parse_malaysiastock_price(html) == "0.075"
    # non-numeric / missing node -> None (degrade).
    assert parse_malaysiastock_price('<span id="SharePrice">N/A</span>') is None
    assert parse_malaysiastock_price("<html></html>") is None


def test_parse_fng() -> None:
    payload = {"fear_and_greed": {"score": 62.5, "rating": "greed"}}
    assert parse_fng(payload) == {"score": 62.5, "rating": "greed"}
    assert parse_fng({"unexpected": {}}) is None
    assert parse_fng({"fear_and_greed": {"score": None, "rating": "x"}}) is None
