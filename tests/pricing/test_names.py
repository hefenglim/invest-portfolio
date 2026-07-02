"""Unit tests: pricing.names — best-effort name lookup (all seams stubbed)."""

import pytest

from portfolio_dash.pricing import names
from portfolio_dash.shared.enums import Market


def test_tw_uses_static_table_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(names, "_tw_name", lambda s: "台積電")
    monkeypatch.setattr(names, "_yf_name", lambda s: pytest.fail("must not hit yfinance"))
    assert names.lookup_name("2330", Market.TW) == "台積電"


def test_tw_falls_through_to_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def yf(sym: str) -> str | None:
        seen.append(sym)
        return "GlobalWafers"

    monkeypatch.setattr(names, "_tw_name", lambda s: None)
    monkeypatch.setattr(names, "_yf_name", yf)
    assert names.lookup_name("6488", Market.TW, board="TPEx") == "GlobalWafers"
    assert seen == ["6488.TWO"]  # TPEx uses the .TWO suffix


def test_us_and_my_suffixes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def yf(sym: str) -> str | None:
        seen.append(sym)
        return "X"

    monkeypatch.setattr(names, "_yf_name", yf)
    assert names.lookup_name("AAPL", Market.US) == "X"
    assert names.lookup_name("0138", Market.MY) == "X"
    assert seen == ["AAPL", "0138.KL"]


def test_lookup_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(sym: str) -> str | None:
        raise RuntimeError("network down")

    monkeypatch.setattr(names, "_tw_name", boom)
    monkeypatch.setattr(names, "_yf_name", boom)
    assert names.lookup_name("2330", Market.TW) is None
    assert names.lookup_name("AAPL", Market.US) is None
