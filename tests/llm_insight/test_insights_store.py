"""Unit tests for the insights store (spec 04.10): table + fingerprint cache + due_at.

Append-only insight cards keyed by an input fingerprint (same inputs same trading day →
cache hit, zero LLM). ``due_at`` is the prediction maturity date (trading-day or
calendar-day horizon); a pure-narrative card has no prediction → due_at is NULL. No money
in float — cost_usd / target_pct persist as Decimal strings.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import insights_store as store
from portfolio_dash.llm_insight.cards import InsightCard, Prediction

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))  # a Thursday


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.ensure_tables(c)
    yield c
    c.close()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- schema -------------------------------------------------------------------


def test_ensure_tables_creates_insights_and_is_idempotent(conn: sqlite3.Connection) -> None:
    assert "insights" in _tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(insights)")}
    assert {
        "id", "insight_type_id", "symbol", "is_shadow", "calibration_version",
        "fingerprint", "title", "summary", "body_md", "tags", "confidence",
        "prediction", "horizon_days", "due_at", "input_snapshot", "model", "cost_usd",
        "created_at",
    } <= cols
    store.ensure_tables(conn)  # idempotent
    assert "insights" in _tables(conn)


def test_empty_store_lists_nothing(conn: sqlite3.Connection) -> None:
    assert store.list_cards(conn) == []


# --- fingerprint --------------------------------------------------------------


def test_fingerprint_is_stable_sha256_hex(conn: sqlite3.Connection) -> None:
    fp1 = store.fingerprint(1, "assembled prompt", "snapdigest", "v3")
    fp2 = store.fingerprint(1, "assembled prompt", "snapdigest", "v3")
    assert fp1 == fp2
    assert len(fp1) == 64 and all(ch in "0123456789abcdef" for ch in fp1)


def test_fingerprint_varies_with_each_input(conn: sqlite3.Connection) -> None:
    base = store.fingerprint(1, "a", "d", "v1")
    assert store.fingerprint(2, "a", "d", "v1") != base  # insight_type_id
    assert store.fingerprint(1, "b", "d", "v1") != base  # assembled
    assert store.fingerprint(1, "a", "e", "v1") != base  # snapshot digest
    assert store.fingerprint(1, "a", "d", "v2") != base  # prompt version


# --- add_card + find_by_fingerprint -------------------------------------------


def _narrative_card() -> InsightCard:
    return InsightCard(title="t", summary="s", body_md="b", tags=["x"])


def _prediction_card() -> InsightCard:
    return InsightCard(
        title="t", summary="s", body_md="b", tags=["x"], symbol="2330", confidence=70,
        prediction=Prediction(
            metric="price_change", direction="up", target_pct=Decimal("0.05"),
            horizon_days=5,
        ),
    )


def test_add_card_and_find_by_fingerprint(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(1, "prompt", "digest", "v1")
    store.add_card(
        conn, insight_type_id=1, card=_narrative_card(), fingerprint=fp,
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="gpt",
        cost_usd=Decimal("0.0012"), now=NOW,
    )
    found = store.find_by_fingerprint(conn, fp)
    assert found is not None
    assert found.card.title == "t"
    assert found.symbol is None
    assert found.card.symbol is None
    # a different fingerprint misses
    assert store.find_by_fingerprint(conn, "deadbeef") is None


def test_add_card_persists_decimal_cost_as_string(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(2, "p", "d", "v1")
    store.add_card(
        conn, insight_type_id=2, card=_prediction_card(), fingerprint=fp,
        calibration_version=3, horizon_days=5, input_snapshot="{}", model="gpt",
        cost_usd=Decimal("0.0034"), now=NOW,
    )
    row = conn.execute("SELECT cost_usd, prediction FROM insights").fetchone()
    assert row["cost_usd"] == "0.0034"  # Decimal -> canonical string, never float
    assert '"target_pct": "0.05"' in row["prediction"] or '"target_pct":"0.05"' in row["prediction"]


def test_card_roundtrips_prediction_and_confidence(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(2, "p", "d", "v1")
    store.add_card(
        conn, insight_type_id=2, card=_prediction_card(), fingerprint=fp,
        calibration_version=3, horizon_days=5, input_snapshot="{}", model="gpt",
        cost_usd=Decimal("0.0034"), now=NOW,
    )
    got = store.find_by_fingerprint(conn, fp)
    assert got is not None
    assert got.card.confidence == 70
    assert got.card.prediction is not None
    assert got.card.prediction.target_pct == Decimal("0.05")
    assert got.calibration_version == 3


# --- due_at (trading-day horizon) ---------------------------------------------


def test_due_at_trading_days_skips_weekend(conn: sqlite3.Connection) -> None:
    # NOW is Thu 2026-06-11; +5 trading days -> Fri12, Mon15, Tue16, Wed17, Thu18.
    fp = store.fingerprint(3, "p", "d", "v1")
    rec = store.add_card(
        conn, insight_type_id=3, card=_prediction_card(), fingerprint=fp,
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0"), now=NOW, horizon_basis="trading_days",
    )
    assert rec.due_at is not None
    assert rec.due_at.startswith("2026-06-18")


def test_due_at_calendar_days(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(4, "p", "d", "v1")
    rec = store.add_card(
        conn, insight_type_id=4, card=_prediction_card(), fingerprint=fp,
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0"), now=NOW, horizon_basis="calendar_days",
    )
    assert rec.due_at is not None
    assert rec.due_at.startswith("2026-06-16")  # Thu + 5 calendar days = Tue 16


def test_narrative_card_has_null_due_at(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(5, "p", "d", "v1")
    rec = store.add_card(
        conn, insight_type_id=5, card=_narrative_card(), fingerprint=fp,
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0"), now=NOW, horizon_basis="trading_days",
    )
    assert rec.due_at is None  # no prediction -> no maturity date


def test_card_horizon_override_used_for_due_at(conn: sqlite3.Connection) -> None:
    # The card's prediction.horizon_days (3) overrides the task default (5) for due_at.
    card = InsightCard(
        title="t", summary="s", body_md="b", tags=[], confidence=60,
        prediction=Prediction(metric="price_change", direction="up", horizon_days=3),
    )
    fp = store.fingerprint(6, "p", "d", "v1")
    rec = store.add_card(
        conn, insight_type_id=6, card=card, fingerprint=fp, calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0"), now=NOW,
        horizon_basis="trading_days",
    )
    # Thu11 +3 trading days -> Fri12, Mon15, Tue16.
    assert rec.due_at is not None and rec.due_at.startswith("2026-06-16")


# --- list_cards filters -------------------------------------------------------


def test_list_cards_filters_by_type_and_symbol(conn: sqlite3.Connection) -> None:
    store.add_card(
        conn, insight_type_id=1, card=_narrative_card(),
        fingerprint=store.fingerprint(1, "a", "d", "v1"), calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0"), now=NOW,
    )
    store.add_card(
        conn, insight_type_id=1, card=_prediction_card(),
        fingerprint=store.fingerprint(1, "b", "d", "v1"), calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0"), now=NOW,
    )
    store.add_card(
        conn, insight_type_id=2, card=_narrative_card(),
        fingerprint=store.fingerprint(2, "a", "d", "v1"), calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0"), now=NOW,
    )
    assert len(store.list_cards(conn)) == 3
    assert len(store.list_cards(conn, insight_type_id=1)) == 2
    by_symbol = store.list_cards(conn, symbol="2330")
    assert len(by_symbol) == 1 and by_symbol[0].symbol == "2330"


# --- latest_cards (dashboard embed) -------------------------------------------


def _add_at(
    conn: sqlite3.Connection, *, insight_type_id: int, title: str, when: datetime,
    is_shadow: bool = False,
) -> None:
    """Add one narrative card stamped at *when* (created_at drives the latest_cards order)."""
    card = InsightCard(title=title, summary="s", body_md="b", tags=[])
    store.add_card(
        conn, insight_type_id=insight_type_id, card=card,
        fingerprint=store.fingerprint(insight_type_id, title, "d", "v1"),
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0"), now=when, is_shadow=is_shadow,
    )


def test_latest_cards_empty_store_is_empty(conn: sqlite3.Connection) -> None:
    assert store.latest_cards(conn, 3) == []


def test_latest_cards_caps_at_n_newest_first_and_excludes_shadow(
    conn: sqlite3.Connection,
) -> None:
    # Four non-shadow cards on distinct days + one shadow card on the newest day.
    _add_at(conn, insight_type_id=1, title="oldest", when=datetime(2026, 6, 8, 9, 0))
    _add_at(conn, insight_type_id=1, title="older", when=datetime(2026, 6, 9, 9, 0))
    _add_at(conn, insight_type_id=1, title="newer", when=datetime(2026, 6, 10, 9, 0))
    _add_at(conn, insight_type_id=1, title="newest", when=datetime(2026, 6, 11, 9, 0))
    _add_at(
        conn, insight_type_id=1, title="shadow", when=datetime(2026, 6, 11, 9, 0),
        is_shadow=True,
    )

    got = store.latest_cards(conn, 3)
    assert len(got) == 3  # capped at N
    assert [r.card.title for r in got] == ["newest", "newer", "older"]  # created_at desc
    assert all(not r.is_shadow for r in got)  # shadow excluded
    assert "shadow" not in {r.card.title for r in got}


def test_latest_cards_id_desc_tiebreak_for_same_created_at(
    conn: sqlite3.Connection,
) -> None:
    same = datetime(2026, 6, 11, 9, 0)
    _add_at(conn, insight_type_id=1, title="first", when=same)
    _add_at(conn, insight_type_id=1, title="second", when=same)
    got = store.latest_cards(conn, 2)
    # Same created_at -> higher id (the later insert, "second") comes first.
    assert [r.card.title for r in got] == ["second", "first"]


def test_latest_cards_returns_fewer_than_n_when_few_rows(conn: sqlite3.Connection) -> None:
    _add_at(conn, insight_type_id=1, title="only", when=datetime(2026, 6, 11, 9, 0))
    got = store.latest_cards(conn, 5)
    assert len(got) == 1
    assert got[0].card.title == "only"
    assert got[0].cost_usd == "0"  # cost passes through as a canonical Decimal string


def test_add_card_records_is_shadow_flag(conn: sqlite3.Connection) -> None:
    fp = store.fingerprint(7, "p", "d", "v1")
    store.add_card(
        conn, insight_type_id=7, card=_narrative_card(), fingerprint=fp,
        calibration_version=2, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0"), now=NOW, is_shadow=True,
    )
    row = conn.execute("SELECT is_shadow, calibration_version FROM insights").fetchone()
    assert row["is_shadow"] == 1
    assert row["calibration_version"] == 2
