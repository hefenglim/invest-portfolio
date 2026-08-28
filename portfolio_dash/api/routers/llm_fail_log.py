"""The AI extraction failure log: per-category counts, a .jsonl export, and a clear (AI-D64).

Three routes over ``shared.llm_fail_log``. The store is a bounded ring the LLM seam writes
to whenever a call fails (and whenever it confesses rows it could not classify), so this
router only reads, packages and empties -- it computes nothing.

Deliberately NOT in ``export.py``: ``tests/contract/test_export_endpoints_have_callers.py``
treats every ``@router.post("/export/...")`` there as a dashboard export needing a caller,
and this is an admin diagnostic, not a report.

⚠ The clear route touches ``llm_fail_log`` and nothing else. Remaining LLM budget is
``sum(topups) - sum(llm_usage.cost)``, so a "tidy up the AI records" that reached
``llm_usage`` would silently hand the user free budget. Pinned by a test, twice -- once at
the store and once here over HTTP.
"""

import sqlite3

from fastapi import APIRouter, Depends, Query, Response

from portfolio_dash.api.deps import get_conn
from portfolio_dash.export.artifact import content_disposition
from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.clock import app_now

router = APIRouter()

#: How many recent rows the panel previews. The full set goes out via the export.
_PREVIEW = 20


@router.get("/llm-fail-log")
def summary(
    limit: int = Query(_PREVIEW, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """Per-agent counts, the ring's capacity, and a preview of the newest rows.

    ``oldest`` is what tells the user when to download: rows roll off silently once the
    ring is full, so a date creeping forward is the signal that the tail is being lost.
    """
    rows = fail_log.list_rows(conn, limit=limit)
    for r in rows:  # the preview is a list, not a reader — keep the payload small
        for field in ("prompt", "raw_output"):
            text = str(r.get(field) or "")
            r[field] = text[:400]
            r[field + "_len"] = len(text)
    return {
        "total": fail_log.total_count(conn),
        "capacity": fail_log._KEEP,
        "by_agent": fail_log.counts_by_agent(conn),
        "recent": rows,
    }


@router.post("/llm-fail-log/export")
def export_jsonl(
    agent: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """The whole log as JSON Lines — one complete record per line, nothing truncated.

    This is the fine-tuning / re-test artefact, so unlike the summary above it carries the
    full prompt and the model's verbatim reply.
    """
    rows = fail_log.list_rows(conn, agent=agent)
    body = fail_log.to_jsonl(rows).encode("utf-8")
    stamp = app_now().date().isoformat()
    name = f"llm_fail_log_{agent}_{stamp}.jsonl" if agent else f"llm_fail_log_{stamp}.jsonl"
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": content_disposition(name)},
    )


@router.delete("/llm-fail-log")
def clear(
    agent: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """Empty the log (or one agent's rows). Never touches ``llm_usage`` — see module doc."""
    return {"ok": True, "deleted": fail_log.delete_all(conn, agent=agent)}
