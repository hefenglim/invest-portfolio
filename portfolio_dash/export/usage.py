"""llm_usage + job_runs CSV exports (spec 02). Raw row dumps, date-range filtered."""

import sqlite3

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact

_USAGE_COLS = ["ts", "model", "agent", "input_tokens", "output_tokens", "cost"]
_JOB_COLS = ["id", "job_id", "started_at", "finished_at", "status", "detail"]


def _tag(frm: str | None, to: str | None) -> str:
    return f"{frm or 'all'}_{to or 'all'}"


def _in_range(day: str, frm: str | None, to: str | None) -> bool:
    if frm and day < frm:
        return False
    if to and day > to:
        return False
    return True


def build_llm_usage_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    rows: list[list[str]] = []
    for r in conn.execute(
        "SELECT ts, model, agent, input_tokens, output_tokens, cost "
        "FROM llm_usage ORDER BY ts ASC, id ASC"
    ):
        if not _in_range(str(r["ts"])[:10], frm, to):
            continue
        rows.append([str(r["ts"]), str(r["model"]), str(r["agent"]),
                     str(r["input_tokens"]), str(r["output_tokens"]), str(r["cost"])])
    return csv_artifact(f"llm_usage_{_tag(frm, to)}.csv", header=_USAGE_COLS, rows=rows)


def build_job_runs_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    rows: list[list[str]] = []
    for r in conn.execute(
        "SELECT id, job_id, started_at, finished_at, status, detail "
        "FROM job_runs ORDER BY started_at ASC, id ASC"
    ):
        if not _in_range(str(r["started_at"])[:10], frm, to):
            continue
        rows.append([str(r["id"]), str(r["job_id"]), str(r["started_at"]),
                     "" if r["finished_at"] is None else str(r["finished_at"]),
                     "" if r["status"] is None else str(r["status"]),
                     "" if r["detail"] is None else str(r["detail"])])
    return csv_artifact(f"job_runs_{_tag(frm, to)}.csv", header=_JOB_COLS, rows=rows)
