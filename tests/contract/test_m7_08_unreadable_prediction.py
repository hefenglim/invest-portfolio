"""M7-08 — one unreadable ``prediction`` JSON row must not take the whole dashboard down.

``insights_store._card_from_row`` re-validates the stored ``prediction`` blob through the
``Prediction`` schema and the ``InsightCard`` validator ("a prediction requires a
confidence") on EVERY read, and neither raise was caught. So a single row whose blob no
longer fits the schema — a future required field, a narrowed ``Literal``, a corrupt blob,
a NULLed confidence — answered ``GET /api/dashboard`` and ``GET /api/insights`` with a 500
「系統發生未預期的錯誤」 and no clue which row.

Owner ruling (option C): degrade the ROW, never the page. The card keeps its title and
summary, ``prediction`` becomes None, and the record carries ``unreadable: true`` so both
pages draw a 待釐清 pill — the same flag-never-hide posture as ``oversold`` /
``unbookable_dividend`` in ``domain-ledger.md``. Silently serving the row as a narrative
card (option A) was rejected: it would hide a data problem AND let the display path say
something different about the row from what the scoring path says.

Three bad shapes (the explorer's white-box reproduction), two endpoints, plus the
counter-proofs: legal cards are byte-identical beside a bad row, and the scoring surface
(``/api/ai-score``, which reads the raw column) is unaffected.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import insights_store
from portfolio_dash.llm_insight.cards import InsightCard, Prediction

_TPE = ZoneInfo("Asia/Taipei")


def _at(day: int) -> datetime:
    return datetime(2026, 6, day, 9, 0, tzinfo=_TPE)


_LEGAL_PREDICTION = Prediction(
    metric="price_change", direction="up", target_pct=Decimal("0.05"), horizon_days=5
)
_LEGAL_PREDICTION_WIRE = {
    "metric": "price_change", "direction": "up", "target_pct": "0.05", "horizon_days": 5,
}

# (prediction column, confidence column) — each one is a row `add_card` can never write,
# because `InsightCard` validates on the way in; they reach the table only through schema
# drift or a hand edit, which is exactly why the READ has to tolerate them.
BAD_SHAPES: dict[str, tuple[str, int | None]] = {
    "A_missing_required_fields": ('{"direction": "up"}', 70),
    "B_confidence_null": (
        '{"metric": "price_change", "direction": "up", "horizon_days": 5}', None,
    ),
    "C_not_json": ("this is not json", 70),
}

# endpoint id -> (url, how to pick the card list out of the body)
ENDPOINTS: dict[str, tuple[str, Callable[[dict[str, Any]], list[dict[str, Any]]]]] = {
    "insights": ("/api/insights?scope=portfolio", lambda body: list(body["rows"])),
    "dashboard": ("/api/dashboard", lambda body: list(body["insights"])),
}


def _insert_raw(
    conn: sqlite3.Connection, *, title: str, prediction: str | None, confidence: int | None,
    when: datetime, symbol: str | None = None,
) -> int:
    """Insert a row straight into ``insights`` (bypassing ``add_card``'s validation)."""
    cur = conn.execute(
        "INSERT INTO insights (insight_type_id, symbol, is_shadow, calibration_version, "
        "fingerprint, title, summary, body_md, tags, confidence, prediction, horizon_days, "
        "due_at, input_snapshot, model, cost_usd, created_at) "
        "VALUES (1, ?, 0, NULL, ?, ?, ?, ?, '[]', ?, ?, 5, NULL, '{}', 'm', '0.0021', ?)",
        (symbol, f"fp-{title}", title, f"{title} summary", f"# {title}", confidence,
         prediction, when.isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _add_legal(
    conn: sqlite3.Connection, *, title: str, when: datetime, with_prediction: bool,
) -> insights_store.InsightRecord:
    card = InsightCard(
        title=title, summary=f"{title} summary", body_md=f"# {title}", tags=["t"],
        symbol=None,
        confidence=70 if with_prediction else None,
        prediction=_LEGAL_PREDICTION if with_prediction else None,
    )
    return insights_store.add_card(
        conn, insight_type_id=1, card=card,
        fingerprint=insights_store.fingerprint(1, title, "d", "v1"),
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal("0.0021"), now=when,
    )


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize("shape", sorted(BAD_SHAPES))
def test_bad_prediction_row_degrades_to_a_flagged_card_not_a_500(
    golden_db: sqlite3.Connection, api_client: TestClient, shape: str, endpoint: str,
) -> None:
    url, pick = ENDPOINTS[endpoint]
    prediction, confidence = BAD_SHAPES[shape]
    _add_legal(golden_db, title="legal", when=_at(10), with_prediction=True)
    _insert_raw(golden_db, title=f"bad {shape}", prediction=prediction,
                confidence=confidence, when=_at(11))

    r = api_client.get(url)
    assert r.status_code == 200, r.text
    by_title = {row["title"]: row for row in pick(r.json())}

    bad = by_title[f"bad {shape}"]          # the row is SHOWN — title kept, never hidden
    assert bad["unreadable"] is True
    assert bad["summary"] == f"bad {shape} summary"   # the narrative columns are intact
    if endpoint == "insights":
        assert bad["prediction"] is None    # the unreadable blob is dropped, not guessed

    good = by_title["legal"]
    assert good["unreadable"] is False


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_legal_cards_are_byte_identical_beside_bad_rows(
    golden_db: sqlite3.Connection, api_client: TestClient, endpoint: str,
) -> None:
    """Counter-proof: a bad row flags ITSELF only — no cross-row pollution, no reorder."""
    url, pick = ENDPOINTS[endpoint]
    _add_legal(golden_db, title="legal-pred", when=_at(10), with_prediction=True)
    _add_legal(golden_db, title="legal-narr", when=_at(11), with_prediction=False)
    before = pick(api_client.get(url).json())
    assert [row["title"] for row in before] == ["legal-narr", "legal-pred"]
    if endpoint == "insights":
        assert before[1]["confidence"] == 70
        assert before[1]["prediction"] == _LEGAL_PREDICTION_WIRE
        assert before[0]["confidence"] is None and before[0]["prediction"] is None

    # The bad rows are OLDER than the legal ones, so the dashboard's newest-3 cap still
    # leads with the two legal cards and the 3rd slot proves a bad row can sit beside them.
    for i, shape in enumerate(sorted(BAD_SHAPES)):
        prediction, confidence = BAD_SHAPES[shape]
        _insert_raw(golden_db, title=f"bad {shape}", prediction=prediction,
                    confidence=confidence, when=_at(1 + i))

    r = api_client.get(url)
    assert r.status_code == 200, r.text
    after = pick(r.json())
    # Each endpoint keeps ITS OWN order: /api/insights lists by id desc (the bad rows were
    # inserted last, so they lead), /api/dashboard by created_at desc capped at 3 (the two
    # legal cards lead, one bad row fills the 3rd slot).
    expected_titles = (
        ["bad C_not_json", "bad B_confidence_null", "bad A_missing_required_fields",
         "legal-narr", "legal-pred"]
        if endpoint == "insights"
        else ["legal-narr", "legal-pred", "bad C_not_json"]
    )
    assert [row["title"] for row in after] == expected_titles
    legal_after = [row for row in after if not row["unreadable"]]
    assert legal_after == before            # exact: prediction, confidence, order, every key
    assert all(row["unreadable"] is True for row in after if row not in legal_after)


def test_ai_score_is_unaffected_by_bad_rows(
    golden_db: sqlite3.Connection, api_client: TestClient,
) -> None:
    """The scoring surface reads the raw column (its own per-row guard) — unchanged."""
    before = api_client.get("/api/ai-score")
    assert before.status_code == 200
    for i, shape in enumerate(sorted(BAD_SHAPES)):
        prediction, confidence = BAD_SHAPES[shape]
        _insert_raw(golden_db, title=f"bad {shape}", prediction=prediction,
                    confidence=confidence, when=_at(1 + i))
    after = api_client.get("/api/ai-score")
    assert after.status_code == 200
    assert after.json() == before.json()
