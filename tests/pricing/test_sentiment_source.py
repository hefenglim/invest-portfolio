"""Tests for the VIX / CNN Fear & Greed sentiment client (spec 20.7).

Hermetic: the VIX last-close getter and the CNN HTTP getter are both monkeypatched.
Returns Decimal values; any failure degrades to None (no fabrication).
"""

from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.pricing import sentiment_source as SS


def test_fetch_vix_returns_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SS, "_vix_last_close", lambda: 14.23)
    got = SS.fetch_vix()
    assert got == Decimal("14.23")
    assert isinstance(got, Decimal)


def test_fetch_vix_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> float | None:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(SS, "_vix_last_close", _boom)
    assert SS.fetch_vix() is None


def test_fetch_vix_none_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SS, "_vix_last_close", lambda: None)
    assert SS.fetch_vix() is None


def test_fetch_fear_greed_parses_score_and_rating(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "fear_and_greed": {"score": 62.5, "rating": "greed", "timestamp": "x"}
    }
    monkeypatch.setattr(SS, "_cnn_graphdata", lambda: payload)
    got = SS.fetch_fear_greed()
    assert got == {"score": Decimal("62.5"), "rating": "greed"}
    assert isinstance(got["score"], Decimal)


def test_fetch_fear_greed_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("CNN unreachable")

    monkeypatch.setattr(SS, "_cnn_graphdata", _boom)
    assert SS.fetch_fear_greed() is None


def test_fetch_fear_greed_none_on_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SS, "_cnn_graphdata", lambda: {"unexpected": {}})
    assert SS.fetch_fear_greed() is None
