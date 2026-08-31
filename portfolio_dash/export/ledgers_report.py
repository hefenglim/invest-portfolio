"""帳本報告 — a print-optimized, self-contained HTML ledger report over a date range.

Mirrors the ledger data sources the zip exports (the same rows ``/api/ledgers/*`` reads):
交易紀錄 / 股利紀錄 / 換匯紀錄 / 期初庫存 / 公司行動 / 資金收支 — one section per exportable
ledger, and ``tests/contract/test_export_ledger_list.py`` holds them to that. Each section is a
chronological table with a per-section count line and — only where they arise naturally
from the listed columns — simple per-currency ``Decimal`` sums (transaction net cash,
dividend net, FX converted amounts, opening cost). This builder computes NO new money: it
FORMATS already-stored ``Decimal`` values and sums listed columns for display totals. Every
dynamic string is escaped; the stylesheet is inline (zero external assets) so the file opens
offline and prints cleanly across many pages (page-break-safe rows, thead repeats per page).

Date range: transactions/dividends/FX filter on their event date; opening inventory filters
on its build date. An empty section renders 「本區間無紀錄」. The header shows the range
(「全部期間」 when unbounded).
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredCorporateAction,
    StoredDividend,
    StoredFxConversion,
    StoredOpening,
    StoredTransaction,
    list_accounts,
    list_cash_movements,
    list_corporate_actions,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.report_html import (
    _NULL,
    _amount_ccy,
    _document,
    _esc,
    _fmt_amount,
    _fmt_shares,
    _page_footer,
    _page_header,
    _version_line,
)
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH, movement_sign
from portfolio_dash.shared.corporate_actions import KIND_ZH

_ZERO = Decimal("0")
_DIV_TYPE_ZH = {"CASH": "現金", "STOCK": "配股", "DRIP": "DRIP", "NET": "淨額"}


# --- shared helpers -----------------------------------------------------------------------


def _in_range(d: date, frm: str | None, to: str | None) -> bool:
    """Inclusive [frm, to] filter on an ISO date (mirrors the ledgers router)."""
    iso = d.isoformat()
    if frm and iso < frm:
        return False
    if to and iso > to:
        return False
    return True


def _range_label(frm: str | None, to: str | None) -> str:
    if frm and to:
        return f"{frm} ～ {to}"
    if frm:
        return f"{frm} 起"
    if to:
        return f"至 {to}"
    return "全部期間"


def _sym_cell(symbol: str, name: str) -> str:
    return (
        f'<td class="l"><span class="sym-code">{_esc(symbol)}</span>'
        f'<span class="sym-name">{_esc(name)}</span></td>'
    )


def _side_chip(side_value: str) -> str:
    buy = side_value.upper() == "BUY"
    cls = "buy" if buy else "sell"
    label = "買" if buy else "賣"
    return f'<span class="chip {cls}">{label}</span>'


def _totals_line(label: str, totals: dict[str, Decimal]) -> str:
    """A per-currency sum line, e.g. 「淨額合計：1,234 USD　·　5,678 TWD」 (— when none)."""
    if not totals:
        return ""
    parts = "　·　".join(
        _amount_ccy(totals[ccy], ccy) for ccy in sorted(totals) if ccy
    )
    if not parts:
        return ""
    return f'<p class="note">{_esc(label)}：{parts}</p>'


def _add(totals: dict[str, Decimal], ccy: str, value: Decimal) -> None:
    totals[ccy] = totals.get(ccy, _ZERO) + value


def _empty_section(title: str) -> str:
    return f'<section><h2>{_esc(title)}</h2><p class="note">本區間無紀錄</p></section>'


def _section(title: str, count: int, head: str, rows: str, totals_lines: list[str]) -> str:
    notes = "".join(t for t in totals_lines if t)
    return (
        f"<section><h2>{_esc(title)}</h2>"
        f'<p class="note">共 {count} 筆</p>'
        f"<table><thead>{head}</thead><tbody>{rows}</tbody></table>"
        f"{notes}</section>"
    )


# --- sections -----------------------------------------------------------------------------


def _transactions_section(
    txs: list[StoredTransaction],
    accts: dict[str, str],
    names: dict[str, str],
    ccys: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    rows: list[str] = []
    net_totals: dict[str, Decimal] = {}
    count = 0
    for t in txs:
        if not _in_range(t.trade_date, frm, to):
            continue
        count += 1
        ccy = ccys.get(t.symbol, "")
        gross = t.quantity * t.price
        net = -(gross + t.fees + t.tax) if t.side.value == "BUY" else (gross - t.fees - t.tax)
        _add(net_totals, ccy, net)
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(t.trade_date.isoformat())}</td>'
            f'<td class="l">{_esc(accts.get(t.account_id, t.account_id))}</td>'
            f"{_sym_cell(t.symbol, names.get(t.symbol, ''))}"
            f'<td class="l">{_side_chip(t.side.value)}</td>'
            f'<td class="num">{_fmt_shares(t.quantity)}</td>'
            f'<td class="num">{_fmt_amount(t.price, ccy)}</td>'
            f'<td class="num">{_fmt_amount(t.fees, ccy)}</td>'
            f'<td class="num">{_fmt_amount(t.tax, ccy)}</td>'
            f'<td class="num">{_amount_ccy(net, ccy)}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("交易紀錄")
    head = (
        '<tr><th>日期</th><th class="l">帳戶</th><th class="l">代號 / 名稱</th>'
        '<th class="l">買賣</th><th>股數</th><th>價格</th><th>手續費</th>'
        "<th>交易稅</th><th>淨額</th></tr>"
    )
    return _section(
        "交易紀錄", count, head, "".join(rows), [_totals_line("淨現金流合計", net_totals)]
    )


def _dividends_section(
    divs: list[StoredDividend],
    accts: dict[str, str],
    names: dict[str, str],
    ccys: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    rows: list[str] = []
    net_totals: dict[str, Decimal] = {}
    count = 0
    for d in divs:
        if not _in_range(d.date, frm, to):
            continue
        count += 1
        ccy = ccys.get(d.symbol, "")
        _add(net_totals, ccy, d.net)
        if d.reinvest_shares is not None:
            reinvest = f"{_fmt_shares(d.reinvest_shares)} 股 @ {_fmt_amount(d.reinvest_price, ccy)}"
        else:
            reinvest = _NULL
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(d.date.isoformat())}</td>'
            f'<td class="l">{_esc(accts.get(d.account_id, d.account_id))}</td>'
            f"{_sym_cell(d.symbol, names.get(d.symbol, ''))}"
            f'<td class="l">{_esc(_DIV_TYPE_ZH.get(d.type.upper(), d.type))}</td>'
            f'<td class="num">{_fmt_amount(d.gross, ccy)}</td>'
            f'<td class="num">{_fmt_amount(d.withholding, ccy)}</td>'
            f'<td class="num">{_amount_ccy(d.net, ccy)}</td>'
            f'<td class="num">{_esc(reinvest)}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("股利紀錄")
    head = (
        '<tr><th>日期</th><th class="l">帳戶</th><th class="l">代號 / 名稱</th>'
        '<th class="l">類型</th><th>總額</th><th>預扣</th><th>淨額</th>'
        "<th>再投資</th></tr>"
    )
    return _section(
        "股利紀錄", count, head, "".join(rows), [_totals_line("淨額合計", net_totals)]
    )


def _fx_section(
    convs: list[StoredFxConversion],
    accts: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    rows: list[str] = []
    out_totals: dict[str, Decimal] = {}
    in_totals: dict[str, Decimal] = {}
    count = 0
    for c in convs:
        if not _in_range(c.date, frm, to):
            continue
        count += 1
        _add(out_totals, c.from_ccy.value, c.from_amount)
        _add(in_totals, c.to_ccy.value, c.to_amount)
        # QA-10: a conversion that received nothing has no implied rate. The whole cell is
        # 「—」 rather than 「1 USD = — TWD」, which reads as a missing digit in a real sentence.
        rate = _NULL if c.implied_rate is None else (
            f"1 {_esc(c.to_ccy.value)} = "
            f"{_fmt_amount(c.implied_rate, c.from_ccy.value)} {_esc(c.from_ccy.value)}")
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(c.date.isoformat())}</td>'
            f'<td class="l">{_esc(accts.get(c.account_id, c.account_id))}</td>'
            f'<td class="num">{_amount_ccy(c.from_amount, c.from_ccy.value)}</td>'
            f'<td class="num">{_amount_ccy(c.to_amount, c.to_ccy.value)}</td>'
            f'<td class="num">{rate}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("換匯紀錄")
    head = (
        '<tr><th>日期</th><th class="l">帳戶</th><th>換出</th><th>換入</th>'
        "<th>隱含匯率</th></tr>"
    )
    return _section(
        "換匯紀錄", count, head, "".join(rows),
        [_totals_line("換出合計", out_totals), _totals_line("換入合計", in_totals)],
    )


def _openings_section(
    openings: list[StoredOpening],
    accts: dict[str, str],
    names: dict[str, str],
    ccys: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    rows: list[str] = []
    cost_totals: dict[str, Decimal] = {}
    count = 0
    for o in openings:
        if not _in_range(o.build_date, frm, to):
            continue
        count += 1
        ccy = ccys.get(o.symbol, "")
        _add(cost_totals, ccy, o.original_cost_total)
        rows.append(
            "<tr>"
            f'<td class="l">{_esc(accts.get(o.account_id, o.account_id))}</td>'
            f"{_sym_cell(o.symbol, names.get(o.symbol, ''))}"
            f'<td class="num">{_fmt_shares(o.shares)}</td>'
            f'<td class="num">{_fmt_amount(o.original_avg, ccy)}</td>'
            f'<td class="num">{_amount_ccy(o.original_cost_total, ccy)}</td>'
            f'<td class="num">{_esc(o.build_date.isoformat())}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("期初庫存")
    head = (
        '<tr><th class="l">帳戶</th><th class="l">代號 / 名稱</th><th>股數</th>'
        "<th>原始均價</th><th>原始總成本</th><th>建檔日</th></tr>"
    )
    return _section(
        "期初庫存", count, head, "".join(rows), [_totals_line("原始成本合計", cost_totals)]
    )


def _actions_section(
    actions: list[StoredCorporateAction],
    accts: dict[str, str],
    names: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    """公司行動 — the one section with NO money column, and that is the point.

    A corporate action moves shares without moving cash (spec §2.1), so there is nothing to
    total: a 「合計」 line here would have to be zero or invented. What the reader needs
    instead is the ratio **as two terms**, exactly as stored — printing a quotient would put
    a rounded number on a page whose whole purpose is reconciliation, and the two-term form
    is what makes 7-for-1 legible as 7-for-1 rather than as 0.142857.

    Added 2026-08-16. Until then this report showed four ledgers out of six and its button
    called them 「四帳本＋期初庫存」 — five names for four sections, with the one ledger that
    explains a changed share count missing. A printed ledger that cannot explain why 100
    shares became 700 is a ledger that does not reconcile.
    """
    rows: list[str] = []
    count = 0
    for a in sorted(actions, key=lambda x: (x.date, x.id)):
        if not _in_range(a.date, frm, to):
            continue
        count += 1
        moves = (
            _sym_cell(a.from_symbol, names.get(a.from_symbol, ""))
            if a.from_symbol == a.to_symbol
            else (f'<td class="l">{_esc(a.from_symbol)} → {_esc(a.to_symbol)}</td>')
        )
        carry = (
            f"{_fmt_amount(a.cost_carry * Decimal(100), '')}%"
            if a.cost_carry is not None else _NULL
        )
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(a.date.isoformat())}</td>'
            f'<td class="l">{_esc(accts.get(a.account_id, a.account_id))}</td>'
            f'<td class="l">{_esc(KIND_ZH.get(a.kind.upper(), a.kind))}</td>'
            f"{moves}"
            f'<td class="num">{_fmt_shares(a.ratio_from)} → {_fmt_shares(a.ratio_to)}</td>'
            f'<td class="num">{_esc(carry)}</td>'
            f'<td class="l">{_esc(a.note or "")}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("公司行動")
    head = (
        '<tr><th>日期</th><th class="l">帳戶</th><th class="l">類型</th>'
        '<th class="l">代號 / 名稱</th><th>比例（原 → 新）</th>'
        "<th>成本分攤</th><th class=\"l\">備註</th></tr>"
    )
    return _section(
        "公司行動", count, head, "".join(rows),
        ["公司行動不移動現金，故本節無金額合計。"],
    )


def _cash_section(
    moves: list[StoredCashMovement],
    accts: dict[str, str],
    frm: str | None,
    to: str | None,
) -> str:
    """資金收支 — the sixth and last ledger, printable since 2026-08-16.

    **The sign comes from the kind, not from the stored amount** (`shared/cash_kinds.py`):
    amounts are stored unsigned, so a fee and a deposit are the same number until the kind
    is consulted. This section therefore prints the signed figure and totals it that way —
    printing seven unsigned rows under one 「金額」 column would show money leaving the
    account as money arriving, which is the one thing a statement must not do.

    ``acq_home_amount`` gets its own column rather than a derived rate: F1 forbids storing a
    rate, and deriving one for display here would be a second place that decides how to
    round it.
    """
    rows: list[str] = []
    totals: dict[str, Decimal] = {}
    count = 0
    for m in sorted(moves, key=lambda x: (x.date, x.id)):
        if not _in_range(m.date, frm, to):
            continue
        count += 1
        ccy = m.ccy.value
        signed = m.amount * movement_sign(m.kind)
        _add(totals, ccy, signed)
        acq = (_fmt_amount(m.acq_home_amount, "") if m.acq_home_amount is not None
               else _NULL)
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(m.date.isoformat())}</td>'
            f'<td class="l">{_esc(accts.get(m.account_id, m.account_id))}</td>'
            f'<td class="l">{_esc(CASH_KIND_ZH.get(m.kind.upper(), m.kind))}</td>'
            f'<td class="num">{_amount_ccy(signed, ccy)}</td>'
            f'<td class="num">{_esc(acq)}</td>'
            f'<td class="l">{_esc(m.note or "")}</td>'
            "</tr>"
        )
    if not rows:
        return _empty_section("資金收支")
    head = (
        '<tr><th>日期</th><th class="l">帳戶</th><th class="l">類型</th>'
        '<th>金額</th><th>取得成本（家幣）</th><th class="l">備註</th></tr>'
    )
    return _section(
        "資金收支", count, head, "".join(rows), [_totals_line("淨額合計", totals)]
    )


def _header_html(now: datetime, frm: str | None, to: str | None) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")
    nature = "本報告直接呈現帳本原始輸入值；統計數字皆由這些紀錄重算，不由本報告寫入。"
    meta = [
        f"生成時間 {_esc(gen)}",
        f"期間 {_esc(_range_label(frm, to))}",
        _version_line(),
    ]
    return _page_header(title="帳本報告", meta_lines=meta, nature=nature)


def build_ledgers_report_html(
    conn: sqlite3.Connection, *, now: datetime, frm: str | None, to: str | None
) -> ExportArtifact:
    """Build the print-optimized 帳本報告 for [frm, to] (read-only; no writes)."""
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    insts = list_instruments(conn)
    names = {i.symbol: i.name for i in insts}
    ccys = {i.symbol: i.quote_ccy.value for i in insts}

    body = "\n".join(
        [
            _header_html(now, frm, to),
            _transactions_section(list_transactions(conn), accts, names, ccys, frm, to),
            _dividends_section(list_dividends(conn), accts, names, ccys, frm, to),
            _fx_section(list_fx_conversions(conn), accts, frm, to),
            _openings_section(list_opening(conn), accts, names, ccys, frm, to),
            _actions_section(list_corporate_actions(conn), accts, names, frm, to),
            _cash_section(list_cash_movements(conn), accts, frm, to),
            _page_footer("帳本為 append-only：更正以新紀錄沖銷，原紀錄永久保留。"),
        ]
    )
    filename = f"ledger-report-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8", _document("帳本報告", body).encode("utf-8")
    )
