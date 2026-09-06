"""現金收支明細 exports — reconciliation-grade CSV + print-optimized HTML (FU-D5).

Both are built from the SAME calculation seam the dashboard/statement view uses
(``portfolio.cash.account_statement`` → ``pool_lines`` + ``running_statement``), so the
downloaded numbers are byte-identical to the on-screen statement. They serialize
already-computed ``Decimal`` values at source precision (CSV) or format them for print
(HTML); neither computes any money of its own.

Scope: one account, optionally one currency. ``ccy=None`` dumps every pool the account
has activity in, each with its OWN per-(account, ccy) running balance — currencies are
never blended. The full statement is exported (all rows), never the paged API window.
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
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
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
from portfolio_dash.portfolio.cash import CashLine, account_statement
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.wire import decimal_str

_ZERO = Decimal("0")
_MINUS = "−"  # U+2212 minus sign (matches web/format.js signed())

_CSV_COLUMNS = [
    "date", "ccy", "kind", "symbol", "name", "qty", "price", "fee", "tax",
    "note_ref", "delta", "balance",
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
]


def _load(conn: sqlite3.Connection) -> _Ledgers:
    """The five ledger inputs ``account_statement`` needs (loaded once)."""
    return (
        list_cash_movements(conn),
        list_fx_conversions(conn),
        list_transactions(conn),
        list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)},
    )


def _d(value: Decimal | None) -> str:
    """A source-precision CSV cell for an optional Decimal (empty when absent)."""
    return decimal_str(value) if value is not None else ""


# --- CSV ----------------------------------------------------------------------------------


def build_cash_statement_csv(
    conn: sqlite3.Connection, *, account: str, ccy: Currency | None, now: datetime
) -> ExportArtifact | None:
    """Reconciliation-grade CSV of one account's cash statement (all pools when ``ccy`` is
    None). Unknown account → None (router answers 400). Raw source-precision strings."""
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    if account not in accts:
        return None
    movements, fx, txs, divs, instruments = _load(conn)
    statements = account_statement(account, movements, fx, txs, divs, instruments, ccy=ccy)
    rows: list[list[str]] = []
    for pool_ccy, stmt in statements:
        for ln, bal in stmt:
            rows.append([
                ln.date.isoformat(), pool_ccy.value, ln.kind,
                ln.symbol or "", ln.name or "",
                _d(ln.qty), _d(ln.price), _d(ln.fee), _d(ln.tax),
                ln.ref, decimal_str(ln.delta), decimal_str(bal),
            ])
    as_of = now.date().isoformat()
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
    if ln.kind in ("buy", "sell"):
        verb = "買入" if ln.kind == "buy" else "賣出"
        name = f"{ln.name}（{ln.symbol}）" if ln.name else (ln.symbol or "")
        return (
            f"{verb} {_esc(name)} {_fmt_shares(ln.qty)} 股 @ {_fmt_price(ln.price, ccy)}"
            f"（費 {_fmt_amount(ln.fee, ccy)}・稅 {_fmt_amount(ln.tax, ccy)}）"
        )
    if ln.kind == "dividend":
        name = f"{ln.name}（{ln.symbol}）" if ln.name else (ln.symbol or "")
        return f"配息 {_esc(name)}"
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
    return next((bal for ln, bal in reversed(stmt) if ln.date <= as_of), _ZERO)


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
    today and flags the rows past it; a static page says the same thing its own way: one cut
    row drawn between the last row that has happened and the first that has not, 「（未來）」
    on each row past it (a table may break across pages, and the cut stays on the first),
    and the title counting what the balance leaves out. Hiding the rows would make the
    printed ledger look like it lost them; folding them in would print a figure no other
    surface shows. With no future row the section is byte-identical to what it was.
    """
    ccy = pool_ccy.value
    bal = _as_of_balance(stmt, as_of)
    future = sum(1 for ln, _ in stmt if ln.date > as_of)
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
            is_future = ln.date > as_of
            if is_future and len(rows) == len(stmt) - future:
                rows.append(cut)  # once, before the first future row (rows are chronological)
            kind = _esc(_KIND_ZH.get(ln.kind, ln.kind)) + ("（未來）" if is_future else "")
            rows.append(
                ('<tr class="future">' if is_future else "<tr>")
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
    return (
        f"<section><h2>{title}</h2>"
        f'<p class="note">共 {len(stmt)} 筆</p>'
        f"<table><thead>{head}</thead><tbody>{body}</tbody></table></section>"
    )


def _header(acct_name: str, account_id: str, ccy: Currency | None, now: datetime) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")
    scope = ccy.value if ccy is not None else "全部幣別"
    meta = [
        f"帳戶 {_esc(acct_name)}（{_esc(account_id)}）",
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
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    if account not in accts:
        return None
    movements, fx, txs, divs, instruments = _load(conn)
    statements = account_statement(account, movements, fx, txs, divs, instruments, ccy=ccy)
    # One clock for the whole page: the header's ``now`` dates the balances too (M5-06).
    sections = [_pool_section(c, stmt, as_of=now.date()) for c, stmt in statements] or [
        '<section><p class="note">此帳戶尚無現金收支紀錄</p></section>'
    ]
    body = "\n".join([
        _header(accts[account], account, ccy, now),
        *sections,
        _page_footer("本明細為帳本重算結果；更正以新紀錄沖銷，原紀錄永久保留。"),
    ])
    scope = ccy.value if ccy is not None else "all"
    filename = f"cash-statement-{account}-{scope}-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8",
        _document("現金收支明細", body).encode("utf-8"),
    )
