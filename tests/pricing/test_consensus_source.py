"""Tests for the analyst-consensus client (Blueprint P1 batch 2).

Hermetic: the two yfinance endpoints are monkeypatched via the ``_fetch_*`` seams; the
pure ``build_consensus`` is exercised directly with the live-probed shapes (2026-07-08).
Money/price numbers are Decimal strings with the 4dp float-noise cap; counts are ints;
ratios use their stated quantization. Missing/empty data yields None (no snapshot).
"""

from datetime import date

import pytest

from portfolio_dash.pricing import consensus_source as CS

_AS_OF = date(2026, 7, 9)

# Live-probed 2330.TW shapes (2026-07-08).
_TW_TARGETS = {"current": 2465.0, "high": 3800.0, "low": 2051.0,
               "mean": 2819.8484, "median": 2780.0}
_TW_RECS = [
    {"period": "0m", "strongBuy": 9, "buy": 23, "hold": 1, "sell": 0, "strongSell": 0},
    {"period": "-1m", "strongBuy": 8, "buy": 22, "hold": 2, "sell": 0, "strongSell": 0},
]


def test_build_consensus_covered_symbol_2330_shape() -> None:
    payload = CS.build_consensus(targets=_TW_TARGETS, rec_records=_TW_RECS, as_of=_AS_OF)
    assert payload is not None
    assert payload["as_of"] == "2026-07-09"
    assert payload["source"] == "yfinance"
    # target prices as Decimal strings, cap-only-never-pad.
    pt = payload["price_targets"]
    assert pt["current"] == "2465.0"
    assert pt["mean"] == "2819.8484"  # already 4dp -> unchanged
    assert pt["high"] == "3800.0" and pt["low"] == "2051.0" and pt["median"] == "2780.0"
    # ratings (this month) as ints + total.
    assert payload["ratings"] == {"strong_buy": 9, "buy": 23, "hold": 1, "sell": 0,
                                  "strong_sell": 0, "total": 33}
    assert payload["ratings_prev_month"]["total"] == 32
    # weighted rating score (1*9+2*23+3*1)/33 = 58/33 = 1.757… -> 1.76.
    assert payload["rating_score"] == "1.76"
    # upside (2819.8484-2465)/2465 = 0.143958… -> 0.1440.
    assert payload["upside_vs_mean_pct"] == "0.1440"


def test_build_consensus_4dp_cap_behavior() -> None:
    # 315.56668 (5dp) caps to 315.5667; a 4dp value is byte-identical.
    targets = {"current": 310.66, "high": 400.0, "low": 215.0,
               "mean": 315.56668, "median": 315.0}
    payload = CS.build_consensus(targets=targets, rec_records=None, as_of=_AS_OF)
    assert payload is not None
    assert payload["price_targets"]["mean"] == "315.5667"
    assert payload["price_targets"]["current"] == "310.66"


def test_build_consensus_zero_total_ratings() -> None:
    # A present 0m row with all-zero counts -> ratings dict with total 0, score None.
    recs = [{"period": "0m", "strongBuy": 0, "buy": 0, "hold": 0, "sell": 0,
             "strongSell": 0}]
    payload = CS.build_consensus(targets=_TW_TARGETS, rec_records=recs, as_of=_AS_OF)
    assert payload is not None  # targets keep the snapshot meaningful
    assert payload["ratings"]["total"] == 0
    assert payload["rating_score"] is None


def test_build_consensus_missing_targets_keeps_ratings() -> None:
    # No targets but ratings present -> price_targets null, upside None, snapshot kept.
    payload = CS.build_consensus(targets=None, rec_records=_TW_RECS, as_of=_AS_OF)
    assert payload is not None
    assert payload["price_targets"] is None
    assert payload["upside_vs_mean_pct"] is None
    assert payload["ratings"]["total"] == 33


def test_build_consensus_no_data_returns_none() -> None:
    # No targets AND no rated period -> no snapshot (honest degrade downstream).
    assert CS.build_consensus(targets=None, rec_records=None, as_of=_AS_OF) is None
    assert CS.build_consensus(targets={}, rec_records=[], as_of=_AS_OF) is None


def test_build_consensus_upside_none_when_current_nonpositive() -> None:
    targets = {"current": 0.0, "mean": 100.0}
    payload = CS.build_consensus(targets=targets, rec_records=_TW_RECS, as_of=_AS_OF)
    assert payload is not None
    assert payload["upside_vs_mean_pct"] is None


def test_fetch_consensus_uses_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CS, "_fetch_price_targets", lambda s: dict(_TW_TARGETS))
    monkeypatch.setattr(CS, "_fetch_recommendations", lambda s: list(_TW_RECS))
    payload = CS.fetch_consensus("2330.TW", as_of=_AS_OF)
    assert payload is not None
    assert payload["rating_score"] == "1.76"


def test_fetch_consensus_degrades_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_s: str) -> object:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(CS, "_fetch_price_targets", boom)
    monkeypatch.setattr(CS, "_fetch_recommendations", boom)
    assert CS.fetch_consensus("AAPL", as_of=_AS_OF) is None
