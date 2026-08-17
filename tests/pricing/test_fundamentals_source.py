"""Unit tests for pricing/fundamentals_source.py (W3, AI-D13..D16).

The builders are pure over plain seam data; the fetch seams are monkeypatched (the repo
bans sockets in tests). Pinned behaviours:

* canonical fields, absent-when-unavailable (never fabricated);
* the honest-denominator rule (negative-EPS PE / negative-equity PB are omitted);
* a partial TTM is NOT a TTM (fewer than 4 quarters -> eps_ttm absent, never annualized);
* unit unification at the seam (finnhub millions -> raw; AV ratios -> percents);
* fast_info camelCase AND legacy snake_case keys both read;
* keyless keyed legs return None without any HTTP;
* every-field-absent -> no block (None), so no hollow snapshot is stored.
"""

from datetime import date
from decimal import Decimal

import pytest
import requests

from portfolio_dash.pricing import fundamentals_source as F
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market

_AS_OF = date(2026, 8, 17)
_REF_US = InstrumentRef(symbol="AAPL", market=Market.US)
_REF_TW = InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")

# Newest-first quarterly statement columns: [Q0, Q1, Q2, Q3, Q4] — 5 columns give the
# YoY pair (Q0 vs Q4).
_QINC: dict[str, list[Decimal | None]] = {
    "Diluted EPS": [Decimal("2"), Decimal("2"), Decimal("2"), Decimal("2"), Decimal("1")],
    "Net Income": [Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("8")],
    "Total Revenue": [
        Decimal("110"), Decimal("100"), Decimal("90"), Decimal("100"), Decimal("80"),
    ],
}
_BS: dict[str, list[Decimal | None]] = {"Stockholders Equity": [Decimal("50")]}
_FAST_INFO = {"lastPrice": 200, "marketCap": 40_000, "currency": "USD"}
_DIVIDENDS = [
    (date(2026, 5, 9), Decimal("1")), (date(2026, 2, 7), Decimal("1")),
    (date(2025, 8, 20), Decimal("1")),  # within the trailing 366 days
    (date(2024, 5, 10), Decimal("5")),  # older — outside the window, must be ignored
]


def test_fetchers_cover_the_declared_sources() -> None:
    assert tuple(sorted(F.FETCHERS)) == tuple(sorted(F.SOURCES))


def test_yfinance_block_full_fixture() -> None:
    block = F.build_yfinance_block(
        fast_info=_FAST_INFO, quarterly_income=_QINC, balance_sheet=_BS,
        dividends=_DIVIDENDS, market=Market.US, as_of=_AS_OF,
    )
    assert block is not None
    # eps_ttm = 2+2+2+2 = 8; pe = 200/8 = 25; pb = 40000/50 = 800; roe = 40/50 = 80%
    assert block["eps_ttm"] == "8"
    assert block["pe_ratio"] == "25"
    assert block["pb_ratio"] == "800"
    assert block["roe_pct"] == "80"
    # revenue YoY = (110-80)/80 = 37.5%
    assert block["revenue_growth_yoy_pct"] == "37.5"
    # trailing-12m dividends = 3 x 1 (the 2024 payment is outside the window): 3/200 = 1.5%
    assert block["dividend_yield_pct"] == "1.5"
    assert block["market_cap"] == "40000"
    assert "beta" not in block  # never available without Ticker.info — absent, not guessed
    assert block["currency"] == "USD"
    assert block["as_of"] == "2026-08-17"


def test_yfinance_block_reads_legacy_snake_case_fast_info() -> None:
    block = F.build_yfinance_block(
        fast_info={"last_price": 100, "market_cap": 5000, "currency": "TWD"},
        quarterly_income=_QINC, balance_sheet=_BS, dividends=[],
        market=Market.TW, as_of=_AS_OF,
    )
    assert block is not None
    assert block["pe_ratio"] == "12.5"  # 100 / 8
    assert block["market_cap"] == "5000"
    assert block["currency"] == "TWD"


def test_yfinance_block_omits_ratios_with_dishonest_denominators() -> None:
    qinc = dict(_QINC)
    qinc["Diluted EPS"] = [Decimal("-1"), Decimal("-1"), Decimal("-1"), Decimal("-1")]
    block = F.build_yfinance_block(
        fast_info=_FAST_INFO, quarterly_income=qinc,
        balance_sheet={"Stockholders Equity": [Decimal("-5")]}, dividends=[],
        market=Market.US, as_of=_AS_OF,
    )
    assert block is not None
    assert "pe_ratio" not in block  # negative-EPS PE omitted, never stored negative
    assert "pb_ratio" not in block  # negative-equity PB omitted
    assert "roe_pct" not in block
    assert block["eps_ttm"] == "-4"  # the reported EPS itself passes through


def test_yfinance_block_never_annualizes_a_short_ttm() -> None:
    qinc: dict[str, list[Decimal | None]] = {
        "Diluted EPS": [Decimal("2"), Decimal("2"), Decimal("2")]
    }  # only 3 quarters
    block = F.build_yfinance_block(
        fast_info=_FAST_INFO, quarterly_income=qinc, balance_sheet=_BS, dividends=[],
        market=Market.US, as_of=_AS_OF,
    )
    assert block is not None
    assert "eps_ttm" not in block
    assert "pe_ratio" not in block


def test_yfinance_block_empty_inputs_yield_no_block() -> None:
    assert F.build_yfinance_block(
        fast_info=None, quarterly_income={}, balance_sheet={}, dividends=[],
        market=Market.MY, as_of=_AS_OF,
    ) is None


def test_fetch_yfinance_swallows_seam_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_symbol: str) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(F, "_fetch_yf_fast_info", boom)
    monkeypatch.setattr(F, "_fetch_yf_quarterly_income", boom)
    monkeypatch.setattr(F, "_fetch_yf_balance_sheet", boom)
    monkeypatch.setattr(F, "_fetch_yf_dividends", boom)
    assert F.fetch_yfinance(_REF_TW, as_of=_AS_OF) is None


def test_finnhub_block_maps_and_normalizes_units() -> None:
    metric = {
        "peTTM": 28.02, "pbAnnual": 45.1, "epsTTM": 6.58,
        "marketCapitalization": 4210.5,          # MILLIONS -> raw units
        "currentDividendYieldTTM": 0.44,          # already a percent
        "beta": 1.1, "roeTTM": 151.4, "revenueGrowthTTMYoy": 7.8,
    }
    block = F.build_finnhub_block(metric, as_of=_AS_OF)
    assert block is not None
    assert block["pe_ratio"] == "28.02"
    assert block["market_cap"] == "4210500000"  # 4210.5 x 1e6
    assert block["dividend_yield_pct"] == "0.44"  # percent as reported
    assert block["roe_pct"] == "151.4"
    assert block["beta"] == "1.1"
    assert block["currency"] == "USD"


def test_finnhub_block_empty_metric_is_no_block() -> None:
    assert F.build_finnhub_block(None, as_of=_AS_OF) is None
    assert F.build_finnhub_block({}, as_of=_AS_OF) is None


def test_finnhub_fetch_is_key_gated_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINNHUB_KEY", raising=False)

    def no_http(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP must not be attempted without a key")

    monkeypatch.setattr(requests, "get", no_http)
    assert F.fetch_finnhub(_REF_US, as_of=_AS_OF, token=None) is None


def test_alphavantage_block_converts_ratios_to_percents_and_drops_placeholders() -> None:
    raw = {
        "Symbol": "AAPL", "PERatio": "28.4", "PriceToBookRatio": "45.06",
        "EPSTTM": "6.58", "MarketCapitalization": "4210000000000",
        "DividendYield": "0.004",                  # ratio -> 0.4%
        "Beta": "1.1",
        "ReturnOnEquityTTM": "1.514",              # ratio -> 151.4%
        "QuarterlyRevenueGrowthYOY": "0.078",      # ratio -> 7.8%
        "ProfitMargin": "None",                    # AV's literal placeholder -> dropped
    }
    block = F.build_alphavantage_block(raw, as_of=_AS_OF)
    assert block is not None
    assert block["dividend_yield_pct"] == "0.4"
    assert block["roe_pct"] == "151.4"
    assert block["revenue_growth_yoy_pct"] == "7.8"
    assert block["market_cap"] == "4210000000000"
    assert block["currency"] == "USD"


def test_alphavantage_block_all_placeholders_is_no_block() -> None:
    assert F.build_alphavantage_block(
        {"Symbol": "ZZZ", "PERatio": "None", "Beta": "-"}, as_of=_AS_OF
    ) is None


def test_alphavantage_throttle_body_is_not_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # AV rate-limits with HTTP 200 + {"Note": ...}: the seam must read it as "no data".
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"Note": "thank you for using Alpha Vantage ..."}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert F._fetch_av_overview("AAPL", "demo-key") is None
