"""持倉報告 — a print-optimized, self-contained HTML holdings report.

Builds ONE offline HTML document from ``portfolio.dashboard.build_dashboard`` — the SAME
computed numbers the dashboard renders (KPIs, per-holding cost basis / market value /
weight, sector + currency allocation). This builder computes NO new money: it FORMATS
already-computed ``Decimal`` values (thousands separators, per-currency minor unit) and the
only derived figures are simple ``Decimal`` sums for TOTAL rows plus a per-holding display
return ratio (unrealized ÷ adjusted cost, guarded against non-positive cost). Every dynamic
string (symbol / name / account / currency / sector) is escaped; the stylesheet is inline
(zero external assets) so the file opens offline and prints cleanly (A4, page-break-safe).

Document sections: header (nature statement + version) → KPI 摘要 → 持倉明細表 (one row per
account×symbol, weight desc, TOTAL row) → 配置 (sector + currency allocation) → footer.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.report_html import (
    _NULL,
    _amount_ccy,
    _document,
    _esc,
    _fmt_amount,
    _fmt_pct,
    _fmt_shares,
    _page_footer,
    _page_header,
    _version_line,
)
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData, HoldingRow
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert

_ZERO = Decimal("0")


# --- header / footer ----------------------------------------------------------------------


def _header_html(now: datetime, reporting_ccy: str, total_mv: Decimal | None) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")  # minute precision, generation wall-clock
    nature = "數字以生成當下之市價與匯率計算；市價或匯率不可得時該欄以「—」標示，不臆測。"
    meta = [
        f"生成時間 {_esc(gen)}",
        f"報告幣別 {_esc(reporting_ccy)}　·　投資組合總市值 "
        f"{_amount_ccy(total_mv, reporting_ccy)}",
        _version_line(),
    ]
    return _page_header(title="持倉報告", meta_lines=meta, nature=nature)


def _footer_html() -> str:
    note = (
        "調整後平均成本已反映股利沖減（台／馬現金股利）；報酬率＝未實現損益 ÷ 調整後成本，"
        "供參考。所有統計由帳本重算，非本報告寫入。"
    )
    return _page_footer(note)


# --- KPI 摘要 -----------------------------------------------------------------------------


def _kpi_html(data: DashboardData, reporting_ccy: str) -> str:
    """A compact KPI grid — only the figures the dashboard payload actually provides."""
    k = data.kpis
    cards: list[tuple[str, str]] = []
    if k.total_market_value is not None:
        cards.append(("總市值", _amount_ccy(k.total_market_value, reporting_ccy)))
    if k.total_return is not None:
        rate = _fmt_pct(k.total_return_rate) if k.total_return_rate is not None else _NULL
        cards.append(("總報酬", f"{_amount_ccy(k.total_return, reporting_ccy)}（{rate}）"))
    if k.xirr is not None:
        cards.append(("年化 XIRR", _fmt_pct(k.xirr)))
    if k.unrealized_total is not None:
        cards.append(("未實現損益", _amount_ccy(k.unrealized_total, reporting_ccy)))
    if k.realized_total is not None:
        cards.append(("已實現損益", _amount_ccy(k.realized_total, reporting_ccy)))
    if not cards:
        return '<section><h2>KPI 摘要</h2><p class="note">目前無可用的績效指標。</p></section>'
    grid = "".join(
        f'<div class="kv"><span class="k">{_esc(label)}</span>'
        f'<span class="v num">{value}</span></div>'
        for label, value in cards
    )
    return f'<section><h2>KPI 摘要</h2><div class="sum-grid">{grid}</div></section>'


# --- 持倉明細表 ---------------------------------------------------------------------------


def _reporting_value(
    h: HoldingRow, resolver: RateResolver, reporting: Currency
) -> Decimal | None:
    """The holding's market value in the reporting currency, or None (missing price/FX)."""
    if h.market_value is None:
        return None
    try:
        return convert(h.market_value, resolver.rate(h.quote_ccy, reporting))
    except KeyError:
        return None  # missing FX -> blank, never fabricated


def _return_ratio(h: HoldingRow) -> Decimal | None:
    """Per-holding display return = unrealized ÷ adjusted cost. Guarded: a non-positive
    adjusted cost (high-yield payback) has no meaningful rate -> None (renders 「—」)."""
    if h.unrealized_pnl is None or h.adjusted_cost_total <= _ZERO:
        return None
    return h.unrealized_pnl / h.adjusted_cost_total


def _holdings_table_html(
    data: DashboardData, resolver: RateResolver, reporting: Currency, reporting_ccy: str
) -> str:
    holdings = sorted(
        data.holdings, key=lambda h: (h.weight if h.weight is not None else _ZERO), reverse=True
    )
    if not holdings:
        return '<section><h2>持倉明細表</h2><p class="note">目前無持倉。</p></section>'

    body: list[str] = []
    total_reporting = _ZERO
    total_weight = _ZERO
    for h in holdings:
        rep_val = _reporting_value(h, resolver, reporting)
        if rep_val is not None:
            total_reporting += rep_val
        if h.weight is not None:
            total_weight += h.weight
        sym_cell = (
            f'<span class="sym-code">{_esc(h.symbol)}</span>'
            f'<span class="sym-name">{_esc(h.name)}</span>'
        )
        body.append(
            "<tr>"
            f'<td class="l">{sym_cell}</td>'
            f'<td class="l">{_esc(h.account_name)}</td>'
            f'<td class="num">{_fmt_shares(h.shares)}</td>'
            f'<td class="num">{_fmt_amount(h.market_price, h.quote_ccy.value)}</td>'
            f'<td class="num">{_fmt_amount(rep_val, reporting_ccy)}</td>'
            f'<td class="num">{_fmt_pct(h.weight)}</td>'
            f'<td class="num">{_fmt_amount(h.adjusted_avg, h.quote_ccy.value)}</td>'
            f'<td class="num">{_amount_ccy(h.unrealized_pnl, h.quote_ccy.value)}</td>'
            f'<td class="num">{_fmt_pct(_return_ratio(h))}</td>'
            "</tr>"
        )
    head = (
        '<tr><th class="l">代號 / 名稱</th><th class="l">帳戶</th><th>股數</th>'
        "<th>現價（原幣）</th><th>市值（報告幣）</th><th>權重</th>"
        "<th>調整後均價（原幣）</th><th>未實現損益（原幣）</th><th>報酬率</th></tr>"
    )
    total_row = (
        '<tr class="total">'
        f'<td class="l" colspan="4">合計（{_esc(reporting_ccy)}）</td>'
        f'<td class="num">{_fmt_amount(total_reporting, reporting_ccy)}</td>'
        f'<td class="num">{_fmt_pct(total_weight)}</td>'
        '<td></td><td></td><td></td></tr>'
    )
    return (
        "<section><h2>持倉明細表</h2>"
        f"<table><thead>{head}</thead>"
        f"<tbody>{''.join(body)}{total_row}</tbody></table>"
        '<p class="note">市值（報告幣）依生成當下匯率換算；缺價或缺匯率的欄位以「—」標示，'
        "並排除於合計之外。</p></section>"
    )


# --- 配置 ---------------------------------------------------------------------------------


def _allocation_html(data: DashboardData, reporting_ccy: str) -> str:
    """Sector allocation table + currency allocation table (each only if the payload has it)."""
    blocks: list[str] = []

    alloc = data.allocation
    if alloc is not None and alloc.by_sector:
        weights = alloc.weights
        rows = sorted(
            alloc.by_sector.items(),
            key=lambda kv: weights.get(kv[0], _ZERO),
            reverse=True,
        )
        body = "".join(
            "<tr>"
            f'<td class="l">{_esc(sector)}</td>'
            f'<td class="num">{_fmt_amount(value, reporting_ccy)}</td>'
            f'<td class="num">{_fmt_pct(weights.get(sector))}</td>'
            "</tr>"
            for sector, value in rows
        )
        head = (
            '<tr><th class="l">產業</th>'
            f"<th>市值（{_esc(reporting_ccy)}）</th><th>權重</th></tr>"
        )
        blocks.append(
            "<h3 style=\"font-size:13px;margin:14px 0 4px\">產業配置</h3>"
            f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"
        )

    view = data.currency_view
    if view is not None and view.by_currency_value:
        body = "".join(
            "<tr>"
            f'<td class="l">{_esc(ccy.value)}</td>'
            f'<td class="num">{_fmt_amount(value, reporting_ccy)}</td>'
            "</tr>"
            for ccy, value in view.by_currency_value.items()
        )
        head = (
            '<tr><th class="l">幣別</th>'
            f"<th>市值（{_esc(reporting_ccy)}）</th></tr>"
        )
        blocks.append(
            "<h3 style=\"font-size:13px;margin:16px 0 4px\">幣別配置</h3>"
            f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"
        )

    if not blocks:
        return ('<section><h2>配置</h2>'
                '<p class="note">目前無可用的配置資料（缺匯率）。</p></section>')
    return f"<section><h2>配置</h2>{''.join(blocks)}</section>"


def build_holdings_report_html(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> ExportArtifact:
    """Build the print-optimized 持倉報告 from the current dashboard snapshot (no writes)."""
    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    reporting_ccy = reporting.value

    body = "\n".join(
        [
            _header_html(now, reporting_ccy, data.kpis.total_market_value),
            _kpi_html(data, reporting_ccy),
            _holdings_table_html(data, resolver, reporting, reporting_ccy),
            _allocation_html(data, reporting_ccy),
            _footer_html(),
        ]
    )
    filename = f"holdings-report-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8", _document("持倉報告", body).encode("utf-8")
    )
