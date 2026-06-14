"""Tests for the yfinance index-quotes client (spec 20.7).

Hermetic: the per-symbol last-close getter is monkeypatched. Returns Decimal closes
keyed by index symbol; symbols with no data are omitted (graceful degradation).
"""

from decimal import Decimal

import pytest

from portfolio_dash.pricing import index_source as IX


def test_fetch_indices_returns_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = {"^TWII": 22150.5, "^GSPC": 5980.12, "^KLSE": 1612.0}
    monkeypatch.setattr(IX, "_index_last_close", lambda sym: closes.get(sym))
    got = IX.fetch_indices()
    assert got == {
        "^TWII": Decimal("22150.5"),
        "^GSPC": Decimal("5980.12"),
        "^KLSE": Decimal("1612.0"),
    }
    assert all(isinstance(v, Decimal) for v in got.values())


def test_fetch_indices_omits_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        IX, "_index_last_close",
        lambda sym: 5980.12 if sym == "^GSPC" else None,
    )
    got = IX.fetch_indices()
    assert got == {"^GSPC": Decimal("5980.12")}


def test_fetch_indices_skips_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _getter(sym: str) -> float | None:
        if sym == "^TWII":
            raise RuntimeError("boom")
        return 100.0

    monkeypatch.setattr(IX, "_index_last_close", _getter)
    got = IX.fetch_indices()
    assert "^TWII" not in got
    assert got["^GSPC"] == Decimal("100.0")


def test_index_symbols_constant() -> None:
    assert IX.INDEX_SYMBOLS == ("^TWII", "^GSPC", "^KLSE")
