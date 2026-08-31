"""持倉報告 — a print-optimized, self-contained HTML holdings report.

Builds ONE offline HTML document from ``portfolio.dashboard.build_dashboard`` — the SAME
computed numbers the dashboard renders (KPIs, per-holding cost basis / market value /
weight, sector + currency allocation). This builder computes NO new money: it FORMATS
already-computed ``Decimal`` values (thousands separators, per-currency minor unit) and the
only derived figures are simple ``Decimal`` sums for TOTAL rows. The per-holding 報酬率 is
the server's ``HoldingRow.unrealized_pct`` — printed, not re-derived (QA-04): this layer
READS computed results (``architecture.md``), and one app must not hold two definitions of
報酬率. Every dynamic string (symbol / name / account / currency / sector) is escaped; the
stylesheet is inline (zero external assets) so the file opens offline and prints cleanly
(A4, page-break-safe).

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
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    HoldingRow,
    HoldingSubtotal,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert

_ZERO = Decimal("0")

# zh-TW market labels for the filter statement (matches web/app.js MARKET_ZH).
_MARKET_ZH = {Market.TW: "台股", Market.US: "美股", Market.MY: "馬股"}

# AI-D41's name for ``KpiSummary.total_return`` — the SAME string web/app.js renders, so the
# printed report and the screen cannot call one number two things.
_TOTAL_RETURN_LABEL = "資產損益（不含本金匯率）"

# This report prints figure A only. B (含匯兌總損益) and the residual terms are dashboard
# surfaces; saying so here is what stops a reader reconstructing a "total" by adding the
# 換匯損益 card to A, which double-counts the cross term (MV − C)(spot − acq).
_FIGURE_A_NOTE = (
    "「資產損益（不含本金匯率）」為各幣別已實現＋未實現損益依今日即期匯率換算之合計，"
    "匯率只作用於損益、未及於本金；本節僅列此一數字，不含本金匯率效果，"
    "亦不可與換匯損益相加（會重複計算）。完整的含匯兌總損益請見儀表板。"
)


def _matches(h: HoldingRow, account: str | None, market: Market | None) -> bool:
    """The holdings-filter predicate — account / market chips, each independent; None on an
    axis means "all" there (so no filter == every row). Mirrors export.holdings._matches."""
    return (account is None or h.account_id == account) and (
        market is None or h.market == market
    )


def _find_subtotal(
    data: DashboardData, account: str | None, market: Market | None
) -> HoldingSubtotal | None:
    """The server-computed reporting-currency subtotal cell for this (account, market)
    filter, or None if the payload emitted no such cell (a combo with no holdings)."""
    for s in data.holdings_subtotals:
        if s.account_id == account and s.market == market:
            return s
    return None


def _filter_label(
    data: DashboardData, account: str | None, market: Market | None
) -> str | None:
    """A human 篩選 statement for the active filter, or None when nothing is filtered.

    The account display name is read from the already-enriched holding rows (no new
    lookup); an account with no held rows falls back to its id."""
    if account is None and market is None:
        return None
    acct_names = {h.account_id: h.account_name for h in data.holdings}
    acct_txt = "全部" if account is None else acct_names.get(account, account)
    mkt_txt = "全部" if market is None else _MARKET_ZH.get(market, market.value)
    return f"篩選　帳戶 {acct_txt}　·　市場 {mkt_txt}"


# --- header / footer ----------------------------------------------------------------------


def _header_html(
    now: datetime, reporting_ccy: str, total_mv: Decimal | None, filter_label: str | None
) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")  # minute precision, generation wall-clock
    nature = "數字以生成當下之市價與匯率計算；市價或匯率不可得時該欄以「—」標示，不臆測。"
    # When a filter is active the total-value line reports the FILTERED subtotal (server
    # computed) and the filter is stated on its own meta line, so the header never claims a
    # grand figure while the table below shows a filtered subset.
    total_label = "篩選後市值" if filter_label else "投資組合總市值"
    meta = [
        f"生成時間 {_esc(gen)}",
        f"報告幣別 {_esc(reporting_ccy)}　·　{total_label} "
        f"{_amount_ccy(total_mv, reporting_ccy)}",
    ]
    if filter_label:
        meta.append(_esc(filter_label))
    meta.append(_version_line())
    return _page_header(title="持倉報告", meta_lines=meta, nature=nature)


def _footer_html() -> str:
    # The 報酬率 denominator stated here must be the one the column actually uses: the
    # server's ``unrealized_pct`` divides by the ORIGINAL invested cost (audit H1), the same
    # basis as 累計報酬率 and 回本進度, so every percentage in the app shares ONE basis.
    note = (
        "調整後平均成本已反映股利沖減（台／馬現金股利）；報酬率＝未實現損益 ÷ 原始投入成本"
        "（與儀表板同一定義），供參考。所有統計由帳本重算，非本報告寫入。"
    )
    return _page_footer(note)


# --- KPI 摘要 -----------------------------------------------------------------------------


def _kpi_html(data: DashboardData, reporting_ccy: str, *, filtered: bool = False) -> str:
    """A compact KPI grid — only the figures the dashboard payload actually provides.

    The KPIs (總報酬 / XIRR / 已實現 / 未實現) are whole-portfolio, money-weighted figures
    that do not decompose per filter, so when a filter is active the heading is annotated
    「（全組合）」 to make clear this section is NOT filtered (only the 持倉明細表 below is)."""
    heading = "KPI 摘要（全組合）" if filtered else "KPI 摘要"
    k = data.kpis
    cards: list[tuple[str, str]] = []
    if k.total_market_value is not None:
        cards.append(("總市值", _amount_ccy(k.total_market_value, reporting_ccy)))
    if k.total_return is not None:
        rate = _fmt_pct(k.total_return_rate) if k.total_return_rate is not None else _NULL
        # AI-D41 (owner ruling 2026-08-24): ``total_return`` is figure A — each currency's
        # native P&L at today's spot — so the rate reaches the GAIN and never the PRINCIPAL.
        # 「總報酬」 read as one grand total and invited the reader to add the 換匯損益 card
        # to it, which double-counts the cross term. The honest name says what it excludes.
        cards.append((_TOTAL_RETURN_LABEL,
                      f"{_amount_ccy(k.total_return, reporting_ccy)}（{rate}）"))
    if k.xirr is not None:
        cards.append(("年化 XIRR", _fmt_pct(k.xirr)))
    if k.unrealized_total is not None:
        cards.append(("未實現損益", _amount_ccy(k.unrealized_total, reporting_ccy)))
    if k.realized_total is not None:
        cards.append(("已實現損益", _amount_ccy(k.realized_total, reporting_ccy)))
    if not cards:
        return (f'<section><h2>{_esc(heading)}</h2>'
                '<p class="note">目前無可用的績效指標。</p></section>')
    grid = "".join(
        f'<div class="kv"><span class="k">{_esc(label)}</span>'
        f'<span class="v num">{value}</span></div>'
        for label, value in cards
    )
    note = f'<p class="note">{_esc(_FIGURE_A_NOTE)}</p>' if k.total_return is not None else ""
    return (f'<section><h2>{_esc(heading)}</h2>'
            f'<div class="sum-grid">{grid}</div>{note}</section>')


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


def _holdings_table_html(
    data: DashboardData,
    resolver: RateResolver,
    reporting: Currency,
    reporting_ccy: str,
    *,
    account: str | None = None,
    market: Market | None = None,
) -> str:
    # Filter the row SET to the active (account, market) chips; the TOTAL row below sums the
    # filtered rows' per-holding reporting values, so it reflects the filter automatically
    # (and equals the server-side holdings_subtotals cell for the same combo).
    filtered = [h for h in data.holdings if _matches(h, account, market)]
    holdings = sorted(
        filtered, key=lambda h: (h.weight if h.weight is not None else _ZERO), reverse=True
    )
    if not holdings:
        return '<section><h2>持倉明細表</h2><p class="note">目前無持倉。</p></section>'

    body: list[str] = []
    total_reporting = _ZERO
    total_weight = _ZERO
    # QA-11: a total of NOTHING is unknown, not zero. ``total_weight`` starts at 0 and only
    # non-None weights are added, so when the reporting blend cannot be formed at all (a
    # currency with no rate anywhere -> every ``h.weight`` is None) the 合計 cell printed a
    # confident 0.00% directly beneath a column of honest 「—」 — contradicting both the
    # header's nature statement and this table's own footnote. Tracked, not inferred from
    # ``total_weight == 0``: a genuinely zero-weight portfolio would then read 「—」 too.
    any_weight = False
    for h in holdings:
        rep_val = _reporting_value(h, resolver, reporting)
        if rep_val is not None:
            total_reporting += rep_val
        if h.weight is not None:
            total_weight += h.weight
            any_weight = True
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
            # 報酬率: the SERVER's per-holding rate (unrealized ÷ abs(ORIGINAL cost), audit
            # H1). Never re-derived here — a second definition drifts from the dashboard the
            # moment dividends move the adjusted total (QA-04: 139.52% on screen printed as
            # 1,370.59%), and abs() is what keeps a short's negative basis from flipping the
            # sign. `None` (unpriced / no original cost) renders 「—」 exactly as before.
            f'<td class="num">{_fmt_pct(h.unrealized_pct)}</td>'
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
        f'<td class="num">{_fmt_pct(total_weight if any_weight else None)}</td>'
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


def _allocation_html(data: DashboardData, reporting_ccy: str, *, filtered: bool = False) -> str:
    """Sector allocation table + currency allocation table (each only if the payload has it).

    Allocation is a whole-portfolio breakdown (it is not recomputed per filter), so when a
    filter is active the heading is annotated 「（全組合）」 for the same honesty reason as
    the KPI section."""
    heading = "配置（全組合）" if filtered else "配置"
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
    if view is not None and (view.by_currency_reporting or view.by_currency_value):
        # QA-03: ``by_currency_value`` is NATIVE — ``portfolio/allocation.py`` accumulates each
        # holding's market value in its OWN quote currency — so printing it under a
        # 「市值（報告幣）」 header states 12,000 USD as 12,000 TWD, while the 產業配置 table a
        # few lines above prints genuinely converted values under the IDENTICAL header. The
        # two tables then disagree with each other and with the report's own total (measured:
        # rows summing to 612,000 beneath a header reading 996,000). ``results.py`` says of the
        # sibling field "anything that ranks or weights currencies must read THIS field", and
        # ``strategy/alerts.py::currency_weights`` already obeys it.
        #
        # ``by_currency_reporting`` is additive with an empty default, so a ``CombinedView``
        # constructed before it existed carries only the native leg. Dropping the section would
        # lose it; printing native amounts under a reporting header IS the defect. So they are
        # printed under a 「市值（原幣）」 header, each in its OWN currency's minor unit, with a
        # caption saying the blend could not be formed — a native number is never labelled as a
        # converted one, the same red line as "a stale price is labelled, never guessed".
        converted = bool(view.by_currency_reporting)
        items = view.by_currency_reporting if converted else view.by_currency_value
        body = "".join(
            "<tr>"
            f'<td class="l">{_esc(ccy.value)}</td>'
            "<td class=\"num\">"
            f"{_fmt_amount(value, reporting_ccy if converted else ccy.value)}</td>"
            "</tr>"
            for ccy, value in items.items()
        )
        column = _esc(reporting_ccy) if converted else "原幣"
        head = f'<tr><th class="l">幣別</th><th>市值（{column}）</th></tr>'
        note = (
            "幣別配置之市值依生成當下匯率換算為報告幣別，合計等於上方總市值。"
            if converted
            else "缺匯率，無法換算為報告幣別；本表為各幣別之原幣金額，不可跨幣別相加。"
        )
        blocks.append(
            "<h3 style=\"font-size:13px;margin:16px 0 4px\">幣別配置</h3>"
            f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"
            f'<p class="note">{_esc(note)}</p>'
        )

    if not blocks:
        return (f'<section><h2>{_esc(heading)}</h2>'
                '<p class="note">目前無可用的配置資料（缺匯率）。</p></section>')
    return f"<section><h2>{_esc(heading)}</h2>{''.join(blocks)}</section>"


def build_holdings_report_html(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    account: str | None = None,
    market: Market | None = None,
) -> ExportArtifact:
    """Build the print-optimized 持倉報告 from the current dashboard snapshot (no writes).

    Optional (account, market) filter: the 持倉明細表 (rows + TOTAL) and the header total
    follow the active dashboard chips; the whole-portfolio KPI / 配置 sections stay grand
    and are annotated 「（全組合）」 so the report is never internally misleading."""
    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    reporting_ccy = reporting.value

    filter_label = _filter_label(data, account, market)
    filtered = filter_label is not None
    # Header total: the server-computed subtotal cell when filtered, else the grand KPI.
    if filtered:
        cell = _find_subtotal(data, account, market)
        header_total = cell.total_market_value if cell is not None else _ZERO
    else:
        header_total = data.kpis.total_market_value

    body = "\n".join(
        [
            _header_html(now, reporting_ccy, header_total, filter_label),
            _kpi_html(data, reporting_ccy, filtered=filtered),
            _holdings_table_html(data, resolver, reporting, reporting_ccy,
                                 account=account, market=market),
            _allocation_html(data, reporting_ccy, filtered=filtered),
            _footer_html(),
        ]
    )
    filename = f"holdings-report-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8", _document("持倉報告", body).encode("utf-8")
    )
