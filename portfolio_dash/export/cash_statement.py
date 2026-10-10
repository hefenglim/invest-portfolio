"""現金收支明細 exports — reconciliation-grade CSV + print-optimized HTML (FU-D5).

Both are built from the SAME calculation seam the dashboard/statement view uses
(``portfolio.cash.account_statement`` → ``pool_lines`` + ``running_statement``), so the
downloaded numbers are byte-identical to the on-screen statement. They serialize
already-computed ``Decimal`` values at source precision (CSV) or format them for print
(HTML); neither computes any money of its own.

Scope: one account, optionally one currency. ``ccy=None`` dumps every pool the account
has activity in, each with its OWN per-(account, ccy) running balance — currencies are
never blended. The full statement is exported (all rows), never the paged API window.

⑪ (owner ruling 2026-10-05): a dividend the replay refused is listed — the owner reconciles
this statement against the broker's, and a row that vanished would read as a lost one — but
it moves no balance, and both exports mark it with the screen's own words
(``portfolio.cash.UNBOOKED_DIVIDEND_NOTE``): the CSV in its ``unbooked`` column, the printed
report in the row's 說明 and in the section title, with the replay's own sentence for each
refused row under the table (X1). The split is the replay's own
(``portfolio.returns.replay_dividend_refusals``), the same one ``GET /api/cash/statement``
reads.

Options (spec §6.2, §9): each export computes ``option_side_of`` ONCE (``portfolio.cash.
option_sides_of`` — the one §6.1 classifier walk over the whole stored ledger) and hands it to
``account_statement``, which labels the option trade lines. Such a line is DESCRIBED from its
label — 「賣出開倉 2 口 XYZ 2026-12-18 50 Put」 (:func:`option_line_text`): in the printed
report's 說明, and in the CSV's ``note_ref`` cell (which on a stock trade line is the bare
symbol, already in ``symbol``). Its money columns are the trade's own, untouched. A stock
ledger takes no walk and both files are byte-identical (spec §10).
"""

import sqlite3
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
    list_accounts,
    list_cash_movements,
    list_corporate_actions,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.export.report_html import (
    _NULL,
    _document,
    _esc,
    _fmt_amount,
    _fmt_shares,
    _page_footer,
    _page_header,
    _version_line,
)
from portfolio_dash.portfolio.cash import (
    UNBOOKED_DIVIDEND_NOTE,
    UNBOOKED_DIVIDEND_REASON,
    CashLine,
    account_statement,
    option_sides_of,
)
from portfolio_dash.portfolio.returns import DividendRefusals, replay_dividend_refusals
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.ledger import counts_by, pending_from
from portfolio_dash.shared.options import UNIT_CONTRACT, contract_name, shares_to_contracts
from portfolio_dash.shared.wire import decimal_str

_ZERO = Decimal("0")
_MINUS = "−"  # U+2212 minus sign (matches web/format.js signed())

# ``counts_from`` (DEF-056 R5, appended LAST so every earlier column keeps its index): the
# day a row dated after ``as_of`` starts to count — empty for a row that already counts. Its
# ``balance`` is then a projection past the footer's ``as_of``, exactly what the screen's
# 「未來日期」 badge and the printed report's cut line say. This CSV is a reconciliation
# export, not an import format, so the extra column breaks no round trip (the four ledger
# CSVs, which ARE re-importable, stay unflagged for that reason — DEF-026).
# ``unbooked`` (⑪, 2026-10-05) sits right after ``balance`` — the column it explains: on a
# dividend the replay refused it reads 「未入帳（待釐清）」 and that row's ``balance`` is the
# one before it (the payout moved nothing); empty on every other row. Inserted rather than
# appended so ``counts_from`` stays the last column, which DEF-056's readers key on; every
# column before it keeps its index.
_CSV_COLUMNS = [
    "date", "ccy", "kind", "symbol", "name", "qty", "price", "fee", "tax",
    "note_ref", "delta", "balance", "unbooked", "counts_from",
]

# Statement kind -> zh label (mirrors web/cash.js KIND_LABEL; the report is display-only).
# Statement line kinds are the LOWERCASED ledger kind for a cash movement (`cash.py:191`)
# plus the five kinds that are not cash movements at all. The movement half is derived from
# `CASH_KIND_ZH` rather than retyped: this dict listed four movement kinds and stopped there,
# so 利息 / 融資利息 / 券商費用 printed as `interest` / `interest_expense` / `broker_fee` on a
# Chinese statement from the day they were added (2026-08-13) until 2026-08-16. The `.get`
# below still has a fallback, but nothing reachable depends on it any more.
_KIND_ZH = {
    **{k.lower(): zh for k, zh in CASH_KIND_ZH.items()},
    "fx_in": "換入", "fx_out": "換出", "buy": "買入", "sell": "賣出", "dividend": "股利",
}

_Ledgers = tuple[
    list[StoredCashMovement], list[StoredFxConversion],
    list[StoredTransaction], list[StoredDividend], dict[str, Instrument],
    list[StoredDividend], DividendRefusals, dict[int, str],
]


def _load(conn: sqlite3.Connection) -> _Ledgers:
    """The ledger inputs ``account_statement`` needs (loaded once): the five pool ledgers,
    the dividends split by the replay's own refusal (⑪ — booked rows in the fourth slot, the
    refused ones next, for listing only, and the refusal itself after them for its reasons),
    exactly as ``api/routers/cash.py::load_cash_ledgers`` splits them for the on-screen
    statement, and last the §6.1 label of every option trade row (options spec §9). One replay
    and one classifier walk per export; a ledger with no option contract reads no corporate
    action and walks nothing."""
    txs = list_transactions(conn)
    divs = list_dividends(conn)
    instruments = {i.symbol: i for i in list_instruments(conn)}
    refused = replay_dividend_refusals(load_ledger_bundle(
        conn, transactions=txs, dividends=divs, instruments=instruments))
    option_side_of = (option_sides_of(txs, list_corporate_actions(conn), instruments)
                      if any(i.is_option for i in instruments.values()) else {})
    return (
        list_cash_movements(conn),
        list_fx_conversions(conn),
        txs,
        refused.booked(divs),
        instruments,
        refused.unbooked(divs),
        refused,
        option_side_of,
    )


def option_line_text(ln: CashLine) -> str | None:
    """「賣出開倉 2 口 XYZ 2026-12-18 50 Put」 — an option trade line described from its §6.1
    label (spec §6.2); ``None`` for every other line. The words are the label's, the 口數 the
    multiplier's (``shared/options.py``), the name the symbol's own parse."""
    if ln.option_side is None or ln.symbol is None or ln.qty is None:
        return None
    contracts = decimal_str(shares_to_contracts(ln.qty))
    name = contract_name(ln.symbol) or ln.name or ln.symbol
    return f"{ln.option_side} {contracts} {UNIT_CONTRACT} {name}"


def _d(value: Decimal | None) -> str:
    """A source-precision CSV cell for an optional Decimal (empty when absent)."""
    return decimal_str(value) if value is not None else ""


# --- CSV ----------------------------------------------------------------------------------


def build_cash_statement_csv(
    conn: sqlite3.Connection, *, account: str, ccy: Currency | None, now: datetime
) -> ExportArtifact | None:
    """Reconciliation-grade CSV of one account's cash statement (all pools when ``ccy`` is
    None). Unknown account → None (router answers 400). Raw source-precision strings."""
    if account not in {a.account_id for a in list_accounts(conn)}:
        return None
    movements, fx, txs, divs, instruments, unbooked, refusals, sides = _load(conn)
    # Options spec §9: ``option_side_of`` computed ONCE per export (in ``_load``).
    statements = account_statement(account, movements, fx, txs, divs, instruments, ccy=ccy,
                                   option_side_of=sides,
                                   unbooked_dividends=unbooked,
                                   unbooked_reason=refusals.reason)
    today = now.date()
    rows: list[list[str]] = []
    for pool_ccy, stmt in statements:
        for ln, bal in stmt:
            pending = pending_from(ln.date, today)
            rows.append([
                ln.date.isoformat(), pool_ccy.value, ln.kind,
                ln.symbol or "", ln.name or "",
                _d(ln.qty), _d(ln.price), _d(ln.fee), _d(ln.tax),
                # An option line is described from its §6.1 label; every other line keeps
                # its ref (a trade's symbol, a movement's note, an fx pair) as before.
                option_line_text(ln) or ln.ref, decimal_str(ln.delta), decimal_str(bal),
                "" if ln.booked else UNBOOKED_DIVIDEND_NOTE,
                pending.isoformat() if pending is not None else "",
            ])
    as_of = today.isoformat()
    scope = ccy.value if ccy is not None else "all"
    footer = [f"account={account}, ccy={scope}, as_of={as_of}, generated={now.isoformat()}"]
    return csv_artifact(
        f"cash_statement_{account}_{scope}_{as_of}.csv",
        header=_CSV_COLUMNS, rows=rows, footer_lines=footer,
    )


# --- print HTML report --------------------------------------------------------------------


def _fmt_price(value: Decimal | None, ccy: str) -> str:
    """A per-share price with thousands separators (MY 3 dp, others 2 dp; — when absent)."""
    if value is None:
        return _NULL
    dp = 3 if ccy == "MYR" else 2
    q = value.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    return f"{q:,.{dp}f}"


def _fmt_rate(value: Decimal | None) -> str:
    """An implied FX rate at 4 dp (— when absent)."""
    if value is None:
        return _NULL
    q = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return f"{q:,.4f}"


def _fmt_signed(value: Decimal | None, ccy: str) -> str:
    """A signed amount in *ccy*'s minor unit (+ / U+2212; — when absent)."""
    if value is None:
        return _NULL
    body = _fmt_amount(abs(value), ccy)
    if value > _ZERO:
        return f"+{body}"
    if value < _ZERO:
        return f"{_MINUS}{body}"
    return body


def _describe(ln: CashLine, ccy: str) -> str:
    """Human-readable 說明 for a statement line (mirrors web/cash.js describe()).

    Every dynamic string (name / symbol / fx pair / counter ccy / note) is escaped;
    numbers come from the safe formatters. The result is trusted HTML for a ``<td>``."""
    option = option_line_text(ln)
    if option is not None:
        # Options spec §6.2: the §6.1 label in words, then the trade's own premium and costs.
        return (
            f"{_esc(option)} @ {_fmt_price(ln.price, ccy)}"
            f"（費 {_fmt_amount(ln.fee, ccy)}・稅 {_fmt_amount(ln.tax, ccy)}）"
        )
    if ln.kind in ("buy", "sell"):
        verb = "買入" if ln.kind == "buy" else "賣出"
        name = f"{ln.name}（{ln.symbol}）" if ln.name else (ln.symbol or "")
        return (
            f"{verb} {_esc(name)} {_fmt_shares(ln.qty)} 股 @ {_fmt_price(ln.price, ccy)}"
            f"（費 {_fmt_amount(ln.fee, ccy)}・稅 {_fmt_amount(ln.tax, ccy)}）"
        )
    if ln.kind == "dividend":
        name = f"{ln.name}（{ln.symbol}）" if ln.name else (ln.symbol or "")
        # ⑪: the screen's badge, in words — a static page has no hover for the reason, so
        # the section's note carries it (`_pool_section`).
        mark = "" if ln.booked else f"・{_esc(UNBOOKED_DIVIDEND_NOTE)}"
        return f"配息 {_esc(name)}{mark}"
    if ln.kind in ("fx_in", "fx_out"):
        parts = [f"換匯 {_esc(ln.ref)}"]
        if ln.fx_rate is not None:
            # M5-01, second surface. `web/cash.js::describe()` was corrected to label this
            # rate; this builder is the same sentence's other copy and was left bare, so a
            # single conversion then read 「1 USD = 31.77 TWD」 on screen and 「31.7723」 in
            # the printed report — a screen-vs-print split the fix itself created (before it,
            # both were consistently unlabelled). Found by regression R3, not by the fix.
            # The wording below is `describe()`'s, verbatim: the backend rate is
            # from_amount / to_amount for BOTH legs, so pool ccy + counter ccy recover the
            # pair (fx_out spends from_ccy, fx_in receives to_ccy); with either missing it
            # degrades to the bare rate rather than mislabelling one.
            frm = ccy if ln.kind == "fx_out" else ln.counter_ccy
            to = ln.counter_ccy if ln.kind == "fx_out" else ccy
            if frm and to:
                parts.append(f"@ 1 {_esc(to)} = {_fmt_rate(ln.fx_rate)} {_esc(frm)}")
            else:
                parts.append(f"@ {_fmt_rate(ln.fx_rate)}")
        if ln.counter_amount is not None and ln.counter_ccy:
            parts.append(
                f"（對應 {_fmt_signed(ln.counter_amount, ln.counter_ccy)} {_esc(ln.counter_ccy)}）"
            )
        return " ".join(parts)
    note = (ln.ref or "").strip()
    return _esc(note) if note else "（無備註）"


def _as_of_balance(stmt: list[tuple[CashLine, Decimal]], as_of: date) -> Decimal:
    """The running balance after the LAST line dated on or before ``as_of`` — that day's
    end-of-day balance (M5-06); 0 for a pool with no such line. The statement is
    chronological, so this equals ``cash_balances(..., as_of=as_of)`` for the same pool by
    construction — the figure ``GET /api/cash`` and ``GET /api/cash/statement`` serve."""
    return next((bal for ln, bal in reversed(stmt) if counts_by(ln.date, as_of)), _ZERO)


def _pool_section(
    pool_ccy: Currency, stmt: list[tuple[CashLine, Decimal]], *, as_of: date
) -> str:
    """One pool's printed section.

    「目前餘額」 is the balance AS OF ``as_of`` (M5-06, X8a) — the ``now`` the page header
    was given, never a second clock read (a re-read would date the balance differently from
    the header on an export that crosses midnight). It was ``stmt[-1][1]``, the running
    balance after the LAST row: a projection, not today's balance, whenever a future-dated
    row exists — and a number the 資金 page had stopped showing.

    Future rows are still printed. The web statement lists every row, bounds the balance to
    today and flags the rows past it (``web/cash.js::renderStatement`` — this sentence was
    true of the API only until DEF-056 R5: the page never read ``future``); a static page
    says the same thing its own way: one cut row drawn between the last row that has
    happened and the first that has not, 「（未來）」 on each row past it (a table may break
    across pages, and the cut stays on the first), and the title counting what the balance
    leaves out. Hiding the rows would make the
    printed ledger look like it lost them; folding them in would print a figure no other
    surface shows. With no future row the section is byte-identical to what it was.
    """
    ccy = pool_ccy.value
    bal = _as_of_balance(stmt, as_of)
    future = sum(1 for ln, _ in stmt if not counts_by(ln.date, as_of))
    unbooked = sum(1 for ln, _ in stmt if not ln.booked)
    head = (
        '<tr><th>日期</th><th class="l">類型</th><th class="l">說明</th>'
        "<th>金額</th><th>餘額</th></tr>"
    )
    cut = (
        '<tr class="cut"><td class="l note" colspan="5">'
        f"以下 {future} 筆為未來日期（{_esc(as_of.isoformat())} 之後），不計入目前餘額；"
        "其餘額欄為投影</td></tr>"
    )
    if not stmt:
        body = '<tr><td class="l" colspan="5">本帳戶此幣別尚無紀錄</td></tr>'
    else:
        rows: list[str] = []
        for ln, b in stmt:
            is_future = not counts_by(ln.date, as_of)
            if is_future and len(rows) == len(stmt) - future:
                rows.append(cut)  # once, before the first future row (rows are chronological)
            kind = _esc(_KIND_ZH.get(ln.kind, ln.kind)) + ("（未來）" if is_future else "")
            classes = " ".join(c for c, on in (("future", is_future),
                                               ("unbooked", not ln.booked)) if on)
            rows.append(
                (f'<tr class="{classes}">' if classes else "<tr>")
                + f'<td class="num">{_esc(ln.date.isoformat())}</td>'
                f'<td class="l">{kind}</td>'
                f'<td class="l">{_describe(ln, ccy)}</td>'
                f'<td class="num">{_fmt_signed(ln.delta, ccy)}</td>'
                f'<td class="num">{_fmt_amount(b, ccy)}</td>'
                "</tr>"
            )
        body = "".join(rows)
    title = f"{_esc(ccy)} 資金池 · 目前餘額 {_fmt_amount(bal, ccy)} {_esc(ccy)}"
    if future:
        title += f"（截至 {_esc(as_of.isoformat())}，不含 {future} 筆未來日期）"
    reasons = ""
    if unbooked:
        # ⑪ / X1: the count in the title, and under the table each refused row with the
        # replay's own sentence — the static page's hover.
        title += f"（另有 {unbooked} 筆股利未入帳，不計入餘額）"
        reasons = "".join(
            f'<p class="note">{_esc(ln.date.isoformat())}・{_esc(ln.symbol or "")}・'
            f"{_esc(UNBOOKED_DIVIDEND_NOTE)}：{_esc(ln.reason or UNBOOKED_DIVIDEND_REASON)}</p>"
            for ln, _ in stmt if not ln.booked)
    return (
        f"<section><h2>{title}</h2>"
        f'<p class="note">共 {len(stmt)} 筆</p>'
        f"<table><thead>{head}</thead><tbody>{body}</tbody></table>{reasons}</section>"
    )


def _header(account_id: str, ccy: Currency | None, now: datetime) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")
    scope = ccy.value if ccy is not None else "全部幣別"
    meta = [
        # DEF-045: the account as an ``{account:<id>}`` token, resolved to its display name by
        # web/api.js's download seam (``_resolveBlobRefs``) — it read 「帳戶 TW Broker
        # （tw_broker）」: the English API label plus the raw id, on a page the owner prints.
        f"帳戶 {_esc(account_ref(account_id))}",
        f"幣別 {_esc(scope)}",
        f"生成時間 {_esc(gen)}",
        _version_line(),
    ]
    nature = (
        "本明細由帳本重算：入金 − 出金 ± 換匯 ± 買賣收付 ＋ 現金股利；"
        "餘額為各資金池的滾動餘額，不同幣別不混算。"
    )
    return _page_header(title="現金收支明細", meta_lines=meta, nature=nature)


def build_cash_statement_report_html(
    conn: sqlite3.Connection, *, account: str, ccy: Currency | None, now: datetime
) -> ExportArtifact | None:
    """Print-optimized 現金收支明細 report (one section per pool). Unknown account → None."""
    if account not in {a.account_id for a in list_accounts(conn)}:
        return None
    movements, fx, txs, divs, instruments, unbooked, refusals, sides = _load(conn)
    statements = account_statement(account, movements, fx, txs, divs, instruments, ccy=ccy,
                                   option_side_of=sides,
                                   unbooked_dividends=unbooked,
                                   unbooked_reason=refusals.reason)
    # One clock for the whole page: the header's ``now`` dates the balances too (M5-06).
    sections = [_pool_section(c, stmt, as_of=now.date()) for c, stmt in statements] or [
        '<section><p class="note">此帳戶尚無現金收支紀錄</p></section>'
    ]
    body = "\n".join([
        _header(account, ccy, now),
        *sections,
        _page_footer("本明細為帳本重算結果；更正以新紀錄沖銷，原紀錄永久保留。"),
    ])
    scope = ccy.value if ccy is not None else "all"
    filename = f"cash-statement-{account}-{scope}-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8",
        _document("現金收支明細", body).encode("utf-8"),
    )
