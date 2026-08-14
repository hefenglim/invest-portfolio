"""§10.5's blocking acceptance run for the corporate-actions feature (D27, 2026-08-09).

    .venv/Scripts/python scripts/verify_corporate_actions.py --export <path> --actions <path>

The feature was commissioned for ONE outcome: the owner's real broker export replays
without tripping the 賣超 guard on the affected tickers, with cost basis intact. The data
is git-ignored, so this can never be a CI gate — which is exactly why it must not be left
as a paragraph someone remembers to honour. The script is the deliverable; the data and the
output are not committed.

**Privacy is a structural property of this file, not a convention.**

* Neither path has a default. A missing argument exits with usage; nothing is ever guessed,
  so the script cannot accidentally read a private location.
* Nothing is written to disk. The ledger is replayed in an in-memory SQLite database that
  ceases to exist when the process does.
* Exactly one function — :func:`_report_line` — prints per-ticker output, and it accepts a
  symbol and three booleans. There is no code path from a Decimal to stdout. Every other
  printed token is a fixed label, an integer count, or a machine issue *kind* (never an
  issue *message*: those interpolate quantities).
* **The type argument above is necessary and was not sufficient** (found 2026-08-11). A
  *symbol* is a free string, and the owner's broker fills it with an amount: an option
  contract is ``TICKER MM/DD/YYYY STRIKE C|P``, so ``TSLA 01/19/2024 200.00 P`` puts a strike
  on stdout with no ``Decimal`` involved. Symbols are therefore masked by
  :func:`_safe_symbol` before printing — see its docstring for why masking rather than
  rejecting, and why the test is not "contains a digit" (TW and MY tickers are numeric).

  With that, stdout is pasteable into a chat session without leaking amounts, share counts or
  account names. **stderr is not**: :func:`_problem` interpolates input FILE NAMES, and a
  broker's own export filename embeds an account-number fragment. Paste the report, not the
  whole terminal.

**What it checks, per affected ticker** (§10.5's three columns):

* **賣超 tripped?** — did the replay flag an UNDECLARED oversell for this symbol? 賣超 is
  STICKY (raised on "was ever negative", never cleared by a later buy), and a DECLARED short
  sale is not an oversell. See :func:`_ever_negative` for the half of this that
  ``Holding.oversold`` structurally cannot see.
* **original_total intact?** — the oversell path DISCARDS the cost basis, so this is the
  "did we lose the basis" check. It is not a restatement of the first column: F-13's
  zero-cost opening zeroes a basis with **no** oversell, which is the failure this column
  exists to catch (§10.5's "strictly worse than the oversell it appears to fix").
* **shares reconcile?** — §7.2's parity property applied to the owner's real ledger: the two
  independent share paths (``data_ingestion/holdings.py``'s walk and ``build_book``'s
  replay) agree. Both existing functions are reused; there is deliberately no third path.

**Exit code** 0 = PASS, 1 = FAIL, 2 = could not run (bad arguments, unreadable input, a ledger
row that would not load). So it chains. PASS requires **both** counts to be zero — no failing
ticker AND no missing corporate-action row — because a run whose ledger is still short a row
has not tested the thing being accepted, and calling that PASS is trap #22 arriving through
the gate written to prevent it. The two counts are printed separately rather than summed; see
the note at the verdict line for why.

**Input formats.** ``--export`` is the project's own canonical ledger CSVs — one file, or a
directory of them — dispatched by header to the same parsers the app's import route uses
(``data_ingestion/import_templates.py`` is the single source for the column sets, so a
parser rename cannot silently desynchronise this script). ``--actions`` is a corporate-action
CSV — one of the app's own import kinds (``corporate_actions``), read from the same header
constant, so one owner-authored file feeds both the app's import door and this run. Both
arguments go through ``normalize_import_csv``, so annotated template headers and non-ISO date
columns behave exactly as they do in the app.
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from portfolio_dash.api.routers.cash import cash_pool_fn
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.cash_import import (
    build_cash_movement_preview,
    write_cash_movement_row,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    canonical_header,
    normalize_import_csv,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.dividend_import import (
    build_dividend_preview,
    write_dividend_row,
)
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.holdings import (
    current_shares,
    load_action_index,
    shares_through,
)
from portfolio_dash.data_ingestion.import_templates import (
    DATE_COLUMN_BY_KIND,
    OPTIONAL_COLUMNS,
    template_columns,
)
from portfolio_dash.data_ingestion.markets import CCY_MARKET
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, Writer, commit_preview
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_corporate_action,
    list_corporate_actions,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    alias_import_account,
    validate_corporate_action,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

# The repo root, so ``scripts.privacy`` resolves when this file is run directly. An editable
# install puts ``portfolio_dash`` on the path but not ``scripts``, and the owner runs this as
# ``python scripts/verify_corporate_actions.py`` — which puts ``scripts/`` on the path, not
# its parent. Same guard as ``verify_live.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.privacy import force_utf8_stdio, safe_symbol  # noqa: E402

_ZERO = Decimal("0")
_BOM = "﻿"   # a leading UTF-8 BOM (Excel), tolerated exactly as the app's parsers do

# Ledger kinds this script can load, in the order they must be WRITTEN. Openings come first
# so a transaction's entry-time guards see the position they are covered by, and corporate
# actions come last (they are written separately, below) because E1a rejects an action whose
# source position does not exist on its date.
#
# **`cash` joined on 2026-08-12 with the 6th import kind, and it earns its place by NOT being
# checked.** None of §10.5's three columns reads a cash movement: they are per-ticker (賣超,
# `original_total`, share parity) and a movement carries no symbol. It is loaded anyway because
# the alternative is worse — `--export` accepts "the project's own canonical ledger CSVs", so an
# owner handing over their whole export directory would otherwise hit
# 「無法辨識這個 CSV 屬於哪一種帳本」 and the run would abort at exit 2 **on a file irrelevant to
# what it verifies**. A gate that refuses to start because of an unrelated file is a gate that
# gets skipped. It is written FIRST because its own withdraw guard is date-ordered and a
# transaction settlement may draw on a deposit.
_LOAD_ORDER: tuple[str, ...] = ("cash", "openings", "transactions", "dividends", "fx")

def _cash_builder(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """The cash parser needs its pool arithmetic INJECTED (architecture.md — `data_ingestion`
    may not import `portfolio`), so it does not fit the two-argument builder signature the
    other kinds share. Bound here, once per file, exactly as `api/routers/input_center.py`
    binds it — the probe is a required argument, so this wrapper cannot silently go missing."""
    return build_cash_movement_preview(conn, csv_text, pool=cash_pool_fn(conn))


_BUILDERS: dict[str, Callable[[sqlite3.Connection, str], ImportPreview]] = {
    "cash": _cash_builder,
    "openings": build_opening_preview,
    "transactions": build_transaction_preview,
    "dividends": build_dividend_preview,
    "fx": build_fx_preview,
}
_WRITERS: dict[str, Writer] = {
    "cash": write_cash_movement_row,
    "openings": write_opening_row,
    "transactions": write_transaction_row,
    "dividends": write_dividend_row,
    "fx": write_fx_row,
}

# The corporate-action CSV. W7 registered it as the app's 5th import kind, so its header and
# its optional cells come from ``import_templates`` like the other four — nothing about the
# ``--actions`` format is restated here. The owner writes ONE file that both the app's import
# door and this script read; a second spelling of the header would diverge silently at exactly
# the moment §10.5 is being run.
_ACTIONS_KIND = "corporate_actions"

# ⚠ Only the FORMAT is shared. This script deliberately does NOT use
# ``build_corporate_action_preview`` / ``write_corporate_action_row``, so it keeps its own
# parse/write and does not accept the zh kind labels (分割 / 換股 / 分拆) the importer maps —
# a zh label fails LOUDLY here (`unknown_action_kind` → rejected → counted as missing),
# never silently. Switching wholesale is a follow-up, and it is now purely a deletion: the
# reason it was deferred (the importer hoisted a single WHOLE-LEDGER book, so on a bulk
# import E3 hard-rejected the very actions that resolve the 賣超) was fixed in
# `validate_corporate_action` on 2026-08-11 — it date-scopes its own replay now, so this
# script simply calls it with no book at all.


# ---------------------------------------------------------------------------
# Output — the ONLY functions in this file that write to stdout
# ---------------------------------------------------------------------------


# Shared with ``schwab_convert.py`` (``scripts/privacy.py``): same owner, same Windows
# console, same zh-TW labels. The private alias keeps this module's own vocabulary.
_force_utf8_output = force_utf8_stdio


def _yn(value: bool) -> str:
    return "yes" if value else "no"


# The masking rule now lives in ``scripts/privacy.py`` — `schwab_convert.py` prints the same
# kind of report against the same kind of file, and a privacy control with two copies is one
# copy short of a leak. The private alias is kept because it is this module's own vocabulary
# and its tests name it; the implementation is shared, not duplicated.
#
# ⚠ ``main`` still builds ``affected`` from the **parsed** action inputs before any validation
# runs, so a rejected row's symbol reaches stdout too. That is why masking, not rejection, is
# the right control here: it changes what is printed and nothing else.
_safe_symbol = safe_symbol


def _report_line(symbol: str, oversold: bool, basis_intact: bool, reconciled: bool,
                 width: int) -> None:
    """Print §10.5's one line per affected ticker: a symbol and three yes/no answers.

    The signature is the privacy guarantee. This function cannot print a quantity because it
    is never given one — no ``Decimal``, no ``Holding``, no account id reaches it. Any future
    "just add the share count so I can see what went wrong" edit has to change this signature
    first, which is the point at which somebody has to justify it.

    The signature is **necessary and not sufficient**: *symbol* is a string, and a string can
    carry an amount. It is masked by :func:`_safe_symbol` before it reaches this line.
    """
    print(f"{_safe_symbol(symbol):<{width}} · 賣超 {_yn(oversold):<3}"
          f" · 成本完整 {_yn(basis_intact):<3} · 股數一致 {_yn(reconciled):<3}")


def _problem(message: str) -> None:
    """A run-blocking diagnostic, on stderr so it never lands in the pasteable report.

    Callers pass row NUMBERS and issue KINDS (machine codes such as ``market_mismatch``),
    never issue MESSAGES — a validation message interpolates the offending quantity, which
    is precisely what must not be printed.
    """
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Loading — the scratch ledger
# ---------------------------------------------------------------------------


def _required_columns(kind: str) -> frozenset[str]:
    """The columns a *kind*'s parser requires, derived from the app's own constants."""
    return frozenset(template_columns(kind)) - OPTIONAL_COLUMNS[kind]


def _header_of(text: str) -> list[str]:
    reader = csv.reader(io.StringIO(text.lstrip(_BOM)))
    return next(reader, [])


def _detect_kind(header: Sequence[str]) -> str | None:
    """Which ledger a CSV is, from its header alone — or None when it matches none.

    Keyed on the REQUIRED column set of each parser, so the signatures are disjoint by
    construction (only openings carry ``build_date``, only dividends ``type`` + ``gross``,
    only fx ``from_ccy``, only cash ``ccy`` + ``amount``). A guard test feeds each
    downloadable template back through this function, so a column rename in
    ``data_ingestion`` cannot leave the dispatcher behind.

    The **corporate-action** template is deliberately NOT one of them: it is the ``--actions``
    argument, and silently loading it from ``--export`` would apply the ratios without ever
    validating them. :func:`_is_action_header` recognises it so the run can say which argument
    it belongs to instead of reporting an unrecognisable file.
    """
    cols = {canonical_header(h) for h in header}
    for kind in _LOAD_ORDER:
        if _required_columns(kind) <= cols:
            return kind
    return None


def _is_action_header(header: Sequence[str]) -> bool:
    """True when a CSV in ``--export`` is really the corporate-action file."""
    cols = {canonical_header(h) for h in header}
    return _required_columns(_ACTIONS_KIND) <= cols


def _csv_files(export: Path) -> list[Path]:
    """The CSVs named by ``--export``: the file itself, or every ``*.csv`` in the directory."""
    if export.is_dir():
        return sorted(p for p in export.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    return [export]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _account_symbol_pairs(text: str) -> set[tuple[str, str]]:
    """Every (account, symbol) a CSV names, whichever ledger it is.

    Read from the RAW rows rather than from a parsed preview, because registration has to
    happen BEFORE the parsers run: an unregistered symbol is a HARD issue in the transaction
    importer, so an unregistered ledger would load as zero rows and the whole run would be a
    confident report about an empty book.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip(_BOM)))
    pairs: set[tuple[str, str]] = set()
    for raw in reader:
        row = {canonical_header(k): (v or "").strip() for k, v in raw.items() if k is not None}
        account = row.get("account", "")
        if not account:
            continue
        resolved, _ = alias_import_account(account)
        for key in ("symbol", "from_symbol", "to_symbol"):
            symbol = row.get(key, "")
            if symbol:
                pairs.add((resolved, symbol))
    return pairs


def _register_missing(conn: sqlite3.Connection, pairs: set[tuple[str, str]]) -> None:
    """Register any symbol the scratch ledger does not already know.

    Market and quote currency come from the OWNING ACCOUNT's settlement currency — a defined
    mapping (``CCY_MARKET``), not a guess about the security. It is also inert with respect
    to everything this script measures: all three checks are share counts and a per-symbol
    cost total in the instrument's own quote currency, none of which change with the label.
    An already-registered symbol is never overwritten.
    """
    for account_id, symbol in sorted(pairs):
        if get_instrument(conn, symbol) is not None:
            continue
        row = conn.execute(
            "SELECT settlement_ccy FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            continue  # unknown account — the row's own hard issue reports it, loudly
        try:
            ccy = Currency(row["settlement_ccy"])
        except ValueError:
            continue
        market = CCY_MARKET.get(ccy.value)
        if market is None:
            continue
        upsert_instrument(
            conn,
            Instrument(symbol=symbol, market=market, quote_ccy=ccy, sector="", name=symbol),
        )


def _load_export(conn: sqlite3.Connection, files: Sequence[Path]) -> list[str]:
    """Import every ledger CSV into the scratch database; return blocking problems.

    A non-empty return aborts the run. That is deliberate: a partially-loaded ledger produces
    a report that looks authoritative and answers a question nobody asked. Rows are accepted
    regardless of SOFT issues (an un-actioned export is full of ``sell_exceeds_holdings``
    warnings — that is the very condition being verified); a HARD issue means the row could
    not be written at all, and is reported by row number and issue kind.
    """
    problems: list[str] = []
    typed: list[tuple[str, Path, str]] = []
    for path in files:
        text = _read_text(path)
        header = _header_of(text)
        kind = _detect_kind(header)
        if kind is None:
            problems.append(
                f"{path.name}: 這是公司行動檔，請用 --actions 傳入，不要放進 --export"
                if _is_action_header(header)
                else f"{path.name}: 無法辨識這個 CSV 屬於哪一種帳本（欄位與所有匯入範本都不符）")
            continue
        typed.append((kind, path, text))
    typed.sort(key=lambda item: (_LOAD_ORDER.index(item[0]), item[1].name))

    for kind, path, text in typed:
        norm = normalize_import_csv(text, DATE_COLUMN_BY_KIND[kind])
        if norm.ambiguity is not None:
            problems.append(
                f"{path.name}: 日期格式不明確（{norm.ambiguity.column} 欄可能是 M/D 也可能是"
                " D/M）。請先把日期改成 YYYY-MM-DD 再執行"
            )
            continue
        preview = _BUILDERS[kind](conn, norm.text)
        by_index = {row.index: row for row in preview.rows}
        summary = commit_preview(
            conn, preview, accept=set(by_index), writer=_WRITERS[kind]
        )
        # Both buckets, because this script accepts EVERY row: anything not written is a
        # problem here, and reading only one of the two would make the gate stop noticing
        # refusals the moment `commit_preview` learned to name them separately (2026-08-14).
        for index in [r.index for r in summary.rejected] + summary.skipped:
            kinds = "、".join(sorted({i.kind for i in by_index[index].issues})) or "unknown"
            problems.append(f"{path.name}: 第 {index + 1} 列未寫入（{kinds}）")
    return problems


# ---------------------------------------------------------------------------
# Loading — the corporate actions
# ---------------------------------------------------------------------------


def _parse_actions(text: str) -> tuple[list[CorporateActionInput], list[str]]:
    """Parse the ``--actions`` CSV into validator inputs; return ``(inputs, problems)``.

    :class:`CorporateActionInput` is deliberately permissive (a plain ``str`` kind,
    unconstrained ``Decimal`` ratio terms) so that a bad row reaches
    :func:`validate_corporate_action` and is REJECTED with a zh message, rather than dying
    here as a pydantic error. Only a genuinely unparseable cell is a problem at this stage.
    """
    cols = {canonical_header(h) for h in _header_of(text)}
    missing = sorted(_required_columns(_ACTIONS_KIND) - cols)
    if missing:
        return [], [f"公司行動檔缺少欄位：{'、'.join(missing)}"]

    norm = normalize_import_csv(text, DATE_COLUMN_BY_KIND[_ACTIONS_KIND])
    if norm.ambiguity is not None:
        return [], ["公司行動檔的日期格式不明確，請先改成 YYYY-MM-DD 再執行"]

    inputs: list[CorporateActionInput] = []
    problems: list[str] = []
    for index, raw in enumerate(csv.DictReader(io.StringIO(norm.text))):
        row = {k: (v or "").strip() for k, v in raw.items() if k is not None}
        try:
            account_id, _ = alias_import_account(row["account"])
            carry = row.get("cost_carry", "")
            inputs.append(
                CorporateActionInput(
                    account_id=account_id,
                    date=date.fromisoformat(row["date"]),
                    kind=row["kind"],
                    from_symbol=row["from_symbol"],
                    to_symbol=row["to_symbol"],
                    ratio_to=Decimal(row["ratio_to"]),
                    ratio_from=Decimal(row["ratio_from"]),
                    cost_carry=Decimal(carry) if carry else None,
                    note=row.get("note") or None,
                )
            )
        except (KeyError, ValueError, InvalidOperation):
            problems.append(f"公司行動檔第 {index + 1} 列無法解析（日期或比例欄位格式錯誤）")
    return inputs, problems




def _write_actions(
    conn: sqlite3.Connection, inputs: Sequence[CorporateActionInput]
) -> tuple[int, set[tuple[str, str]]]:
    """Validate and write the supplied actions; return how many were REJECTED **and which**.

    The ``(account_id, symbol)`` keys of the rejected rows — both ends — are returned because
    :func:`_missing_action_rows` cannot reconstruct them: it builds its "already explained"
    set from :func:`list_corporate_actions`, i.e. from **written** rows, and a rejected row is
    by definition never written. Its docstring claimed such a position "is already counted by
    (1) and is excluded here", and that exclusion **provably never fired** — measured
    2026-08-11 on the acceptance corpus, where supplying a row with a decimal ratio and
    omitting it entirely left the ledger in an identical state yet reported ``missing 2``
    against ``missing 1`` for the same one missing row. Same defect class as D13's 「the ⚠
    provably never fires」 and E15/D29: a guard written against a state that cannot occur.
    A count labelled 「下限」 that over-reports is not a lower bound.

    Written INCREMENTALLY, in date order, each row validated against the ledger that already
    holds its predecessors. Validating the whole batch against an empty action ledger would
    reproduce audit F-08 from the other side: E1a asks whether the source position exists on
    the action date, and the second action of a chain only has one once the first is stored.

    ``batch`` is still the FULL supplied set, because E12 (same-date collisions) and E13 (the
    all-accounts rule) are batch-level rules — a per-row check that cannot see its siblings
    rejects a correct multi-account entry and accepts a partial one.

    A rejected row is not written, so the replay proceeds without it; that is what makes the
    rejection count part of "rows needed but not found" rather than a separate verdict.
    """
    rejected = 0
    rejected_keys: set[tuple[str, str]] = set()
    ordered = sorted(inputs, key=lambda a: (a.date, a.from_symbol, a.to_symbol, a.account_id))
    for inp in ordered:
        issues = validate_corporate_action(conn, inp, batch=inputs)
        if any(not issue.needs_confirm for issue in issues):
            rejected += 1
            rejected_keys.add((inp.account_id, inp.from_symbol))
            rejected_keys.add((inp.account_id, inp.to_symbol))
            continue
        insert_corporate_action(
            conn,
            account_id=inp.account_id,
            action_date=inp.date,
            kind=CorporateActionKind(inp.kind.strip().upper()),
            from_symbol=inp.from_symbol,
            to_symbol=inp.to_symbol,
            ratio_to=inp.ratio_to,
            ratio_from=inp.ratio_from,
            cost_carry=inp.cost_carry,
            note=inp.note,
        )
    return rejected, rejected_keys


# ---------------------------------------------------------------------------
# The three checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    """§10.5's three answers for one ticker. Booleans only — see :func:`_report_line`."""

    symbol: str
    oversold: bool
    basis_intact: bool
    shares_reconcile: bool

    @property
    def ok(self) -> bool:
        return (not self.oversold) and self.basis_intact and self.shares_reconcile


@dataclass(frozen=True)
class _Analysis:
    verdicts: dict[str, _Verdict]
    missing_action_rows: int


def _positions(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every (account, symbol) any ledger names — the candidate set for the parity check.

    The corporate-action ledger joins the union on BOTH ends, because a position acquired
    purely by an EXCHANGE or a SPINOFF appears in no other table (the same hole audit F-07
    found in ``validate._accounts_holding_on``, where its absence was silent).
    """
    rows = conn.execute(
        "SELECT DISTINCT account_id, symbol FROM ("
        "  SELECT account_id, symbol FROM transactions"
        "  UNION SELECT account_id, symbol FROM opening_inventory"
        "  UNION SELECT account_id, symbol FROM dividends"
        "  UNION SELECT account_id, from_symbol AS symbol FROM corporate_actions"
        "  UNION SELECT account_id, to_symbol AS symbol FROM corporate_actions"
        ")"
    ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


def _ever_negative(conn: sqlite3.Connection, index: ActionIndex) -> set[tuple[str, str]]:
    """Positions an UNDECLARED sell pushed below zero on its own trade date.

    This is the half of 賣超 that ``Holding.oversold`` structurally cannot report. The replay
    sets the sticky ``ever_oversold`` marker on the position, but ``build_book``'s holdings
    loop DROPS any position whose final signed quantity is exactly zero — so a position that
    oversold and was later bought back to flat carries a discarded cost basis and emits no
    holding at all, i.e. no flag. Detected here instead, with the codebase's own date-aware
    rule and its own function: a sell is an oversell exactly when the position at the CLOSE
    of its trade date is negative (same-day buys legitimately cover it, which is why
    ``shares_through`` and not ``shares_on``).

    Scoped to UNDECLARED sells, and that scoping is what makes the test exact rather than
    heuristic: a declared short also drives the signed count negative, but it is not an
    oversell — and in ``build_book`` a sell that is not declared and exceeds the LONG lot is
    an oversell whatever the short lot is doing, which is the same condition as a negative
    signed count. So no declared-short exclusion is needed; there is simply nothing to test
    on a position whose only sells were declared.
    """
    found: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT account_id, symbol, trade_date FROM transactions "
        "WHERE side=? AND short_sale=0 ORDER BY trade_date",
        (Side.SELL.value,),
    ):
        key = (str(row["account_id"]), str(row["symbol"]))
        if key in found:
            continue
        held = shares_through(
            conn, key[0], key[1], on=date.fromisoformat(row["trade_date"]), index=index
        )
        if held < _ZERO:
            found.add(key)
    return found


def _analyse(
    conn: sqlite3.Connection, book: Book, index: ActionIndex, *, rejected: int,
    rejected_keys: set[tuple[str, str]],
) -> _Analysis:
    """Turn the replayed book into one verdict per symbol, plus the missing-rows count."""
    by_key = {(h.account_id, h.symbol): h for h in book.holdings}
    oversold_positions = {k for k, h in by_key.items() if h.oversold} | _ever_negative(conn, index)

    by_symbol: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in _positions(conn) | set(by_key):
        by_symbol[key[1]].append(key)

    verdicts: dict[str, _Verdict] = {}
    for symbol, keys in by_symbol.items():
        oversold = any(key in oversold_positions for key in keys)
        # A LIVE holding with a zero cost total is the money-of-record symptom, and it has two
        # causes: the oversell path (which zeroes both totals) and F-13's zero-cost opening
        # (which does NOT trip any flag — the case §10.5 calls strictly worse). The oversell
        # term is not redundant with it: a later buy re-cost the position, so the holding can
        # carry a NON-zero total that is still missing everything the oversell discarded.
        zero_basis = any(by_key[k].original_cost_total == _ZERO for k in keys if k in by_key)
        reconciled = all(
            current_shares(conn, k[0], k[1], index=index)
            == (by_key[k].shares if k in by_key else _ZERO)
            for k in keys
        )
        verdicts[symbol] = _Verdict(
            symbol=symbol,
            oversold=oversold,
            basis_intact=(not oversold) and not zero_basis,
            shares_reconcile=reconciled,
        )
    return _Analysis(
        verdicts=verdicts,
        missing_action_rows=_missing_action_rows(
            conn, book, oversold_positions, rejected=rejected,
            rejected_keys=rejected_keys),
    )


def _missing_action_rows(
    conn: sqlite3.Connection,
    book: Book,
    oversold_positions: set[tuple[str, str]],
    *,
    rejected: int,
    rejected_keys: set[tuple[str, str]],
) -> int:
    """Corporate-action rows the run NEEDED and the ledger does not contain — a LOWER BOUND.

    §10.5 requires this number so that a ``FAIL`` distinguishes "the replay is wrong" from
    "the ledger is still missing rows" — two very different next actions (escalate, versus go
    and collect more data). It is the sum of three populations, each of which is a row the
    replay required and did not get:

    1. rows supplied in ``--actions`` that validation REJECTED, so they were never written.
       The file names them; the ledger does not have them, and to every downstream number
       that is indistinguishable from never having been supplied. E13's all-accounts gap
       lands here: a partially-covered event rejects its own supplied rows;
    2. rows that were written but the replay REFUSED (``Book.unapplied_actions``). Present in
       the table, absent from the replay's effect — again the same thing to every number;
    3. one per position that still trips the undeclared-oversell guard and carries NO stored
       action row of its own. At least one row is missing there — that is the entire premise
       of §1 — but not how many, so it counts as one.

    **A position whose action was supplied and REJECTED is excluded from (3)**, because (1)
    already counted it. That exclusion needs ``rejected_keys`` to work, and the reason is the
    whole of this parameter's justification: ``covered`` below is built from
    :func:`list_corporate_actions` — the rows actually **written** — and a rejected row is by
    definition never written, so it can never appear there. Keying the exclusion on ``covered``
    alone made it **unreachable**, and the count then reported the same one missing row twice.
    Measured 2026-08-11 on the acceptance corpus: a supplied-but-rejected row (decimal ratio,
    E6a) and an omitted row leave the ledger **identical** — every per-ticker line is
    byte-identical between the two runs — yet reported ``missing 2`` against ``missing 1``.
    See :func:`_write_actions`; the guard now excludes on ``covered | rejected_keys``.

    Deliberately NOT counted: a guess at how many rows would fix an oversell. The script
    cannot know whether one SPLIT or a SPLIT and an EXCHANGE is missing, and inventing a
    number is the fabrication ``data-and-pricing.md`` forbids. Hence "lower bound", stated in
    the label and in ``--help``: ``> 0`` means the ledger is incomplete; ``0`` with a FAIL
    means the replay is what needs looking at. A lower bound that OVER-reports is not a lower
    bound, which is why the double count was a defect and not a cosmetic imprecision.
    """
    stored = list_corporate_actions(conn)
    covered = {(s.account_id, s.from_symbol) for s in stored}
    covered |= {(s.account_id, s.to_symbol) for s in stored}
    covered |= rejected_keys
    unexplained = sum(1 for key in oversold_positions if key not in covered)
    return rejected + len(book.unapplied_actions) + unexplained


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_EPILOG = """\
formats
  --export   the project's canonical ledger CSVs (one file, or a directory of *.csv),
             dispatched by header: transactions / dividends / fx / openings. These are the
             app's own import templates — download them from 匯入 if unsure.
  --actions  a corporate-action CSV with the columns:
             account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note
             kind is SPLIT / EXCHANGE / SPINOFF; cost_carry is SPINOFF-only; note optional.
             ratio_to / ratio_from are two POSITIVE INTEGERS (3-for-1 is 3 and 1; 1-for-20
             is 1 and 20) — never a pre-divided decimal (D14).

output
  one line per affected ticker: symbol · 賣超 · 成本完整 · 股數一致 (yes/no), then the count
  of corporate-action rows the run needed and did not find (a LOWER bound: rejected rows +
  rows the replay refused + one per unexplained 賣超 position), then PASS or FAIL n.
  n is the number of FINDINGS — failing tickers plus missing rows — so the two lines above
  it add up to it. PASS therefore requires both: every ticker clean AND nothing missing,
  because a ledger that is still short a row has not tested the thing being accepted.
  Amounts, share counts and account names are never printed, so the output is safe to paste.

reading a FAIL
  missing > 0  ->  the ledger is still missing rows: go and supply them.
  missing = 0  ->  the replay is wrong: escalate. §10.5 makes this BLOCKING either way.

exit codes
  0 PASS · 1 FAIL (blocking — see §10.5) · 2 could not run
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_corporate_actions.py",
        description=(
            "§10.5's acceptance run: replay a real broker export plus its corporate actions "
            "and report, per affected ticker, whether 賣超 tripped, whether original_total "
            "survived, and whether the two share paths agree. Reads only the paths given; "
            "writes nothing to disk; prints no amounts."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # No defaults, by design (D27): a defaulted path is a path that can be read by accident,
    # and the data this runs against is git-ignored real financial history.
    parser.add_argument("--export", required=True, metavar="<path>",
                        help="ledger CSV, or a directory of them (no default)")
    parser.add_argument("--actions", required=True, metavar="<path>",
                        help="corporate-action CSV (no default)")
    return parser


def _open_scratch_db() -> sqlite3.Connection:
    """An in-memory scratch ledger. Nothing this script reads is ever written to disk."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    # The pricing tables are created EMPTY: the fee engine resolves a trade-date USD/MYR rate
    # for the Moomoo US stamp (FE-D2) and would otherwise raise on a missing table. Empty is
    # the correct state — no price is fetched, and none of the three checks reads one.
    create_pricing_tables(conn)
    seed_accounts(conn)
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_output()  # before parse_args: --help is zh too
    args = _build_parser().parse_args(argv)
    export = Path(args.export)
    actions = Path(args.actions)
    for label, path in (("--export", export), ("--actions", actions)):
        if not path.exists():
            _problem(f"{label}: 找不到這個路徑")
            return 2

    conn = _open_scratch_db()
    try:
        action_inputs, problems = _parse_actions(_read_text(actions))
        files = _csv_files(export)
        if not files:
            problems.append("--export: 這個資料夾裡沒有任何 .csv")
        pairs: set[tuple[str, str]] = set()
        for path in files:
            pairs |= _account_symbol_pairs(_read_text(path))
        pairs |= _account_symbol_pairs(_read_text(actions))
        _register_missing(conn, pairs)
        problems += _load_export(conn, files)
        if problems:
            for message in problems:
                _problem(message)
            _problem("無法完成驗證：帳本沒有完整載入，先修正上面的問題再重跑")
            return 2

        rejected, rejected_keys = _write_actions(conn, action_inputs)
        try:
            book = build_book(load_ledger_bundle(conn), allow_oversell=True)
        except (ValueError, KeyError) as exc:
            _problem(f"重播失敗（{type(exc).__name__}）：帳本裡有這個模型無法記錄的事件")
            return 2
        analysis = _analyse(conn, book, load_action_index(conn), rejected=rejected,
                           rejected_keys=rejected_keys)
    finally:
        conn.close()

    affected = {sym for a in action_inputs for sym in (a.from_symbol, a.to_symbol)}
    affected |= {sym for sym, v in analysis.verdicts.items() if not v.ok}
    # Column width is measured on the MASKED forms, or an option contract's 24-character
    # string pads every other line to its length and announces on stdout that one was present.
    width = max((len(_safe_symbol(s)) for s in affected), default=1)
    masked = sum(1 for s in affected if _safe_symbol(s) != s)
    if masked:
        _problem(f"注意：{masked} 個代號不是股票代號（含空白或小數，例如選擇權合約），"
                 "報表已遮蔽其內容。本功能不處理選擇權公司行動（規格 §範圍）")
    failed = 0
    for symbol in sorted(affected):
        # A symbol named by a supplied action can be absent from every ledger when that action
        # was rejected — it has no position, so it has nothing wrong with it. Reported as
        # clean; its absence is what the missing-rows count below is for.
        verdict = analysis.verdicts.get(symbol, _Verdict(symbol, False, True, True))
        _report_line(symbol, verdict.oversold, verdict.basis_intact,
                     verdict.shares_reconcile, width)
        failed += 0 if verdict.ok else 1
    print(f"缺少的公司行動筆數（下限） {analysis.missing_action_rows}")
    # The PASS CRITERION is "no failing ticker AND no missing row" — a missing row can never
    # print PASS. That is reachable with every ticker clean: an action rejected at entry, or
    # one the replay refused, leaves the affected position in PRE-action terms, where BOTH
    # share paths agree and no sell has yet exposed it. Green there would be green having
    # tested none of the feature — trap #22 arriving through the gate written to prevent it.
    #
    # The two counts are REPORTED SEPARATELY rather than summed (2026-08-11). The sum was
    # measured to misrepresent scale in three ways: the missing rows are the CAUSE of the
    # failing tickers, so a single defect is counted twice (the corpus with every action
    # removed printed `FAIL 17` for 8 problems); the halves use different denominators —
    # tickers are per SYMBOL, missing rows per (account, symbol) POSITION, so one split held
    # in two accounts is 1 ticker and 2 rows; and both numbers are already on the two lines
    # above, so the sum adds nothing but a bigger number. The criterion is unchanged —
    # `failed + missing == 0` and `failed == 0 and missing == 0` are the same predicate over
    # two non-negative counts — so this is a display change, not a gate change.
    ok = failed == 0 and analysis.missing_action_rows == 0
    print("PASS" if ok else f"FAIL — 標的 {failed}、缺漏 {analysis.missing_action_rows}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
