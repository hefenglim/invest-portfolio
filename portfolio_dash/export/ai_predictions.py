"""AI-prediction battle-record export (reconciliation channel).

Source of truth: ``llm_insight.evaluations_store.ai_score`` — the SAME store function
that feeds the AI 洞察「預測明細」score table (``GET /api/ai-score``). This builder reads
the store directly (not the HTTP layer), mirrors the table's columns, and emits the
whole archived-excluded set (the table is paged; the export is complete). Archived
insight-task rows are excluded here exactly as the on-screen table excludes them.

Retires the client-side display dump (``web/export.js`` ``tableButton`` over the
rendered ``#score-table``) as the reconciliation data source: that path scraped the
CURRENT page of DISPLAY glyphs (「生效」/「影子」, ✓/✗, 「命中」/「失誤」, signed-pct) out of
the DOM. Per the owner directive (2026-07-14) the export now comes straight from the
evaluations store at source precision (raw Decimal/int strings, one row per stored
evaluation), not from rendered cells.
"""

import sqlite3
from typing import Any

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es

# Mirrors the 預測明細 table's data source (``_row_wire``): task/calib/shadow/quant/
# narrative/result/actual/evaluated + the reconciliation ids (insight_id, status,
# confidence) the DOM table omits.
_COLUMNS = [
    "insight_type_id", "insight_id", "calibration_version", "is_shadow", "status",
    "quant_hit", "narrative_score", "miss", "actual_value", "confidence", "evaluated_at",
]


def _s(value: object) -> str:
    """Raw cell: value -> str; None -> empty; bool -> 'true'/'false' (never 'True')."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_ai_predictions_csv(conn: sqlite3.Connection) -> ExportArtifact:
    es.ensure_tables(conn)
    cs.ensure_seeded(conn)
    # rows_limit=None -> the whole (archived-excluded) set, matching the table's source
    # before its client-side paging slice.
    score: dict[str, Any] = es.ai_score(
        conn, exclude_type_ids=cs.archived_type_ids(conn), rows_limit=None
    )
    rows: list[list[str]] = [
        [_s(r.get(col)) for col in _COLUMNS] for r in score["rows"]
    ]
    return csv_artifact("ai_predictions.csv", header=_COLUMNS, rows=rows)
