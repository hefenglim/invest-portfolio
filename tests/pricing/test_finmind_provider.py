import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers import finmind_provider as FP
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Currency, Market

_FIX = Path("tests/pricing/fixtures/finmind/TaiwanStockDividend_2330.json")
_PRICE_FIX = Path("tests/pricing/fixtures/finmind/TaiwanStockPrice_2330.json")


def test_supports_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # isolate from a real FINMIND_TOKEN in the environment (litellm loads .env on import)
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert FinMindProvider(token="x").supports(DataType.DIVIDEND, Market.TW)
    assert not FinMindProvider(token=None).supports(DataType.DIVIDEND, Market.TW)
    assert not FinMindProvider(token="x").supports(DataType.DIVIDEND, Market.US)
    assert not FinMindProvider(token="x").supports(DataType.QUOTE_LATEST, Market.TW)


def test_supports_quote_history_tw_token_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """QUOTE_HISTORY is served for TW only, and only when a token is present (P1-②)."""
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert FinMindProvider(token="x").supports(DataType.QUOTE_HISTORY, Market.TW)
    assert not FinMindProvider(token=None).supports(DataType.QUOTE_HISTORY, Market.TW)
    assert not FinMindProvider(token="x").supports(DataType.QUOTE_HISTORY, Market.US)
    assert not FinMindProvider(token="x").supports(DataType.QUOTE_HISTORY, Market.MY)
    # QUOTE_LATEST stays unsupported (FinMind is a history fallback, not a live-quote one)
    assert not FinMindProvider(token="x").supports(DataType.QUOTE_LATEST, Market.TW)


def test_fetch_dividends_uses_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dividend fetch sends ``Authorization: Bearer {token}`` (spec 20.15.1), not a
    ``token`` query param."""
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"msg": "success", "status": 200, "data": []}

    def _fake_get(url: str, *, params: dict[str, Any], headers: dict[str, str],
                  timeout: int) -> _Resp:
        captured["params"] = params
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(FP.requests, "get", _fake_get)
    FinMindProvider(token="tok-9").fetch_dividends(
        [InstrumentRef(symbol="2330", market=Market.TW)]
    )
    assert captured["headers"]["Authorization"] == "Bearer tok-9"
    assert "token" not in captured["params"]


def test_parse_dividends_from_fixture() -> None:
    payload = json.loads(_FIX.read_text("utf-8"))
    events = FinMindProvider(token="x")._parse_dividends(payload, instrument="2330")
    assert events, "expected at least one dividend event with an ex-date"
    assert all(e.source == "finmind" and e.currency is Currency.TWD for e in events)
    assert all(e.market is Market.TW for e in events)
    assert all(isinstance(e.cash_amount, Decimal) for e in events if e.cash_amount is not None)
    assert events == sorted(events, key=lambda e: e.ex_date)  # ascending


def test_parse_quote_history_from_fixture() -> None:
    """TaiwanStockPrice rows map to PriceRow with OHLC + integer volume (P1-②)."""
    payload = json.loads(_PRICE_FIX.read_text("utf-8"))
    rows = FinMindProvider(token="x")._parse_quote_history(payload, instrument="2330")
    assert rows, "expected price rows from the TaiwanStockPrice fixture"
    assert all(r.source == "finmind" and r.market is Market.TW for r in rows)
    assert rows == sorted(rows, key=lambda r: r.as_of)  # ascending
    first = rows[0]
    assert first.as_of == date(2024, 1, 2)
    assert first.close == Decimal("593.0")
    assert first.open == Decimal("590.0")
    assert first.high == Decimal("593.0")   # FinMind ``max`` -> high
    assert first.low == Decimal("589.0")    # FinMind ``min`` -> low
    assert first.volume == Decimal("27997826")  # Trading_Volume, integer Decimal
    # volume is integer-valued (not money — no 2-dp quantization)
    assert all(r.volume is not None and r.volume == r.volume.to_integral_value()
               for r in rows)


def test_parse_quote_history_skips_missing_close_and_zero_volume_kept() -> None:
    payload = {"data": [
        {"date": "2026-01-05", "close": None, "Trading_Volume": 100},   # skipped: no close
        {"date": "", "close": 10.0, "Trading_Volume": 100},             # skipped: no date
        {"date": "2026-01-06", "close": 12.5, "open": 12.0, "max": 13.0,
         "min": 11.5, "Trading_Volume": 0},                             # kept, vol stays 0
    ]}
    rows = FinMindProvider(token="x")._parse_quote_history(payload, instrument="2330")
    assert len(rows) == 1
    assert rows[0].as_of == date(2026, 1, 6) and rows[0].close == Decimal("12.5")
    assert rows[0].volume == Decimal("0")  # a genuine no-trade session, not None


def test_fetch_quote_history_uses_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quote-history fetch sends Bearer auth + the TaiwanStockPrice dataset params."""
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"msg": "success", "status": 200, "data": []}

    def _fake_get(url: str, *, params: dict[str, Any], headers: dict[str, str],
                  timeout: int) -> _Resp:
        captured["params"] = params
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(FP.requests, "get", _fake_get)
    FinMindProvider(token="tok-7").fetch_quote_history(
        InstrumentRef(symbol="2330", market=Market.TW, board="TWSE"), date(2021, 1, 1)
    )
    assert captured["headers"]["Authorization"] == "Bearer tok-7"
    assert captured["params"]["dataset"] == "TaiwanStockPrice"
    assert captured["params"]["data_id"] == "2330"
    assert captured["params"]["start_date"] == "2021-01-01"
    assert "token" not in captured["params"]


def test_fetch_quote_history_non_tw_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-TW ref never hits the network (registry-level safety); returns empty."""
    def _boom(*a: Any, **k: Any) -> Any:  # pragma: no cover - must not be reached
        raise AssertionError("network should not be called for non-TW")

    monkeypatch.setattr(FP.requests, "get", _boom)
    out = FinMindProvider(token="x").fetch_quote_history(
        InstrumentRef(symbol="AAPL", market=Market.US), date(2021, 1, 1)
    )
    assert out == []


def test_registry_history_falls_through_yfinance_to_finmind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """yfinance empty/raises for a TW symbol -> FinMind fills; winning source recorded."""
    from portfolio_dash.pricing.providers.base import ProviderBase
    from portfolio_dash.pricing.registry import Registry

    class _EmptyYf(ProviderBase):
        name = "yfinance"

        def supports(self, data_type: DataType, market: Market | None) -> bool:
            return data_type is DataType.QUOTE_HISTORY

        def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[Any]:
            return []  # no data -> the registry must fall through to FinMind

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            data: dict[str, Any] = json.loads(_PRICE_FIX.read_text("utf-8"))
            return data

    monkeypatch.setattr(FP.requests, "get",
                        lambda *a, **k: _Resp())  # FinMind returns the fixture
    reg = Registry(
        providers={"yfinance": _EmptyYf(), "finmind": FinMindProvider(token="x")},
        order={(DataType.QUOTE_HISTORY, Market.TW): ["yfinance", "finmind"]},
    )
    rows, sources, failed = reg.fetch_quote_history(
        [InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")], date(2021, 1, 1)
    )
    assert rows and sources == {"2330": "finmind"} and failed == []
    assert all(r.source == "finmind" for r in rows)
