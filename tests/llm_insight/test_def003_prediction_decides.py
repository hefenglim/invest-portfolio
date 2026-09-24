"""DEF-003 (functional test G-06, owner ruling 2026-09-24): the PREDICTION decides.

Measured on the demo: AI 洞察 › 持倉健診 › 2884 (#182) printed 「信心 0%」 while
``GET /api/insights`` served that card with ``prediction: null, confidence: 0``; 25 of the 40
prediction-less cards stored a confidence (0 / 40 / 55 / 80 / 100). The page decided on
``confidence == null`` alone.

Ruling: a card whose prediction is null always shows 「純描述・無預測」, never a confidence, and
never enters the calibration curve. Cards are append-only, so the stored value is IGNORED AT
READ TIME (``insights_store._card_from_row``) — and every consumer that reads a confidence
population is pinned here, one parametrised case each, against the same fixture: one
predicted card and one prediction-less card that both state a confidence and both carry a
scored evaluation. Each consumer must count the first and never the second.
"""

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_WEB = Path(__file__).resolve().parents[2] / "web"


def _pred() -> Prediction:
    return Prediction(metric="price_change", direction="up", horizon_days=5)


def _add(conn: sqlite3.Connection, *, predicted: bool, confidence: int, fp: str,
         body: str = "b") -> int:
    rec = istore.add_card(
        conn, insight_type_id=1, fingerprint=fp, calibration_version=1,
        card=InsightCard(title="t", summary="s", body_md=body, symbol="2884",
                         confidence=confidence, prediction=_pred() if predicted else None),
        horizon_days=5, input_snapshot="x", model="m", cost_usd=Decimal("0"), now=NOW,
        ceiling_at_create=50,
    )
    return rec.id


def _score(conn: sqlite3.Connection, insight_id: int, confidence: int, miss: bool) -> None:
    es.add_evaluation(
        conn, insight_id=insight_id, insight_type_id=1, calibration_version=1,
        is_shadow=False, status="scored", quant_hit=not miss, narrative_score=None,
        miss=miss, actual_value=Decimal("0.01"), confidence=confidence, now=NOW,
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    istore.ensure_tables(c)
    es.ensure_tables(c)
    # a predicted card: confidence 90, over its ceiling, scored a HIT
    pid = _add(c, predicted=True, confidence=90, fp="p")
    _score(c, pid, 90, miss=False)
    # a prediction-less card that nonetheless stored confidence 10 — the demo's shape — with
    # an evaluation row carrying it (a legacy narrative scoring), scored a MISS
    nid = _add(c, predicted=False, confidence=10, fp="n")
    _score(c, nid, 10, miss=True)
    c.execute("UPDATE insights SET due_at = ? WHERE id = ?", (NOW.isoformat(), nid))
    c.commit()
    yield c
    c.close()


# --- the read seam -----------------------------------------------------------------


def test_a_card_without_a_prediction_reads_with_no_confidence(conn: sqlite3.Connection) -> None:
    by_id = {r.id: r for r in istore.list_cards(conn)}
    predicted, narrative = by_id[1], by_id[2]
    assert predicted.card.confidence == 90
    assert narrative.card.prediction is None and narrative.card.confidence is None
    assert narrative.stated_confidence == 10  # stored as emitted — append-only, never rewritten
    assert conn.execute("SELECT confidence FROM insights WHERE id = 2").fetchone()[0] == 10


# --- every confidence population --------------------------------------------------------


def _bins(c: sqlite3.Connection) -> Any:
    return [(b["bucket"], b["n"]) for b in es.calibration_bins(c)]


def _gap_pairs(c: sqlite3.Connection) -> Any:
    return es.scored_confidence_hits(c)


def _recent(c: sqlite3.Connection) -> Any:
    return es.recent_confidence_hits(c, limit=20)


def _rolling(c: sqlite3.Connection) -> Any:
    return es.rolling_calibration_gap(c, min_scored=1).model_dump()


def _ceiling(c: sqlite3.Connection) -> Any:
    return es.ceiling_violations(c)


def _score_rows(c: sqlite3.Connection) -> Any:
    return sorted((r["insight_id"], r["confidence"])
                  for r in es.ai_score(c, min_samples=1)["rows"])


def _due(c: sqlite3.Connection) -> Any:
    # a legacy prediction-less card that matured and was never scored (due_at set by hand —
    # the shape a pre-04.10 row can have): the scorer must not be handed its confidence
    nid = _add(c, predicted=False, confidence=30, fp="due")
    c.execute("UPDATE insights SET due_at = ? WHERE id = ?", (NOW.isoformat(), nid))
    c.commit()
    return [(d.insight_id - nid + 2, d.confidence)
            for d in es.due_insights(c, now=NOW + timedelta(1)) if d.insight_id == nid]


def _miss_samples(c: sqlite3.Connection) -> Any:
    return [(s["insight_id"], s["confidence"])
            for s in es.miss_samples_for_version(c, insight_type_id=1, version=1)]


_CASES = {
    # consumer: (what it must return — the predicted card only, never the narrative one)
    "calibration_bins (校準曲線)": (_bins, [("80-100", 1)]),
    "scored_confidence_hits (calib_gap / backtest anchor)": (_gap_pairs, [(90, True)]),
    "recent_confidence_hits (rolling window)": (_recent, [(90, True)]),
    "rolling_calibration_gap": (_rolling, {"gap": Decimal("0.1"), "window_n": 1}),
    "ceiling_violations (信心上限違規率)": (_ceiling, {"n": 1, "violations": 1,
                                                    "rate": "1.0000"}),
    "ai_score rows (預測明細 + CSV export)": (_score_rows, [(1, 90), (2, None)]),
    "due_insights (the scorer's confidence source)": (_due, [(2, None)]),
    "miss_samples_for_version (Loop-3 master input)": (_miss_samples, [(2, None)]),
}


@pytest.mark.parametrize("name", list(_CASES))
def test_a_prediction_less_card_never_enters_a_confidence_population(
    conn: sqlite3.Connection, name: str
) -> None:
    fn, want = _CASES[name]
    assert fn(conn) == want


def test_without_an_insights_table_the_populations_degrade_not_crash() -> None:
    """An evaluations-only connection is a real shape (architecture.md): no card is KNOWN to
    be prediction-less there, so nothing is excluded and nothing raises."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    es.ensure_tables(c)
    _score(c, 7, 60, miss=False)
    assert es.scored_confidence_hits(c) == [(60, True)]
    assert [b["n"] for b in es.calibration_bins(c)] == [1]


# --- the API and the figure check ---------------------------------------------------------


def test_the_api_serves_a_description_without_a_confidence(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    istore.ensure_tables(golden_db)
    _add(golden_db, predicted=True, confidence=55, fp="p1")
    # the body quotes the stored confidence: still the model's own number, not a fabrication
    _add(golden_db, predicted=False, confidence=55, fp="n1", body="模型自評信心 55%")
    rows = {r["prediction"] is not None: r for r in api_client.get("/api/insights").json()["rows"]}
    assert rows[True]["confidence"] == 55
    assert rows[False]["confidence"] is None and rows[False]["prediction"] is None
    assert "55" not in rows[False]["figure_flags"]["unverified_figures"]


# --- the page: 「純描述・無預測」 is decided by the prediction ---------------------------


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _run_chips(cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    src = (_WEB / "insights.html").read_text(encoding="utf-8")
    code = (
        "function el(tag, cls, text) { return { tag, cls, text: text == null ? '' : text,"
        " kids: [], title: '', href: '', appendChild(k) { this.kids.push(k); },"
        " get textContent() { return this.text + this.kids.map((k) => k.textContent ||"
        " k.text || '').join(''); } }; }\n"
        "const f = { num: (x, d) => String(x) };\n"
        "document = { createTextNode: (t) => ({ text: t, textContent: t }) };\n"
        + _slice(src, "  function unreadablePill(", "  /* M9: the server's read-time")
        + _slice(src, "  function promptVersionChip(", "  function cardNode(")
        + "const cards = " + json.dumps(cards, ensure_ascii=False) + ";\n"
        + "const out = {};\n"
        + "for (const [k, c] of Object.entries(cards)) {\n"
        + "  const conf = confChip(c); const pv = promptVersionChip(c);\n"
        + "  out[k] = { conf: conf && conf.textContent, pv: pv && pv.textContent,"
        + " pvHref: pv && pv.href };\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    done = subprocess.run([str(node), "-e", code], capture_output=True, timeout=30,
                          encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def test_the_chip_reads_the_prediction_not_the_confidence() -> None:
    pred = {"metric": "price_change", "direction": "up", "target_pct": None,
            "horizon_days": 5}
    got = _run_chips({
        # the demo's #182: prediction null, confidence 0 — must NOT print 「信心 0%」
        "narrative_with_stale_conf": {"prediction": None, "confidence": 0},
        "narrative": {"prediction": None, "confidence": None},
        "predicted": {"prediction": pred, "confidence": 55},
        "predicted_zero": {"prediction": pred, "confidence": 0},
    })
    assert got["narrative_with_stale_conf"]["conf"] == "純描述・無預測"
    assert got["narrative"]["conf"] == "純描述・無預測"
    assert got["predicted"]["conf"] == "信心 55%"
    assert got["predicted_zero"]["conf"] == "信心 0%"  # a real 0% self-assessment stays


def test_the_card_names_the_prompt_version_it_was_generated_with() -> None:
    """DEF-033's page half on the same card foot: the recorded version, or 「未記錄」."""
    got = _run_chips({
        "legacy": {"prediction": None, "confidence": None, "prompt_versions": None},
        "anomaly": {"prediction": None, "confidence": None, "prompt_versions": []},
        "one": {"prediction": None, "confidence": None,
                "prompt_versions": [{"strategy_id": 3, "name": "持倉健診", "version": 4}]},
        "two": {"prediction": None, "confidence": None,
                "prompt_versions": [{"strategy_id": 3, "name": "甲", "version": 4},
                                    {"strategy_id": 5, "name": "乙", "version": None}]},
    })
    assert got["legacy"]["pv"] == "生成時版本未記錄"
    assert got["anomaly"]["pv"] is None
    assert got["one"]["pv"] == "提示詞 v4" and got["one"]["pvHref"] == "settings.html#prompts"
    assert got["two"]["pv"] == "提示詞 v4・版本不明"


def test_the_scorer_writes_no_confidence_for_a_narrative_card(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write site's own guard, for a caller that hands the scorer a DueInsight directly
    (bypassing ``due_insights``): a narrative card is scored on its narrative, and its
    evaluation row carries NO confidence — so it can never reach a confidence population."""
    from portfolio_dash.api import insight_service as svc
    from portfolio_dash.llm_insight import composer_store as cs
    from portfolio_dash.llm_insight import master
    from portfolio_dash.shared.corporate_actions import ActionIndex
    from portfolio_dash.shared.enums import Currency

    cs.ensure_seeded(conn)
    monkeypatch.setattr(master, "score_narrative",
                        lambda **kw: {"narrative_score": 80, "note": "ok"})
    due = es.DueInsight(
        insight_id=2, insight_type_id=1, symbol="2884", calibration_version=1,
        is_shadow=False, confidence=10, prediction=None, due_at=NOW.isoformat(),
        created_at=NOW.isoformat(),
    )
    svc._score_one(conn, due, master_configured=True, now=NOW,
                   actions=ActionIndex.build([]), reporting=Currency.TWD)
    row = conn.execute(
        "SELECT status, narrative_score, confidence FROM insight_evaluations "
        "WHERE insight_id = 2 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row["status"], row["narrative_score"], row["confidence"]) == ("scored", 80, None)
