"""The three W6 variable producers (AI-D31), hand-checked through the real seam.

`backtest_json` / `calibration_gap_json` keep their DECLARED spec-04 meaning (AI
self-calibration over insight_evaluations — confidence anchoring); the event study rides
the new per-symbol `signal_backtest_json`. The endpoint-level pin proves the rendered
prompt actually carries the study (preview path = run path, same `_external_vars`).
"""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers.prompts import (
    _backtest_var,
    _calibration_gap_var,
    _signal_backtest_var,
)
from portfolio_dash.api.signals_service import scan_signals
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight.evaluations_store import EvalStatus, add_evaluation
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW

_END = GOLDEN_NOW.date()


def _score(
    conn: sqlite3.Connection, row: int, confidence: int, miss: bool, *,
    shadow: bool = False, status: EvalStatus = "scored",
) -> None:
    add_evaluation(
        conn, insight_id=1000 + row, insight_type_id=1, calibration_version=None,
        is_shadow=shadow, status=status, quant_hit=not miss, narrative_score=None,
        miss=miss, actual_value=None, confidence=confidence,
        now=GOLDEN_NOW + timedelta(days=row),
    )


# --- backtest_json (global calibration bins + overall hit rate) -----------------


def test_backtest_var_unavailable_when_nothing_scored(
    golden_db: sqlite3.Connection,
) -> None:
    assert _backtest_var(golden_db) == {"unavailable": True, "last_as_of": None}


def test_backtest_var_bins_and_overall_hand_checked(
    golden_db: sqlite3.Connection,
) -> None:
    # 60-80 bucket: 4 rows @70, 3 hits. 80-100 bucket: 4 rows @90, 2 hits.
    for i, (conf, miss) in enumerate(
        [(70, False), (70, False), (70, False), (70, True),
         (90, False), (90, False), (90, True), (90, True)]
    ):
        _score(golden_db, i, conf, miss)
    # Anti-poison pins: a shadow row and a pending row must NOT enter the population.
    _score(golden_db, 8, 10, True, shadow=True)
    _score(golden_db, 9, 100, True, status="pending_data")
    out = _backtest_var(golden_db)
    assert out["overall_hit_rate"] == "0.625"
    assert out["bins"] == [
        {"bucket": "60-80", "n": 4, "hit_count": 3, "claimed_pct": "70.00",
         "actual_pct": "75.00", "calibration_error_pp": "5.00"},
        {"bucket": "80-100", "n": 4, "hit_count": 2, "claimed_pct": "90.00",
         "actual_pct": "50.00", "calibration_error_pp": "40.00"},
    ]


# --- calibration_gap_json (rolling, signed, gated) ------------------------------


def test_calibration_gap_var_gated_below_eight_scored(
    golden_db: sqlite3.Connection,
) -> None:
    for i in range(7):
        _score(golden_db, i, 80, False)
    assert _calibration_gap_var(golden_db) == {"unavailable": True, "last_as_of": None}


def test_calibration_gap_var_negative_sign_hand_checked(
    golden_db: sqlite3.Connection,
) -> None:
    # 8 rows @ confidence 80, 6 hits → claimed 0.80, actual 0.75 → gap −0.05.
    for i in range(8):
        _score(golden_db, i, 80, i >= 6)
    assert _calibration_gap_var(golden_db) == {
        "gap": "-0.050", "window_n": 8,
        # W7.1 — the direction ships as copyable text, not as a sign the model must read.
        "reading": "最近 8 筆平均高估自己 5.0 個百分點",
    }


def test_calibration_gap_var_positive_sign_and_the_rolling_window(
    golden_db: sqlite3.Connection,
) -> None:
    # 5 OLD rows @100 all miss (would drag the gap negative), then 20 NEW rows @50
    # with 15 hits — the rolling window of 20 must EXCLUDE the old five.
    for i in range(5):
        _score(golden_db, i, 100, True)
    for i in range(5, 25):
        _score(golden_db, i, 50, i >= 20)
    # Window = the 20 newest: claimed 0.50, actual 15/20 = 0.75 → +0.25.
    assert _calibration_gap_var(golden_db) == {
        "gap": "+0.250", "window_n": 20,
        "reading": "最近 20 筆平均低估自己 25.0 個百分點",
    }


# --- signal_backtest_json (the per-symbol event study) ---------------------------


def _register_with_history(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name=symbol,
    ))
    upsert_prices(
        conn,
        [PriceRow(instrument=symbol, market=Market.US,
                  as_of=_END - timedelta(days=319 - i),
                  close=Decimal(100 + i), source="test")
         for i in range(320)],
        fetched_at=GOLDEN_NOW,
    )
    scan_signals(conn, now=GOLDEN_NOW)


def test_signal_backtest_var_unavailable_without_history(
    golden_db: sqlite3.Connection,
) -> None:
    upsert_instrument(golden_db, Instrument(
        symbol="THIN", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="THIN",
    ))
    assert _signal_backtest_var(
        golden_db, "THIN", actions=load_action_index(golden_db), now=GOLDEN_NOW
    ) == {
        "unavailable": True, "last_as_of": None,
    }


def test_signal_backtest_var_populated_after_scan(
    golden_db: sqlite3.Connection,
) -> None:
    _register_with_history(golden_db, "WATCH")
    out = _signal_backtest_var(
        golden_db, "WATCH", actions=load_action_index(golden_db), now=GOLDEN_NOW
    )
    assert out["symbol"] == "WATCH"
    assert out["history"]["rows"] == 61
    assert out["history"]["params_version"] == "rules-v1"
    # ⑪ (2026-08-25): was [20, 60, 120]; +10 leads now — the scoring-window alignment.
    assert out["windows"] == [10, 20, 60, 120]
    # The baseline rides the FULL-span closes (61 dates): window 20 → 41 valid starts,
    # window 120 → none (censored to None). Monotonic ascent → every sample positive.
    assert out["baseline"]["20"]["n"] == 41
    assert out["baseline"]["20"]["pct_positive"] == "1.0000"
    assert out["baseline"]["120"] is None
    # A monotonic series fires NO events (first sightings are silent seeds).
    assert out["groups"] == []
    assert out["events_without_price"] == 0
    # The payload is JSON-safe end to end (the variables layer's own encoder).
    from portfolio_dash.shared.wire import to_wire
    json.dumps(to_wire(out), ensure_ascii=False)


def test_signal_backtest_var_renders_through_preview(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _register_with_history(golden_db, "WATCH")
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{signal_backtest_json}}", "scope": "per_symbol",
              "symbol": "WATCH"},
    )
    assert r.status_code == 200
    value = json.loads(r.json()["rendered"])
    assert value["symbol"] == "WATCH" and "baseline" in value and "history" in value


def test_ai_self_vars_render_through_preview(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    for i in range(8):
        _score(golden_db, i, 80, i >= 6)
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{calibration_gap_json}}", "scope": "portfolio"},
    )
    assert r.status_code == 200
    assert json.loads(r.json()["rendered"]) == {
        "gap": "-0.050", "window_n": 8,
        "reading": "最近 8 筆平均高估自己 5.0 個百分點",
    }


def test_per_symbol_preview_builds_the_action_index_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap #21 at the preview seam: ONE ``load_action_index`` per request.

    Pre-fix the per-symbol preview built it TWICE (``_build_context``'s long-history read
    + ``_signal_backtest_var``'s internal build); the run path (``_per_symbol_ctx``)
    already threads a caller-built index but never passed it in, so generation rebuilt it
    once per symbol. The count pin is the regression lock.
    """
    import portfolio_dash.api.routers.prompts as prompts_mod
    from portfolio_dash.shared.corporate_actions import ActionIndex

    calls = 0

    def counting(conn: sqlite3.Connection) -> ActionIndex:
        nonlocal calls
        calls += 1
        return load_action_index(conn)  # the real one, from its home module

    # String-form setattr: patch the NAME in the prompts module (resolved at call time)
    # while keeping the unpatched original reachable via the holdings import above.
    monkeypatch.setattr(prompts_mod, "load_action_index", counting)
    _register_with_history(golden_db, "WATCH")
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{signal_backtest_json}}", "scope": "per_symbol",
              "symbol": "WATCH"},
    )
    assert r.status_code == 200
    assert calls == 1

    calls = 0
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{calibration_gap_json}}", "scope": "portfolio"},
    )
    assert r.status_code == 200
    assert calls == 0  # portfolio scope deliberately pays zero ledger reads


# --- W7.1: the three fixes for the first live run's output defects ------------------
# Demo, 2026-08-23: 0 of 13 advice cards obeyed the anchoring law; two printed a sub-gate
# cell's same-window BASELINE as its event return; one fabricated a mean outright; and the
# return unit was rendered four different ways in one batch. The producers were correct in
# every case — so the fixes put the SEMANTICS next to the numbers.


def test_backtest_var_carries_a_precomputed_confidence_ceiling(
    golden_db: sqlite3.Connection,
) -> None:
    """The law's arithmetic ships as ONE integer (AI-D33's rule is unchanged — the prompt
    still states it and nothing clamps the model's answer).

    Hand-check under AI-D39 (2026-08-25): 8 rows @70 with 6 hits → overall 75%. The 60-80
    bucket is n=8 (at the gate), actual 75.00 → cap 80.00; every UNSCORED bucket now caps at
    overall+5 = 80 rather than at the old constant 70. Scanning down from 100: 100..81 exceed
    80, and 80 falls in the unscored 80-100 whose cap is exactly 80 → ceiling 80.

    Was 79 before AI-D39, and the difference is entirely the unscored-bucket rule: the old
    constant 70 blocked 80-100, so the answer had to drop into 60-80. The rolling gap is not
    consulted at all now (it was +0.050 here and adjusted nothing either way).
    """
    for i in range(8):
        _score(golden_db, i, 70, i >= 6)
    out = _backtest_var(golden_db)
    assert out["confidence_ceiling"] == 80
    assert "上限" in out["confidence_ceiling_note"]


def test_backtest_var_ceiling_no_longer_charges_the_gap_a_second_time(
    golden_db: sqlite3.Connection,
) -> None:
    """REPLACES test_backtest_var_ceiling_pays_the_penalty_when_over_confident (expected 75).

    8 rows @80 with 6 hits: bucket bounds are [lo, hi), so 80 lands in 80-100 with actual
    75.00 → cap 80.00, and 80 <= 80 → ceiling 80. The old law then deducted the −0.050 rolling
    gap to reach 75 — charging the model for over-confidence that the bucket's own
    ``actual_pct + 5`` cap had already priced in (AI-D39 (a)).
    """
    for i in range(8):
        _score(golden_db, i, 80, i >= 6)
    assert _backtest_var(golden_db)["confidence_ceiling"] == 80


def test_backtest_var_ceiling_collapses_to_the_headroom_on_a_record_of_pure_misses(
    golden_db: sqlite3.Connection,
) -> None:
    """REPLACES test_backtest_var_ceiling_is_zero_on_a_badly_calibrated_record (expected 0).

    8 rows @90 that ALL missed → overall 0%. The 80-100 bucket is at the gate with actual
    0.00 → cap 5.00, and every unscored bucket now caps at overall+5 = 5 as well, so 5 is the
    largest self-consistent value.

    ⚠ A consequence worth naming: with the gap step gone the ceiling can no longer reach 0 —
    ``CEILING_HEADROOM`` is a floor of sorts, at 5, and it is the SAME headroom every bucket
    gets rather than a special case invented here. The advice template's old branch
    「confidence_ceiling 為 0 時…」 became unreachable and was rewritten to key on a LOW
    ceiling instead; dead text in a prompt is worse than dead code, because the model reads it.
    """
    for i in range(8):
        _score(golden_db, i, 90, True)
    assert _backtest_var(golden_db)["confidence_ceiling"] == 5


def test_calibration_gap_reading_names_the_direction_in_words(
    golden_db: sqlite3.Connection,
) -> None:
    """The live weekly card read gap −0.466 as 「低估自身表現」 — the exact opposite — with the
    convention stated in the same prompt section. A signed fraction is one negation away
    from asserting the reverse of the truth, so the direction is now copyable text."""
    for i in range(8):
        _score(golden_db, i, 80, i >= 6)  # claimed 0.80, actual 0.75 → gap −0.05
    out = _calibration_gap_var(golden_db)
    assert out["gap"].startswith("-")
    assert "高估自己" in out["reading"] and "低估" not in out["reading"]


def test_signal_backtest_var_declares_its_unit_next_to_the_numbers(
    golden_db: sqlite3.Connection,
) -> None:
    """The unit lived only in the variable registry's `desc` — UI documentation the model
    never sees — so 0.1336 was printed as 「+0.1336%」, the true value 100× smaller."""
    _register_with_history(golden_db, "UNITS")
    out = _signal_backtest_var(
        golden_db, "UNITS", actions=load_action_index(golden_db), now=GOLDEN_NOW
    )
    assert out["units"]["mean"].startswith("fraction")
    assert "13.36%" in out["units"]["mean"]  # the worked example travels with the payload
    assert set(out["units"]) == {"mean", "median", "pct_positive"}


def test_calibration_gap_reading_agrees_with_the_number_it_describes(
    golden_db: sqlite3.Connection,
) -> None:
    """DISPROOF of two renderings of one quantity disagreeing (found in W7.1's own first
    deploy): a raw gap of −0.4655 wires as "-0.466", so the words must say 46.6 — not the
    46.550 an unquantized derivation produced.

    20 rows @ 65.5 average claimed... built here as 20 rows alternating 65/66 with 9 hits:
    claimed = 65.5/100 = 0.655, actual = 9/20 = 0.45 → gap = −0.205 → "-0.205" / 20.5pp.
    The invariant under test is the AGREEMENT, so it is asserted arithmetically.
    """
    for i in range(20):
        _score(golden_db, i, 65 if i % 2 else 66, i >= 9)
    out = _calibration_gap_var(golden_db)
    magnitude = out["gap"].lstrip("+-")
    pp_from_number = str(Decimal(magnitude) * 100).rstrip("0").rstrip(".")
    pp_in_words = out["reading"].split("自己 ")[1].split(" 個")[0].rstrip("0").rstrip(".")
    assert pp_from_number == pp_in_words, (out["gap"], out["reading"])
