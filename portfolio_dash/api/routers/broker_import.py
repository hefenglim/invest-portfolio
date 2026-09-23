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
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.broker.convert import (
    Conversion,
    DroppedRow,
    RowDetail,
    convert,
    render_kind,
)
from portfolio_dash.data_ingestion.broker.ir import RawEvent, UnmappedRow
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileIssue, ReconcileReport
from portfolio_dash.data_ingestion.broker.registry import (
    BROKER_IDS,
    parse_export,
    serves_account,
)
from portfolio_dash.data_ingestion.holdings import load_action_index, shares_through
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.data_ingestion.validate import unknown_account_message
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.models.assets import Account
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


def _row_wire(i: int, d: RowDetail) -> dict[str, Any]:
    """One converted row for the page's table (DEF-028): every figure is the CSV's own
    string, and ``cells`` is the exact list ``render_kind`` writes, so the page can commit
    a ticked subset without parsing CSV text back into fields."""
    return {
        "i": i, "refs": list(d.refs), "date": d.date, "type": d.type, "symbol": d.symbol,
        "shares": d.shares, "price": d.price, "amount": d.amount, "currency": d.currency,
        "note": d.note, "cells": list(d.cells),
    }


def _dropped_wire(d: DroppedRow) -> dict[str, Any]:
    return {
        "refs": list(d.refs), "why": d.why, "date": d.date, "symbol": d.symbol,
        "kinds": list(d.kinds), "into": d.into, "detail": d.detail,
    }


def _opening_gaps(
    conn: sqlite3.Connection, account: str, openings: dict[str, Decimal], build: date | None
) -> list[dict[str, Any]]:
    """The pre-history positions, measured against what the LEDGER already holds (DEF-027).

    ``prehistory_shares`` looks only inside the file: 「this symbol was sold 1,000 shares
    the file never bought」. The ledger may already hold some of them — the account had
    85.04 AAPL from earlier entries when a statement selling 1,000 arrived, and the file-only
    hint asked for a 1,000-share opening on top. The gap is what the file needs MINUS what
    the ledger holds at the close of the day before the statement starts (the opening's own
    build date), through ``holdings.shares_through`` — the same date-aware count the sell
    guard uses, so the hint and the guard agree.

    A blank ``build`` (no events) reads the ledger's net position, which is the only
    defined answer without a window. Money-free: shares only, as Decimal strings.
    """
    index = load_action_index(conn)
    out: list[dict[str, Any]] = []
    for symbol, needed in sorted(openings.items()):
        held = (
            shares_through(conn, account, symbol, on=build, index=index)
            if build is not None
            else Decimal(0)
        )
        if held < Decimal(0):
            held = Decimal(0)
        unknown = needed <= Decimal(0)
        gap = Decimal(0) if unknown else max(needed - held, Decimal(0))
        out.append({
            "symbol": symbol,
            # The file-only figure, as before (blank = held before the window but the
            # count cannot be read off the file).
            "shares": decimal_str(needed) if not unknown else "",
            "ledger_shares": decimal_str(held),
            "gap": decimal_str(gap) if not unknown else "",
            # Satisfied: the ledger already covers the sells (or, for an unknown count,
            # already holds the symbol) — no opening row is needed, and the page must not
            # offer to add one on top.
            "satisfied": (held >= needed) if not unknown else held > Decimal(0),
            "as_of": build.isoformat() if build is not None else "",
        })
    return out


def _conversion_wire(
    conv: Conversion,
    report: ReconcileReport,
    *,
    openings: list[dict[str, Any]],
    build: date | None,
) -> dict[str, Any]:
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
        "openings_needing_cost": openings,
        # The day BEFORE the earliest statement row: the opening rows' build date, and the
        # day the ledger holdings above were measured at. Sent so the page stops deriving
        # it by parsing the transactions CSV (F-03: never re-parse server text).
        "openings_build_date": build.isoformat() if build is not None else "",
        # DEF-028: the rows, one by one, in the order they will be committed (and, within a
        # day, the order they happened — see ``ir.chrono_key``), plus every source row
        # that reaches no row of its own and where it went. ⚠ The rows are sent even on a
        # blocking verdict, exactly like ``counts``: the owner needs to see what WOULD have
        # been written. What is withheld is ``files`` — and ``cells`` are the same data, so
        # the page must (and does) refuse to commit when ``ok`` is false.
        "rows": {
            k: [_row_wire(i, d) for i, d in enumerate(conv.details.get(k, []))]
            for k in COMMIT_ORDER if k in conv.details
        },
        "dropped": [_dropped_wire(d) for d in conv.dropped],
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
def broker_adapters(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Which brokers this build can convert, and which ACCOUNTS each one's statements
    belong to. The page renders both pickers from this, so adding an adapter to
    ``registry.py`` (plus its ``ACCOUNT_BROKERS`` line) is the whole change — there is no
    second list, and the page never holds its own broker→account table (DEF-029)."""
    accounts = list_accounts(conn)
    return {
        "brokers": list(BROKER_IDS),
        "accounts_by_broker": {
            b: [a.account_id for a in accounts if serves_account(b, a.broker)]
            for b in BROKER_IDS
        },
    }


def _broker_mismatch(broker: str, account: Account) -> dict[str, Any] | None:
    """The DEF-029 refusal: a *broker* statement converted into an account of another
    broker. A BLOCKING reconcile issue in the ordinary verdict shape (``ok: false``, no
    files), not a 400: the page already renders blocking issues as the all-or-nothing
    refusal, and this is one — nothing about the file is wrong, the pairing is.

    The account is named by its MARKER (``shared.account_ref``), never by the English
    ``accounts.name``; the adapter is named by its id, which the page resolves through
    ``pdNames.broker``. The message therefore reads in one language on the screen and
    stays greppable in the log.
    """
    if serves_account(broker, account.broker):
        return None
    issue = ReconcileIssue(
        code="account_broker_mismatch", severity="blocking", refs=(),
        detail=(
            f"帳戶 {account_ref(account.account_id)} 不屬於券商 {broker}"
            "（對帳單格式），一列都不會轉換"
        ),
    )
    return {
        "ok": False,
        "rows_in": 0,
        "cash_total": "0",
        "counts": {},
        # ``account_id`` / ``broker_id`` (never ``account``): the frontend guard
        # ``test_account_name_single_source`` reads any ``x.account`` as a raw-name leak.
        "blocking": [
            {**_issue_wire(issue), "account_id": account.account_id, "broker_id": broker}
        ],
        "advisory": [],
        "aliases_inferred": {},
        "aliases_ambiguous": {},
        "unconvertible": [],
        "actions_needing_input": [],
        "openings_needing_cost": [],
        "openings_build_date": "",
        "rows": {},
        "dropped": [],
        "commit_order": [],
        "worksheet_headers": {
            k: render_kind(k, []).rstrip("\r\n") for k in ("corporate_actions", "openings")
        },
    }


@router.post("/broker/convert")
def broker_convert(
    body: BrokerConvertBody, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    if body.broker not in BROKER_IDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"不支援的券商：{body.broker}", field="broker"))
    accounts = {a.account_id: a for a in list_accounts(conn)}
    if body.account not in accounts:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", unknown_account_message(body.account), field="account"))
    if not body.exports:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請至少選擇一個匯出檔", field="exports"))
    if sum(len(f.text) for f in body.exports) > _MAX_CHARS:
        return JSONResponse(status_code=413, content=error_body(
            "payload_too_large", "檔案過大，請分批轉換", field="exports"))
    # DEF-029: BEFORE the parse. An account of another broker gets the refusal without a
    # single row being converted — the verdict must not read 「對帳通過」 on its way to it.
    if (mismatch := _broker_mismatch(body.broker, accounts[body.account])) is not None:
        return mismatch

    events: list[RawEvent] = []
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
    # The opening rows' build date — the day before the first statement row — is ALSO the
    # day the ledger's existing holdings are measured at (DEF-027): what the file needs
    # minus what the account already held when the statement starts.
    earliest = min((e.trade_date for e in events), default=None)
    build = earliest - timedelta(days=1) if earliest is not None else None
    return _conversion_wire(
        conv, report,
        openings=_opening_gaps(conn, body.account, conv.openings, build),
        build=build,
    )
