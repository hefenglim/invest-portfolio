"""Downloadable CSV import templates — annotated header + worked example rows per kind.

SINGLE SOURCE OF TRUTH: each kind's header is the column-order constant defined next to that
kind's parser (:data:`csv_import.TRANSACTION_COLUMNS`, :data:`dividend_import.DIVIDEND_COLUMNS`,
:data:`fx_import.FX_COLUMNS`, :data:`opening_import.OPENING_COLUMNS`,
:data:`corporate_action_import.CORPORATE_ACTION_COLUMNS`,
:data:`cash_import.CASH_MOVEMENT_COLUMNS`).  A parser column rename
that is not mirrored here is caught by the round-trip guard test — the generated template must
re-parse through the real preview builder (after the import-seam header canonicalization) with
zero ``parse_error`` rows (tests/contract/test_import_template.py).

FU-D19 — the rendered header is ANNOTATED: the date column carries its ISO format hint
(``date(YYYY-MM-DD)`` / ``build_date(YYYY-MM-DD)``) and every optional column is marked
``(選填)``, so a user editing in Excel sees the expected date shape and what may be blank.  The
parsers canonicalize headers (:func:`csv_import.canonical_header` strips the annotation), so the
annotated template round-trips.  Required-vs-optional is declared in :data:`OPTIONAL_COLUMNS`
(mirrors each parser's own required set) and the date column in :data:`DATE_COLUMN_BY_KIND`.

The rendered text uses CRLF line endings and carries NO BOM; the HTTP layer
(:mod:`api.routers.input_center`) prepends the UTF-8 BOM so Excel opens the Chinese ``note``
column cleanly.  The importers strip a leading BOM defensively, so a downloaded-then-reuploaded
template round-trips whether or not the BOM survives the editor.
"""

import csv
import io

from portfolio_dash.data_ingestion.cash_import import CASH_MOVEMENT_COLUMNS
from portfolio_dash.data_ingestion.corporate_action_import import CORPORATE_ACTION_COLUMNS
from portfolio_dash.data_ingestion.csv_import import TRANSACTION_COLUMNS
from portfolio_dash.data_ingestion.dividend_import import DIVIDEND_COLUMNS
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS
from portfolio_dash.data_ingestion.opening_import import OPENING_COLUMNS

# ⚠ A new kind is NOT one edit. Seven registration points (audit F-28), and every one of
# them fails quietly on its own: the parser module + its ``*_COLUMNS`` constant, then
# TEMPLATE_KINDS / DATE_COLUMN_BY_KIND / OPTIONAL_COLUMNS / _HEADERS / _ROWS below, then
# ``api/routers/input_center._BUILDERS`` and ``._WRITERS``, then ``web/input.js``'s
# CSV_KINDS + CSV_HINTS. Miss the router maps and the kind 400s as "未知 kind"; miss the
# frontend and it exists but is unreachable; miss DATE_COLUMN_BY_KIND and the import seam
# raises a KeyError on a date column that is not there.
#
# ⚠ An EIGHTH point exists for a kind whose builder needs an injected dependency, and
# ``cash`` is the first one: its ``_BUILDERS`` entry is a named wrapper that BINDS the
# pool arithmetic (``input_center._cash_builder``), because ``build_cash_movement_preview``
# takes its probe as a REQUIRED argument. That is the point — see the module docstring of
# ``data_ingestion/cash_import.py``. Registering the bare parser does not compile.
TEMPLATE_KINDS: tuple[str, ...] = (
    "transactions", "dividends", "fx", "openings", "corporate_actions", "cash",
)

# The date column per kind (drives both the header annotation and the router's date
# normalization). Mirrors each parser's date field: transactions/dividends/fx/cash use
# ``date``, opening_inventory uses ``build_date``.
DATE_COLUMN_BY_KIND: dict[str, str] = {
    "transactions": "date",
    "dividends": "date",
    "fx": "date",
    "openings": "build_date",
    "corporate_actions": "date",
    "cash": "date",
}

# Optional (may-be-blank) columns per kind — mirrors the required set each parser enforces:
# transactions require account/symbol/side/date/shares/price; dividends require
# account/symbol/date/type/gross; fx requires every column; openings require
# account/symbol/shares/original_cost_total/build_date and treat original_avg_cost as the
# legacy-optional column (A6). Marked ``(選填)`` in the downloadable template header.
OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    # short_sale (2026-08-11): a DECLARED short sale. The engine has carried the flag since
    # 2026-07-31 (owner ruling, spec option C) but the canonical CSV could not express it, so
    # an imported declared short became an ORDINARY sell — which the 賣超 guard then flags as
    # an undeclared oversell and DISCARDS the cost basis for. Found by §10.5's acceptance run,
    # where the owner's real export contains a declared short: the run reported a failure that
    # had nothing to do with corporate actions and routed the owner to hunt for a missing
    # action row that does not exist. It is never inferred (domain-ledger.md: the system cannot
    # distinguish a genuine short from a missing buy), so an explicit column is the only way in.
    "transactions": frozenset({"fee", "tax", "daytrade", "short_sale", "note"}),
    "dividends": frozenset({"withholding", "net", "reinvest_shares", "reinvest_price"}),
    "fx": frozenset(),
    "openings": frozenset({"original_avg_cost"}),
    # cost_carry is SPINOFF-only (E8 rejects it on the other two kinds); note is free text.
    # ratio_to / ratio_from are BOTH required — never one `ratio` column (E6a, §3.1(ii)).
    "corporate_actions": frozenset({"cost_carry", "note"}),
    # acq_home_amount (spec 2026-07-30, F1) is the HOME-currency cost of a FOREIGN credit.
    # Optional because "I do not know what rate I got" is an honest answer and a guessed
    # rate is not — the amount then funds the pool but stays out of the weighted average,
    # and the dashboard discloses that through covered_ratio / fx_basis_gap. Blank on a
    # home-currency row is not merely optional but REQUIRED: a cost there is rejected.
    # There is deliberately NO acq_rate column — store the amount, never the rate.
    "cash": frozenset({"acq_home_amount", "note"}),
}

# Example rows align POSITIONALLY with each kind's column constant. Fixed recent ISO dates;
# accounts + symbols are the seeded ids (tw_broker / schwab / moomoo_my, 2330 / AAPL / a MY
# ETF) so the guard test resolves every reference cleanly. moomoo_my is the merged dual-market
# account (Batch B), so it books BOTH a US-market and an MY-market example row. is_etf is NOT a
# column — it comes from the instrument registry (hence the MY-ETF row's note).
_TRANSACTION_ROWS: list[list[str]] = [
    # TW buy — fee/tax auto-computed (blank -> account fee-rule set fills them).
    ["tw_broker", "2330", "buy", "2026-07-10", "1000", "612.5", "", "", "", "", ""],
    # TW sell, day-trade (當沖) -> 0.15% sell tax; daytrade flag = 1.
    ["tw_broker", "2330", "sell", "2026-07-13", "1000", "620", "", "", "1", "", "當沖"],
    # Schwab US sell — SEC/TAF regulatory fees auto-computed on the sell side.
    ["schwab", "AAPL", "sell", "2026-07-13", "5", "210", "", "", "", "", ""],
    # Moomoo MY, US-market buy (settles USD; US fee/dividend rules bound to this market).
    ["moomoo_my", "AAPL", "buy", "2026-07-14", "3", "205", "", "", "", "", ""],
    # Moomoo MY, MY-market ETF buy — the ETF stamp exemption keys off the registry flag.
    ["moomoo_my", "0800EA", "buy", "2026-07-14", "100", "1.25", "", "", "", "",
     "ETF 以標的登錄為準"],
    # Manual fee + tax override — both columns supplied -> auto-compute skipped for this row.
    ["tw_broker", "2330", "sell", "2026-07-15", "500", "620", "20", "5", "", "",
     "手動覆寫費稅"],
    # A DECLARED short sale (short_sale = 1). Without this column the row books as an
    # ordinary sell, the 賣超 guard flags it and the position's cost basis is DISCARDED —
    # the exact failure §10.5 surfaced on the owner's real export. Never inferred.
    ["schwab", "AAPL", "sell", "2026-07-16", "10", "215", "", "", "", "1", "宣告放空"],
]

_DIVIDEND_ROWS: list[list[str]] = [
    # TW cash dividend (net = gross, single-tier) — reduces adjusted cost.
    ["tw_broker", "2330", "2026-07-10", "CASH", "5000", "", "", "", ""],
    # US DRIP — 30% withholding + net auto-computed; reinvest_shares from net / reinvest_price.
    ["schwab", "AAPL", "2026-07-11", "DRIP", "100", "", "", "", "150"],
]

_FX_ROWS: list[list[str]] = [
    # Schwab funds USD from TWD; Moomoo MY funds USD from MYR.
    ["schwab", "2026-07-10", "TWD", "32000", "USD", "1000"],
    ["moomoo_my", "2026-07-11", "MYR", "4400", "USD", "1000"],
]

_OPENING_ROWS: list[list[str]] = [
    # A6: original_cost_total is REQUIRED (authoritative money of record); original_avg_cost is
    # legacy-optional. Row 1 supplies total only; row 2 also carries a matching legacy avg.
    ["tw_broker", "2330", "1000", "500000", "2026-01-02", ""],
    ["schwab", "AAPL", "10", "1000", "2026-01-02", "100"],
]

_CORPORATE_ACTION_ROWS: list[list[str]] = [
    # A forward SPLIT: 2330 is held in tw_broker only, so ONE row is the complete entry
    # (E13 counts holding accounts, not markets). Fully clean against the seeded ledger.
    ["tw_broker", "2026-06-01", "SPLIT", "2330", "2330", "10", "1", "",
     "一股分割為十股（10 換 1）"],
    # An EXCHANGE and a SPINOFF need a DESTINATION symbol, and the seed has no second
    # same-currency instrument to point at — so these two name placeholders. They are
    # REJECTED on re-upload with 「目的標的 … 尚未註冊」, which is E10 doing its job (D19:
    # keyed on registration, a database fact, never on the shape of the string). The shapes
    # are what the example is for: two integer terms, and cost_carry on the SPINOFF only.
    #
    # ⚠ The two rows use DIFFERENT source positions on purpose (corrected 2026-08-14). Both
    # were AAPL, and an EXCHANGE moves the WHOLE position off its source (§4.2) — so the
    # file said "all my AAPL became NEWCO on the 2nd" and then "spin SPINCO out of my AAPL
    # on the 3rd". A template is teaching material and that pair is not enterable. It read
    # as valid only because the importer's ActionIndex could not see a row's siblings; the
    # moment it could (C2, the chained-action fix), the SPINOFF was correctly refused
    # `no_position_on_action_date`. The example was wrong, not the new verdict.
    ["schwab", "2026-06-02", "EXCHANGE", "AAPL", "NEWCO", "2", "7", "",
     "併購換股：每 7 股換得 2 股 NEWCO（請改成實際已註冊的代號）"],
    ["tw_broker", "2026-06-03", "SPINOFF", "2330", "SPINCO", "1", "2", "0.3",
     "分拆：每 2 股配 1 股 SPINCO，母公司 30% 成本移轉給子公司"],
]

_CASH_ROWS: list[list[str]] = [
    # The set is SELF-FUNDING and dated after the seeded flows on purpose: a withdrawal is
    # validated against the stored ledger PLUS its siblings in the same file, so the example
    # must carry the deposit that covers it — which is also what the example is teaching.
    ["tw_broker", "2026-07-01", "DEPOSIT", "TWD", "600000", "", "初始入金"],
    # A FOREIGN credit with its home-currency acquisition cost (spec F1). schwab funds in
    # TWD and settles in USD, so the USD amount's cost is a TWD amount — never a rate.
    ["schwab", "2026-07-02", "OPENING", "USD", "100000", "3135870", "期初外幣資金"],
    # The same movement WITHOUT a cost: legal, and the honest entry when the rate is not
    # known. It funds the pool but stays out of the FX weighted average (covered_ratio < 1).
    ["moomoo_my", "2026-07-03", "DEPOSIT", "MYR", "5000", "", "取得成本不明時留空,不要猜"],
    # A withdrawal covered by the first row. 出金 / 入金 / 期初 / 折讓款 are accepted as
    # kind labels too; REBATE is normally booked by the 折讓款 inbox confirm (FE-D1), not
    # by hand, so it carries no example row here.
    ["tw_broker", "2026-07-20", "WITHDRAW", "TWD", "50000", "", "提領"],
    # The broker-statement kinds (2026-08-13). INTEREST is a CREDIT that is NOT an FX
    # acquisition — income arising inside the pool inherits the pool's average rate, so
    # acq_home_amount is REJECTED here, exactly as it is on a withdrawal. The other two are
    # DEBITS; they reduce the pool but are NOT subject to the withdrawal overdraft guard,
    # because a fee is a recorded fact and a margin account legitimately runs negative.
    # Directions + FX semantics: portfolio_dash/shared/cash_kinds.py.
    ["schwab", "2026-07-05", "INTEREST", "USD", "12.34", "", "帳戶利息"],
    ["schwab", "2026-07-06", "INTEREST_EXPENSE", "USD", "8.01", "", "融資利息"],
    ["schwab", "2026-07-07", "BROKER_FEE", "USD", "9.79", "", "券商費用"],
]

_HEADERS: dict[str, list[str]] = {
    "transactions": TRANSACTION_COLUMNS,
    "dividends": DIVIDEND_COLUMNS,
    "fx": FX_COLUMNS,
    "openings": OPENING_COLUMNS,
    "corporate_actions": CORPORATE_ACTION_COLUMNS,
    "cash": CASH_MOVEMENT_COLUMNS,
}
_ROWS: dict[str, list[list[str]]] = {
    "transactions": _TRANSACTION_ROWS,
    "dividends": _DIVIDEND_ROWS,
    "fx": _FX_ROWS,
    "openings": _OPENING_ROWS,
    "corporate_actions": _CORPORATE_ACTION_ROWS,
    "cash": _CASH_ROWS,
}


def template_columns(kind: str) -> list[str]:
    """Canonical (un-annotated) header for *kind*; raises ``KeyError`` for an unknown kind."""
    return _HEADERS[kind]


def _annotate(kind: str, col: str) -> str:
    """A single column with its FU-D19 annotation: the date column carries its ISO hint,
    optional columns are marked ``(選填)``; a required non-date column is unchanged."""
    if col == DATE_COLUMN_BY_KIND.get(kind):
        return f"{col}(YYYY-MM-DD)"
    if col in OPTIONAL_COLUMNS[kind]:
        return f"{col}(選填)"
    return col


def annotated_columns(kind: str) -> list[str]:
    """The rendered template header for *kind* — canonical order, with FU-D19 annotations.
    ``canonical_header`` strips the annotations back to :func:`template_columns`, so the
    annotated template still round-trips through every kind's parser."""
    return [_annotate(kind, c) for c in _HEADERS[kind]]


def template_filename(kind: str) -> str:
    """Download filename for *kind*'s template (e.g. ``import_template_transactions.csv``)."""
    return f"import_template_{kind}.csv"


def render_import_template(kind: str) -> str:
    """Render *kind*'s template as CSV text (annotated header row + example rows), CRLF, NO BOM.

    Raises ``KeyError`` for an unknown kind (the router validates against
    :data:`TEMPLATE_KINDS` first, so callers see a 400 rather than a 500).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(annotated_columns(kind))
    for row in _ROWS[kind]:
        writer.writerow(row)
    return buf.getvalue()
