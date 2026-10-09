"""llm_usage + job_runs CSV exports (spec 02). Raw row dumps, date-range filtered."""

import sqlite3

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.shared.wire import stored_decimal_str

# Trailing columns (owner 2026-09-30, the verifier's R10 observations ③ ④), so a reader of
# the old layout still finds every earlier column where it was: ``usage_estimated`` = 1 when
# the provider sent no usage and the counts are a local estimate; a run's spend as its row
# records it (empty = the run made no AI call).
_USAGE_COLS = ["ts", "model", "agent", "input_tokens", "output_tokens", "cost",
               "usage_estimated"]
_JOB_COLS = ["id", "job_id", "started_at", "finished_at", "status", "detail",
             "cost_usd", "llm_calls", "tokens_in", "tokens_out"]


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
        "SELECT ts, model, agent, input_tokens, output_tokens, cost, usage_estimated "
        "FROM llm_usage ORDER BY ts ASC, id ASC"
    ):
        if not _in_range(str(r["ts"])[:10], frm, to):
            continue
        rows.append([str(r["ts"]), str(r["model"]), str(r["agent"]),
                     str(r["input_tokens"]), str(r["output_tokens"]),
                     # canonical form: rows written before 2026-09-30 stored str(cost),
                     # which is ``5E-7`` for a sub-micro-dollar call
                     stored_decimal_str(r["cost"]) or "",
                     "1" if r["usage_estimated"] else "0"])
    return csv_artifact(f"llm_usage_{_tag(frm, to)}.csv", header=_USAGE_COLS, rows=rows)


def build_job_runs_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    rows: list[list[str]] = []
    for r in conn.execute(
        "SELECT id, job_id, started_at, finished_at, status, detail, cost_usd, llm_calls, "
        "tokens_in, tokens_out FROM job_runs ORDER BY started_at ASC, id ASC"
    ):
        if not _in_range(str(r["started_at"])[:10], frm, to):
            continue
        rows.append([str(r["id"]), str(r["job_id"]), str(r["started_at"]),
                     "" if r["finished_at"] is None else str(r["finished_at"]),
                     "" if r["status"] is None else str(r["status"]),
                     "" if r["detail"] is None else str(r["detail"]),
                     stored_decimal_str(r["cost_usd"]) or "",
                     *("" if r[k] is None else str(r[k])
                       for k in ("llm_calls", "tokens_in", "tokens_out"))])
    return csv_artifact(f"job_runs_{_tag(frm, to)}.csv", header=_JOB_COLS, rows=rows)
