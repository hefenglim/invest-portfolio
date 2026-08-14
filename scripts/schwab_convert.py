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
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from portfolio_dash.data_ingestion.broker.convert import (
    FILENAMES,
    Conversion,
    account_for_output,
    convert,
    render_kind,
)
from portfolio_dash.data_ingestion.broker.grouping import GroupedImport, apply_aliases
from portfolio_dash.data_ingestion.broker.ir import RawEvent, UnmappedRow
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileReport
from portfolio_dash.data_ingestion.broker.registry import parse_export

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.privacy import force_utf8_stdio, safe_symbol  # noqa: E402

_ZERO = Decimal(0)


# --------------------------------------------------------------------------- output



def _write_csvs(out_dir: Path, conv: Conversion) -> list[tuple[str, int]]:
    written: list[tuple[str, int]] = []
    for kind, rows in conv.rows.items():
        path = out_dir / FILENAMES[kind]
        path.write_text(render_kind(kind, rows), encoding="utf-8", newline="")
        written.append((FILENAMES[kind], len(rows)))
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

    leaks = account_for_output(grouped, conv)
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
