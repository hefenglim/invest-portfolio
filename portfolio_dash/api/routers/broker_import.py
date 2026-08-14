"""Broker statement → this app's import CSVs, over HTTP. The web door to the converter.

The offline CLI (``scripts/schwab_convert.py``) came first and stays. This endpoint exists
because the CLI asks the owner to open a terminal, run a script with three required
arguments, then upload three to five files **in dependency order** — and the one moment they
will actually do that is the moment they are loading five years of real broker history into
an empty ledger. A door that is hard to use at exactly that moment is not really a door.

**One conversion, two callers.** Everything here is a thin wrapper over
``data_ingestion/broker/convert.py``: parse → group → reconcile → rows. The CLI calls the
same functions and its output is byte-identical, which is what
``tests/scripts/test_schwab_convert.py`` pins.

**Nothing is written, and nothing is stored.** This endpoint returns CSV *text*; the browser
feeds it back through the ordinary ``/api/import/preview`` → ``/api/import/commit`` path, so
the converted rows meet **every** validation a hand-made CSV meets, get the same duplicate
detection, and land in the same undoable ``import_batches``. A converter with its own write
path would be a second way into the ledger — a second place for the oversell guard, the
fee snapshot and the provenance stamp to be forgotten.

**All or nothing.** A blocking reconcile issue means our own transformation invented or
destroyed money, and the response carries **no CSVs at all** — the same refusal the CLI
makes, for the same reason: a partial import of a file whose arithmetic contradicts itself
leaves a ledger nobody can rebuild.

⚠ **Privacy.** The request body is a real broker statement. It is held in memory for the
duration of the call and written nowhere — not to disk, not to a log. The only fragment that
outlives the request is the file NAME, which rides ``import_batches.source_name`` when the
browser commits, exactly as it already does for a hand-uploaded CSV. This is not new
exposure: the converted CSVs carry the same amounts and were always going to be uploaded.
"""

import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.broker.convert import (
    Conversion,
    convert,
    render_kind,
)
from portfolio_dash.data_ingestion.broker.ir import UnmappedRow
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileIssue, ReconcileReport
from portfolio_dash.data_ingestion.broker.registry import BROKER_IDS, parse_export
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

#: Total characters accepted across all uploaded exports. The owner's real 1,375-row export
#: is ~250 KB, so this is ~20 exports' worth — generous, and still a bound. Unbounded input
#: on a 1 GB VM is a way to take the site down by accident.
_MAX_CHARS = 8_000_000

#: The kinds that become uploadable CSVs, in the order they must be committed.
#:
#: **The order is a dependency, not a preference.** Openings establish positions that later
#: sells rely on; dividends and corporate actions are validated against a position, so they
#: follow the trades that create it. Actions AFTER trades specifically — the reverse was
#: measured on 2026-08-12 and hard-rejected 3 of 5 actions, because a corporate action's own
#: guards need the position to exist. The trades are protected from the resulting 賣超 by
#: ``pending_actions_csv`` (see ``input_center._resolve_builder``), not by reordering.
COMMIT_ORDER: tuple[str, ...] = (
    "openings", "transactions", "corporate_actions", "dividends", "cash", "fx",
)

#: Worksheet keys in :attr:`Conversion.rows` that are NOT ready to upload — they carry blanks
#: only the owner can fill. Named here so the response can say so per file rather than
#: shipping them alongside the real ones and relying on a filename to warn anybody.
WORKSHEETS: tuple[str, ...] = ("_actions_worksheet", "_openings_worksheet")


class BrokerExportFile(BaseModel):
    name: str
    text: str


class BrokerConvertBody(BaseModel):
    account: str
    broker: str = "schwab"
    currency: str = "USD"
    exports: list[BrokerExportFile] = Field(default_factory=list)
    #: ``{CUSIP: TICKER}`` the owner supplied by hand, for rows the file itself does not
    #: resolve. Merged OVER the inferred ones — a human correction outranks an inference.
    aliases: dict[str, str] = Field(default_factory=dict)


def _issue_wire(i: ReconcileIssue) -> dict[str, Any]:
    return {"code": i.code, "severity": i.severity, "refs": list(i.refs), "detail": i.detail}


def _conversion_wire(conv: Conversion, report: ReconcileReport) -> dict[str, Any]:
    """The whole verdict, with the CSVs only when the batch is importable.

    Withholding the files on a blocking issue is the enforcement, not a presentation choice.
    Sending them with a flag would leave the refusal one ignored checkbox from being bypassed
    — and the refusal is the point of the reconciler.
    """
    ok = not report.blocking
    out: dict[str, Any] = {
        "ok": ok,
        "rows_in": report.rows_in,
        # A Decimal, as a string. The frontend never computes money (CLAUDE.md invariant).
        "cash_total": decimal_str(report.cash_total),
        "counts": {k: len(v) for k, v in conv.rows.items()},
        "blocking": [_issue_wire(i) for i in report.blocking],
        "advisory": [_issue_wire(i) for i in report.advisory],
        "aliases_inferred": dict(conv.aliases_inferred),
        "aliases_ambiguous": {k: sorted(v) for k, v in conv.aliases_ambiguous.items()},
        "unconvertible": [
            {"ref": e.ref, "date": e.trade_date.isoformat(), "kind": e.kind.value, "why": why}
            for e, why in conv.unconvertible
        ],
        # The two worksheets, as STRUCTURE rather than as CSV text, so the page can render an
        # input beside each blank instead of asking the owner to open a spreadsheet. What the
        # file cannot determine is exactly what the form must ask for.
        "actions_needing_input": [
            {
                "date": p.trade_date.isoformat(),
                "kind": p.kind.value,
                "from_symbol": p.from_symbol,
                "to_symbol": p.to_symbol,
                "ratio_to": p.ratio_to,
                "ratio_from": p.ratio_from,
                "needs": p.needs,
                "refs": list(p.refs),
            }
            for p in conv.actions_needing_input
        ],
        "openings_needing_cost": [
            {"symbol": s, "shares": decimal_str(sh) if sh > Decimal(0) else ""}
            for s, sh in sorted(conv.openings.items())
        ],
        "commit_order": [k for k in COMMIT_ORDER if conv.rows.get(k)],
        # The header line for each worksheet kind, so the page can render the rows it
        # collected back into a CSV **without keeping its own copy of the column list**.
        # A second copy of a column order in JS is the registration-point defect this repo
        # keeps meeting (``import_templates`` names seven of them): the two drift, and the
        # symptom is an import that rejects every row for a reason nobody can see.
        "worksheet_headers": {
            k: render_kind(k, []).rstrip("\r\n")
            for k in ("corporate_actions", "openings")
        },
    }
    if ok:
        out["files"] = {
            k: render_kind(k, conv.rows[k])
            for k in COMMIT_ORDER
            if conv.rows.get(k)
        }
    return out


@router.get("/broker/adapters")
def broker_adapters() -> dict[str, Any]:
    """Which brokers this build can convert. The page renders a picker from this, so adding
    an adapter to ``registry.py`` is the whole change — there is no second list."""
    return {"brokers": list(BROKER_IDS)}


@router.post("/broker/convert")
def broker_convert(
    body: BrokerConvertBody, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    if body.broker not in BROKER_IDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"不支援的券商：{body.broker}", field="broker"))
    if body.account not in {a.account_id for a in list_accounts(conn)}:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account} 不存在", field="account"))
    if not body.exports:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請至少選擇一個匯出檔", field="exports"))
    if sum(len(f.text) for f in body.exports) > _MAX_CHARS:
        return JSONResponse(status_code=413, content=error_body(
            "payload_too_large", "檔案過大，請分批轉換", field="exports"))

    events = []
    try:
        for f in body.exports:
            events += parse_export(
                body.broker, f.text, source_file=f.name, aliases=body.aliases)
    except UnmappedRow as exc:
        # Rule 7: an unmapped (action, description) pair STOPS the run with the pair quoted.
        # There is no catch-all bucket — a default is the same defect wearing a name — so
        # this is a refusal the owner can act on, not a crash. It is 422 and not 500 for
        # exactly that reason: the file is the input, and the input is answerable.
        return JSONResponse(status_code=422, content=error_body(
            "broker_row_unmapped", str(exc), field="exports"))

    conv, _grouped, report = convert(events, body.account, body.currency)
    return _conversion_wire(conv, report)
