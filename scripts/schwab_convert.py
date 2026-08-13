#!/usr/bin/env python
"""Convert a Charles Schwab transaction export into this app's own import templates.

Run it, read the report, fill in what it says it cannot know, then upload the CSVs through
匯入. Nothing here writes to a ledger and nothing here talks to the app — the conversion is
offline on purpose, so a bad run costs a directory of files and not a restore.

**All the logic lives in** ``portfolio_dash/data_ingestion/broker/`` — parsing, classification,
folding, and the reconciliation gate. This file is the command line around it: it reads files,
writes files, and prints. That split is deliberate. The package is covered by ``mypy --strict``
and the regression suite; a script is covered by whoever last ran it.

**Privacy is a structural property of this file**, in the same terms as
``verify_corporate_actions.py``:

* Neither path has a default, so the script cannot read a private location by accident.
* **stdout is pasteable.** It carries fixed labels, integer counts, issue *codes*, and symbols
  masked by :func:`scripts.privacy.safe_symbol`. No amount, no share count, no file name, and
  no issue *message* — a message interpolates the very quantities being protected.
* **stderr is NOT pasteable.** It interpolates file names, and a broker's export filename
  embeds a fragment of the account number. Paste the report, not the whole terminal.
* The **detailed** report — amounts, symbols, line references — is written to a file inside
  ``--out``, beside the converted CSVs. That directory already holds the owner's full ledger,
  so the detail leaks nothing new there, and keeping it out of stdout is what lets the summary
  be shared while the detail stays home.

**All or nothing.** If the reconciler raises a blocking issue, the CSVs are NOT written — only
the report is. A partial conversion is the thing that turns one bad row into an afternoon of
reconciling a ledger against a statement.

Two files are written as **worksheets**, with fields the export genuinely does not determine
left blank: ``openings_TO_COMPLETE.csv`` (a position older than the export window has no cost
in it) and ``corporate_actions_TO_COMPLETE.csv`` (a one-leg split states its delta, never its
ratio). Both are rejected by the importer until filled, which is the intended behaviour and
not an obstacle to work around — ``original_cost_total`` of 0 is refused by D37, and a
guessed ratio is refused by D14.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from portfolio_dash.data_ingestion.broker.grouping import (
    ActionPair,
    DividendEvent,
    GroupedImport,
    apply_aliases,
    group_events,
    infer_cusip_aliases,
    pair_actions,
    prehistory_shares,
)
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent, UnmappedRow
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileReport, reconcile
from portfolio_dash.data_ingestion.broker.registry import parse_export
from portfolio_dash.data_ingestion.import_templates import template_columns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.privacy import force_utf8_stdio, safe_symbol  # noqa: E402

_ZERO = Decimal(0)

#: Share counts are written at 8 dp. A derived reinvest count is an exact quotient carrying
#: 28 significant digits, which is false precision, not accuracy — the broker states an amount
#: and a price, and eight places is past any fractional share either can express. The value
#: still differs from the printed quantity, which is the entire point of deriving it.
_SHARE_PLACES = Decimal("0.00000001")

#: Broker event kinds that become a cash movement, and the ledger kind each becomes.
_CASH_KIND: dict[EventKind, str] = {
    EventKind.DEPOSIT: "DEPOSIT",
    EventKind.WITHDRAWAL: "WITHDRAW",
    EventKind.INTEREST_INCOME: "INTEREST",
    EventKind.INTEREST_EXPENSE: "INTEREST_EXPENSE",
    EventKind.FEE: "BROKER_FEE",
}

#: Ledger-bound kinds this converter cannot express, and why. Enumerated rather than defaulted:
#: a kind that falls off the end of a mapping is a row that leaves the ledger silently, which
#: is the failure this whole package exists to prevent. :func:`_account_for_output` asserts
#: that every routed event is either written or named here.
_UNCONVERTIBLE: dict[EventKind, str] = {
    EventKind.CASH_IN_LIEU: (
        "cash for a fractional share has no ledger row of its own — it is neither a trade nor "
        "a cash movement. Enter it manually as a small sale, or as a cash movement if the "
        "position it came from is already closed"
    ),
}

_BUY_SIDE = {EventKind.BUY, EventKind.BUY_COVER}
_SELL_SIDE = {EventKind.SELL, EventKind.SELL_SHORT}


@dataclass
class Conversion:
    """The converted rows, keyed by import kind, plus what could not be converted."""

    rows: dict[str, list[list[str]]]
    unconvertible: list[tuple[RawEvent, str]]
    aliases_inferred: dict[str, str]
    aliases_ambiguous: dict[str, set[str]]
    actions_needing_input: list[ActionPair]
    openings: dict[str, Decimal]
    #: Every source row this conversion accounted for — written into a CSV or named as
    #: unconvertible. Checked against the routed events by :func:`_account_for_output`.
    written_refs: set[str]


# --------------------------------------------------------------------------- formatting


def _money(value: Decimal) -> str:
    """A money string with no exponent and no trailing-zero drift."""
    return f"{value:f}"


def _shares(value: Decimal) -> str:
    quantized = value.quantize(_SHARE_PLACES, rounding=ROUND_HALF_UP).normalize()
    # ``normalize`` renders 100 as 1E+2; the ledger stores TEXT and reads it back as a number,
    # but a human opening the CSV should not have to.
    return f"{quantized:f}"


# --------------------------------------------------------------------------- conversion


#: Prefix stamped on the ``note`` of a row the reconciler saw twice across source files.
_DUPLICATE_MARK = "可能重複（兩份匯出的日期區間重疊）"


def _transaction_rows(grouped: GroupedImport, account: str, suspect: set[str]) -> list[
    list[str]
]:
    """Trades. Fees are the BROKER's own numbers, never recomputed.

    ``csv_import`` only auto-fills a fee or tax it was not given, so writing both columns keeps
    the ledger reconcilable against the statement to the cent. Recomputing them from the
    account's fee rules would produce a defensible number that does not match the money that
    actually left the account, which is the wrong kind of correct.
    """
    out: list[list[str]] = []
    for e in sorted(grouped.trades, key=lambda x: (x.trade_date, x.line_no)):
        side = "BUY" if e.kind in _BUY_SIDE else "SELL"
        out.append([
            account, e.symbol, side, e.trade_date.isoformat(),
            _shares(abs(e.quantity)), _money(e.price),
            _money(abs(e.fees)), "0",
            "0", "1" if e.kind is EventKind.SELL_SHORT else "0",
            f"{_DUPLICATE_MARK} {e.ref}" if e.ref in suspect else e.ref,
        ])
    return out


def _dividend_rows(dividends: list[DividendEvent], account: str) -> list[list[str]]:
    """One row per folded distribution. ``DRIP`` when it reinvested, ``CASH`` when it paid out.

    ``CASH`` on a ``drip_us`` account is ordinary, not exceptional (P1b): the same US position
    pays cash in some quarters and reinvests in others, and the model accepts both.
    """
    out: list[list[str]] = []
    for d in sorted(dividends, key=lambda x: (x.trade_date, x.symbol)):
        reinvested = d.reinvest_shares is not None and d.reinvest_price is not None
        out.append([
            account, d.symbol, d.trade_date.isoformat(),
            "DRIP" if reinvested else "CASH",
            _money(d.gross), _money(d.withholding), _money(d.net),
            _shares(d.reinvest_shares) if d.reinvest_shares is not None else "",
            _money(d.reinvest_price) if d.reinvest_price is not None else "",
        ])
    return out


def _cash_rows(
    grouped: GroupedImport, account: str, currency: str, suspect: set[str]
) -> tuple[list[list[str]], list[tuple[RawEvent, str]]]:
    """Cash movements. The ledger takes a MAGNITUDE plus a kind; the kind carries the sign.

    A row that appeared identically in two source files is written, and its note says so. It
    is not dropped — two deposits of the same amount on one day is a thing that happens, and
    the file cannot tell the two cases apart. It is not left silent either: the report is read
    once, whereas the ``note`` column is in front of whoever opens the CSV, which is where the
    decision actually gets made. (``import_batches`` catches a repeat of the whole FILE; it
    cannot see two copies of one row inside a single import.)
    """
    out: list[list[str]] = []
    unconvertible: list[tuple[RawEvent, str]] = []
    for e in sorted(grouped.cash, key=lambda x: (x.trade_date, x.line_no)):
        kind = _CASH_KIND.get(e.kind)
        if kind is None:
            unconvertible.append((e, _UNCONVERTIBLE.get(e.kind, "no mapping to a ledger row")))
            continue
        note = f"{_DUPLICATE_MARK} {e.ref}" if e.ref in suspect else e.ref
        out.append([
            account, e.trade_date.isoformat(), kind, currency,
            _money(abs(e.amount)), "", note,
        ])
    return out, unconvertible


def _action_rows(pairs: list[ActionPair], account: str) -> tuple[
    list[list[str]], list[list[str]], list[ActionPair]
]:
    """Split the paired actions into importable rows and a worksheet.

    A pair whose ratio the file does not determine goes to the worksheet with both ratio
    columns blank. They are REQUIRED columns, so the worksheet cannot be imported until the
    owner fills them — which is the point, not a snag: D14 refuses a decimal ratio because a
    rounded one replays into the 賣超 cascade this feature exists to prevent, and a ratio
    invented by the converter is the same defect with a friendlier face.
    """
    ready: list[list[str]] = []
    worksheet: list[list[str]] = []
    pending: list[ActionPair] = []
    for p in sorted(pairs, key=lambda x: (x.trade_date, x.to_symbol)):
        row = [
            account, p.trade_date.isoformat(), p.kind.value,
            p.from_symbol, p.to_symbol,
            "" if p.ratio_to is None else str(p.ratio_to),
            "" if p.ratio_from is None else str(p.ratio_from),
            "", " · ".join(p.refs),
        ]
        if p.ratio_to is None or p.ratio_from is None or not p.from_symbol:
            row[-1] = f"{p.needs} [{' · '.join(p.refs)}]"
            worksheet.append(row)
            pending.append(p)
        else:
            ready.append(row)
    return ready, worksheet, pending


def _opening_rows(
    openings: dict[str, Decimal], account: str, build_date: str
) -> list[list[str]]:
    """The pre-history worksheet: shares where they are known, cost always blank.

    ``original_cost_total`` is left empty rather than zeroed. D37 rejects a cost of 0 outright,
    so a zero would look like data and import as an error; a blank looks like a question.
    """
    return [
        [account, symbol, _shares(shares) if shares > _ZERO else "", "", build_date, ""]
        for symbol, shares in sorted(openings.items())
    ]


def _account_for_output(grouped: GroupedImport, conv: Conversion) -> list[str]:
    """Every routed event is written or named. Returns the complaints, empty when sound.

    ``reconcile`` proves nothing was lost between the FILE and the grouped events. This proves
    nothing was lost between the grouped events and the CSVs — a second seam with its own way
    of dropping a row: a kind missing from ``_CASH_KIND`` is simply not written, and the counts
    at the bottom of the report still look plausible. (It found three real ones on the first
    run — a broker withholds tax on credit INTEREST too, and those rows matched no cash kind.)

    By ``ref``, never by count, for the same reason :func:`grouping.account_for` is: counts
    balance while rows swap places. A count-based version of this function was written first
    and was quietly wrong — an unpaired corporate-action leg lands in ``unconvertible`` AND in
    ``grouped.actions``, so it was counted on both sides and cancelled its own complaint.
    """
    expected = {
        *(e.ref for e in grouped.trades),
        *(r for d in grouped.dividends for r in d.refs),
        *(e.ref for e in grouped.cash),
        *(e.ref for e in grouped.actions),
    }
    problems: list[str] = []
    missing = sorted(expected - conv.written_refs)
    if missing:
        problems.append(
            f"{len(missing)} routed rows reached no output and were not named as "
            f"unconvertible — first: {missing[:5]}"
        )
    invented = sorted(conv.written_refs - expected)
    if invented:
        problems.append(f"output cites rows that were not routed: {invented[:5]}")

    # The ref check proves ROUTING is complete; it is built from the source objects, so it
    # would still pass if a row builder filtered something out on the way to the CSV. These
    # two are the other half: both builders are straight 1:1 comprehensions, so a count that
    # disagrees means one of them grew a condition.
    for label, rows, source in (
        ("transactions", conv.rows["transactions"], len(grouped.trades)),
        ("dividends", conv.rows["dividends"], len(grouped.dividends)),
    ):
        if len(rows) != source:
            problems.append(
                f"{source} routed {label} produced {len(rows)} rows — the builder dropped some"
            )
    return problems


def convert(events: list[RawEvent], account: str, currency: str) -> tuple[
    Conversion, GroupedImport, ReconcileReport
]:
    """Parse-to-rows, with the reconciler run over the grouped events on the way through."""
    inferred, ambiguous = infer_cusip_aliases(events)
    events = apply_aliases(events, inferred)
    grouped = group_events(events)
    report = reconcile(events, grouped)

    pairs, unpaired = pair_actions(events, grouped.actions)
    ready, worksheet, pending = _action_rows(pairs, account)
    suspect = {ref for i in report.advisory if i.code == "overlap_duplicate" for ref in i.refs}
    cash, unconvertible = _cash_rows(grouped, account, currency, suspect)
    unconvertible += [(e, "corporate-action leg that could not be paired") for e in unpaired]

    openings = prehistory_shares(events)
    earliest = min((e.trade_date for e in events), default=None)
    # The DAY BEFORE the first event, never the same day: an opening position exists before
    # the ledger starts, and dating it onto a day that also carries transactions makes the
    # replay's same-date ordering decide whether the opening covers a sell or arrives after it.
    build = (earliest - timedelta(days=1)).isoformat() if earliest is not None else ""

    conv = Conversion(
        rows={
            "transactions": _transaction_rows(grouped, account, suspect),
            "dividends": _dividend_rows(grouped.dividends, account),
            "cash": cash,
            "fx": [],
            "corporate_actions": ready,
            "_actions_worksheet": worksheet,
            "_openings_worksheet": _opening_rows(openings, account, build),
        },
        unconvertible=unconvertible,
        aliases_inferred=inferred,
        aliases_ambiguous=ambiguous,
        actions_needing_input=pending,
        openings=openings,
        # Taken from the SOURCE objects, not parsed back out of the note column: a check that
        # reads the artifact it is verifying agrees with itself (the same mistake the
        # reconciler's share check made and had to have removed).
        written_refs={
            *(e.ref for e in grouped.trades),
            *(r for d in grouped.dividends for r in d.refs),
            *(e.ref for e in grouped.cash if e.kind in _CASH_KIND),
            *(r for p in pairs for r in p.refs),
            *(e.ref for e, _ in unconvertible),
        },
    )
    return conv, grouped, report


# --------------------------------------------------------------------------- output


_FILENAMES: dict[str, str] = {
    "transactions": "import_transactions.csv",
    "dividends": "import_dividends.csv",
    "cash": "import_cash.csv",
    "fx": "import_fx.csv",
    "corporate_actions": "import_corporate_actions.csv",
    "_actions_worksheet": "corporate_actions_TO_COMPLETE.csv",
    "_openings_worksheet": "openings_TO_COMPLETE.csv",
}

_TEMPLATE_KIND: dict[str, str] = {
    "_actions_worksheet": "corporate_actions",
    "_openings_worksheet": "openings",
}


def _render(kind: str, rows: list[list[str]]) -> str:
    """One import CSV: the template's own plain header, then the rows. CRLF, no BOM.

    The header is the PLAIN column list, not the annotated one the download endpoint renders —
    the import seam canonicalizes annotations away, but a file a human may hand-edit should not
    carry ``（選填）`` in its header.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(template_columns(_TEMPLATE_KIND.get(kind, kind)))
    writer.writerows(rows)
    return buf.getvalue()


def _write_csvs(out_dir: Path, conv: Conversion) -> list[tuple[str, int]]:
    written: list[tuple[str, int]] = []
    for kind, rows in conv.rows.items():
        path = out_dir / _FILENAMES[kind]
        path.write_text(_render(kind, rows), encoding="utf-8", newline="")
        written.append((_FILENAMES[kind], len(rows)))
    return written


def _write_report(
    out_dir: Path, conv: Conversion, grouped: GroupedImport, report: ReconcileReport,
    sources: list[Path], wrote_csvs: bool,
) -> Path:
    """The DETAILED report — amounts, symbols and line references, unmasked.

    Written into ``--out``, which already holds the converted ledger, and deliberately not to
    stdout. Splitting the two is what lets the summary be pasted into a chat while the detail
    stays on the owner's disk.
    """
    lines: list[str] = [
        "Schwab conversion report",
        "=" * 72,
        f"sources: {', '.join(p.name for p in sources)}",
        f"rows read: {report.rows_in}",
        f"net cash of the rows to be written: {report.cash_total}",
        "",
        "CSVs written: " + ("yes" if wrote_csvs else "NO — blocked, see below"),
        "",
    ]

    if conv.aliases_inferred:
        lines += ["CUSIP aliases inferred from the file itself:"]
        lines += [f"  {c} -> {t}" for c, t in sorted(conv.aliases_inferred.items())]
        lines += [""]
    if conv.aliases_ambiguous:
        lines += ["CUSIPs naming more than one ticker — NOT applied:"]
        lines += [f"  {c} -> {sorted(t)}" for c, t in sorted(conv.aliases_ambiguous.items())]
        lines += [""]

    lines += ["BLOCKING — nothing is written until these are resolved:"] if report.blocking \
        else ["BLOCKING: none"]
    lines += [f"  [{i.code}] {i.detail}" + (f"  {list(i.refs)}" if i.refs else "")
              for i in report.blocking]
    lines += [""]

    lines += ["ADVISORY — rows that will NOT be imported, or facts to act on:"]
    lines += [f"  [{i.code}] {i.detail}" + (f"  {list(i.refs)}" if i.refs else "")
              for i in report.advisory] or ["  none"]
    lines += [""]

    if conv.actions_needing_input:
        lines += ["Corporate actions the file does not fully determine:"]
        lines += [
            f"  {p.trade_date} {p.kind.value} {p.from_symbol or '?'} -> {p.to_symbol}: {p.needs}"
            for p in conv.actions_needing_input
        ]
        lines += [""]

    if conv.openings:
        lines += ["Positions older than the export window (fill in the cost):"]
        lines += [
            f"  {symbol}: {shares if shares > _ZERO else 'quantity unknown'}"
            for symbol, shares in sorted(conv.openings.items())
        ]
        lines += [""]

    if grouped.folded:
        lines += ["Rows absorbed into another event (their money IS in the ledger):"]
        lines += [f"  {e.ref} {e.trade_date} {e.kind.value} {e.amount}" for e in grouped.folded]
        lines += [""]

    if conv.unconvertible:
        lines += ["Rows with no ledger representation — enter these by hand:"]
        lines += [f"  {e.ref} {e.trade_date} {e.kind.value}: {why}"
                  for e, why in conv.unconvertible]
        lines += [""]

    lines += ["Suppressed (self-cancelling) groups:"]
    lines += [f"  {g.key[0]} {g.key[1] or '(no symbol)'}: {list(g.refs)}"
              for g in grouped.suppressed] or ["  none"]
    lines += [""]

    path = out_dir / "conversion_report.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _print_summary(
    conv: Conversion, grouped: GroupedImport, report: ReconcileReport, wrote_csvs: bool
) -> None:
    """stdout, and it must stay pasteable — labels, integers, codes, masked symbols only."""
    print(f"讀入列數 {report.rows_in}")
    print(
        "產生 "
        f"交易 {len(conv.rows['transactions'])} · "
        f"股利 {len(conv.rows['dividends'])} · "
        f"現金 {len(conv.rows['cash'])} · "
        f"公司行動 {len(conv.rows['corporate_actions'])} · "
        f"換匯 {len(conv.rows['fx'])}"
    )
    print(
        f"待補 公司行動 {len(conv.rows['_actions_worksheet'])} · "
        f"期初庫存 {len(conv.rows['_openings_worksheet'])}"
    )
    print(
        f"抑制群組 {len(grouped.suppressed)}"
        f"（{sum(len(g.refs) for g in grouped.suppressed)} 列）"
    )
    print(f"選擇權（辨識但不支援） {len(grouped.options)}")
    print(f"無法轉換 {len(conv.unconvertible)}")
    print(f"CUSIP 別名（由檔案自行推得） {len(conv.aliases_inferred)}")

    for label, issues in (("阻擋", report.blocking), ("提示", report.advisory)):
        counts = Counter(i.code for i in issues)
        print(f"問題（{label}） {sum(counts.values())}")
        for code, n in sorted(counts.items()):
            print(f"  {code} {n}")

    if conv.actions_needing_input:
        print("需人工補齊的公司行動")
        for p in conv.actions_needing_input:
            print(f"  {p.trade_date} {p.kind.value} {safe_symbol(p.to_symbol)}")

    print("PASS — 已產生匯入檔" if wrote_csvs else f"FAIL — {len(report.blocking)} 項阻擋")


# --------------------------------------------------------------------------- cli


def _problem(message: str) -> None:
    """A run-blocking diagnostic, on stderr so it never lands in the pasteable report.

    Callers pass file NAMES and exception TYPES. Never an issue message: those interpolate
    quantities, which is exactly what must not be printed.
    """
    print(message, file=sys.stderr)


def _sources(export: Path) -> list[Path]:
    if export.is_dir():
        return sorted(p for p in export.glob("*.csv") if p.is_file())
    return [export]


def _parse_aliases(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        identifier, _, ticker = item.partition("=")
        if not identifier.strip() or not ticker.strip():
            raise ValueError(f"--alias expects CUSIP=TICKER, got {item!r}")
        out[identifier.strip()] = ticker.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schwab_convert.py",
        description="Convert a Schwab transaction export into this app's import CSVs.",
        epilog=(
            "stdout carries only labels, counts and issue codes, so it is safe to paste. "
            "stderr and the report file are NOT — they name files and amounts."
        ),
    )
    # No defaults, by design (D27): a defaulted path is a path that can be read by accident,
    # and the data this runs against is git-ignored real financial history.
    parser.add_argument("--export", required=True, metavar="<path>",
                        help="Schwab CSV, or a directory of them (no default)")
    parser.add_argument("--out", required=True, metavar="<dir>",
                        help="directory for the converted CSVs and the report (no default)")
    parser.add_argument("--account", required=True, metavar="<account_id>",
                        help="the ledger account these rows belong to")
    parser.add_argument("--currency", default="USD", metavar="<ccy>",
                        help="settlement currency of the cash rows (default USD)")
    parser.add_argument("--alias", action="append", default=[], metavar="CUSIP=TICKER",
                        help="map an identifier the file never names; repeatable")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)

    export = Path(args.export)
    out_dir = Path(args.out)
    if not export.exists():
        _problem(f"--export: 找不到這個路徑（{export.name}）")
        return 2
    try:
        owner_aliases = _parse_aliases(args.alias)
    except ValueError as exc:
        _problem(str(exc))
        return 2

    sources = _sources(export)
    if not sources:
        _problem(f"--export: 目錄裡沒有 CSV（{export.name}）")
        return 2

    events: list[RawEvent] = []
    for path in sources:
        try:
            text = path.open(encoding="utf-8-sig", newline="").read()
            events.extend(parse_export("schwab", text, source_file=path.name,
                                       aliases=owner_aliases))
        except UnmappedRow as exc:
            # Rule 7. The action and description are the BROKER's vocabulary, not the owner's
            # data, so naming them is what makes the error fixable — but the file name is the
            # owner's, which is why this goes to stderr.
            _problem(str(exc))
            return 2
        except (OSError, ValueError) as exc:
            _problem(f"{path.name}: {type(exc).__name__}: {exc}")
            return 2

    if owner_aliases:
        events = apply_aliases(events, owner_aliases)

    conv, grouped, report = convert(events, args.account, args.currency)

    leaks = _account_for_output(grouped, conv)
    for message in leaks:
        _problem(f"internal: {message}")

    wrote = report.ok and not leaks
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if wrote:
            _write_csvs(out_dir, conv)
        _write_report(out_dir, conv, grouped, report, sources, wrote)
    except OSError as exc:
        _problem(f"--out: {type(exc).__name__}: {exc}")
        return 2

    _print_summary(conv, grouped, report, wrote)
    return 0 if wrote else 1


if __name__ == "__main__":
    raise SystemExit(main())
