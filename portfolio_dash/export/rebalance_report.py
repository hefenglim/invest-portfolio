"""再平衡試算執行報告 — a print-optimized, self-contained HTML execution guide.

Builds ONE offline HTML document from the CURRENT rebalance preview: the SAME numbers of
record as ``POST /api/rebalance/preview`` (``strategy.rebalance.compute_rebalance``) plus the
dashboard total market value + instrument names (``portfolio.dashboard.build_dashboard``).

This builder computes NO new money — it only FORMATS already-computed ``Decimal`` values
(thousands separators, per-currency minor unit). Every dynamic string (symbol / name /
account_name / currency) is ``html.escape()``'d. ALL CSS is inline in a ``<style>`` block;
there are zero external assets/fonts/scripts, so the file opens offline and prints cleanly
(A4, page-break-safe sections, grayscale-safe direction chips).

Document sections: header (nature statement + version) → 摘要表（依標的）→ 執行清單（依帳戶）
→ 彙總 → footer. Empty/on-target previews still render a valid document with a
「目前無需任何交易」notice.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.report_html import (
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
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.strategy.rebalance import compute_rebalance

_ZERO = Decimal("0")
_ONE = Decimal("1")


# --- formatting: shared vocabulary lives in export.report_html (imported above). This
# module only adds the rebalance-specific direction glyphs / classes below. ----------------


def _side_glyph(side: str) -> str:
    """Direction label with a filled glyph so it survives grayscale printing."""
    return "▲ 買" if side == "buy" else "▼ 賣"


def _side_class(side: str) -> str:
    return "buy" if side == "buy" else "sell"


# --- typed views over the (dict[str, object]) compute_rebalance output ---------------------


@dataclass(frozen=True)
class _LegView:
    """One executing trade against a concrete account (fees bound to that account)."""

    account_name: str
    side: str
    shares: Decimal
    amount: Decimal
    fee: Decimal
    tax: Decimal
    odd_lot: bool
    ccy: str  # inherited from the parent row (legs share the symbol's quote ccy)
    symbol: str
    name: str


@dataclass(frozen=True)
class _RowView:
    """One preview row (a symbol's combined cross-account plan)."""

    symbol: str
    name: str
    ccy: str
    amount: Decimal
    fee: Decimal
    tax: Decimal
    current_weight: Decimal
    target_weight: Decimal
    new_weight: Decimal
    constituents: list[tuple[str, Decimal]]  # (account_name, shares), most-shares first
    legs: list[_LegView]


def _parse_rows(
    raw_rows: list[dict[str, object]], name_by_symbol: dict[str, str]
) -> list[_RowView]:
    """Cast the loosely-typed preview rows into typed views (names filled from the book)."""
    out: list[_RowView] = []
    for raw in raw_rows:
        symbol = cast(str, raw["symbol"])
        ccy = cast(str, raw["ccy"])
        name = name_by_symbol.get(symbol, "")  # missing dashboard row -> blank, never crash
        legs = [
            _LegView(
                account_name=cast(str, lg["account_name"]),
                side=cast(str, lg["side"]),
                shares=cast(Decimal, lg["shares"]),
                amount=cast(Decimal, lg["amount"]),
                fee=cast(Decimal, lg["fee"]),
                tax=cast(Decimal, lg["tax"]),
                odd_lot=cast(bool, lg["odd_lot"]),
                ccy=ccy,
                symbol=symbol,
                name=name,
            )
            for lg in cast(list[dict[str, object]], raw["legs"])
        ]
        constituents = [
            (cast(str, a["account_name"]), cast(Decimal, a["shares"]))
            for a in cast(list[dict[str, object]], raw["accounts"])
        ]
        out.append(
            _RowView(
                symbol=symbol,
                name=name,
                ccy=ccy,
                amount=cast(Decimal, raw["amount"]),
                fee=cast(Decimal, raw["fee"]),
                tax=cast(Decimal, raw["tax"]),
                current_weight=cast(Decimal, raw["current_weight"]),
                target_weight=cast(Decimal, raw["target_weight"]),
                new_weight=cast(Decimal, raw["new_weight"]),
                constituents=constituents,
                legs=legs,
            )
        )
    return out


# --- HTML sections ------------------------------------------------------------------------


def _header_html(now: datetime, reporting_ccy: str, total_mv: Decimal | None) -> str:
    gen = now.strftime("%Y-%m-%d %H:%M")  # minute precision, generation wall-clock
    nature = (
        "本報告為試算結果，不寫入帳本；股數、金額與費稅以生成當下之市價與匯率計算，"
        "實際成交將隨市場變動。"
    )
    meta = [
        f"生成時間 {_esc(gen)}",
        f"報告幣別 {_esc(reporting_ccy)}　·　投資組合總市值 "
        f"{_amount_ccy(total_mv, reporting_ccy)}",
        _version_line(),
    ]
    return _page_header(title="再平衡試算執行指南", meta_lines=meta, nature=nature)


def _leg_action_html(leg: _LegView) -> str:
    """One 動作 line for the summary table: chip + shares + account (＋ 零股 hint)."""
    chip = f'<span class="chip {_side_class(leg.side)}">{_side_glyph(leg.side)}</span>'
    odd = ' <span class="oddlot">（零股）</span>' if leg.odd_lot else ""
    return (
        f'<div class="leg-line">{chip} {_fmt_shares(leg.shares)} 股 @ '
        f"{_esc(leg.account_name)}{odd}</div>"
    )


def _empty_section(title: str) -> str:
    return f'<section><h2>{_esc(title)}</h2><p class="note">目前無需任何交易。</p></section>'


def _summary_table_html(rows: list[_RowView]) -> str:
    """摘要表（依標的）— mirrors the drawer: one row per symbol."""
    if not rows:
        return _empty_section("摘要表（依標的）")
    body: list[str] = []
    for r in rows:
        sym_cell = (
            f'<span class="sym-code">{_esc(r.symbol)}</span>'
            f'<span class="sym-name">{_esc(r.name)}</span>'
        )
        if len(r.constituents) > 1:  # multi-account symbol: list constituents underneath
            cons = "、".join(
                f"{_esc(an)} {_fmt_shares(sh)}股" for an, sh in r.constituents
            )
            sym_cell += f'<div class="cons">{cons}</div>'
        action = "".join(_leg_action_html(lg) for lg in r.legs)
        body.append(
            "<tr>"
            f'<td class="l">{sym_cell}</td>'
            f'<td class="num">{_fmt_pct(r.current_weight)}</td>'
            f'<td class="num">{_fmt_pct(r.target_weight)}</td>'
            f'<td class="l">{action}</td>'
            f'<td class="num">{_amount_ccy(r.amount, r.ccy)}</td>'
            f'<td class="num">{_fmt_amount(r.fee + r.tax, r.ccy)}</td>'
            f'<td class="num">{_fmt_pct(r.new_weight)}</td>'
            "</tr>"
        )
    head = (
        '<tr><th class="l">代號 / 名稱</th><th>現權重</th><th>目標 %</th>'
        '<th class="l">動作</th><th>預估金額（原幣）</th><th>費稅（原幣）</th>'
        "<th>試算後權重</th></tr>"
    )
    return (
        "<section><h2>摘要表（依標的）</h2>"
        f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"
        "</section>"
    )


def _execution_html(rows: list[_RowView]) -> str:
    """執行清單（依帳戶）— the execution-guide core: legs grouped by account, checklist rows."""
    if not rows:
        return _empty_section("執行清單（依帳戶）")
    by_account: dict[str, list[_LegView]] = {}
    for r in rows:
        for lg in r.legs:
            by_account.setdefault(lg.account_name, []).append(lg)

    head = (
        '<tr><th class="l">動作</th><th class="l">標的</th>'
        "<th>預估金額</th><th>費稅</th></tr>"
    )
    sections: list[str] = []
    for acct_name in sorted(by_account):  # accounts ordered by name
        legs = by_account[acct_name]
        leg_rows: list[str] = []
        subtotal: dict[str, Decimal] = {}
        for lg in legs:
            subtotal[lg.ccy] = subtotal.get(lg.ccy, _ZERO) + lg.amount
            odd = "（零股）" if lg.odd_lot else ""
            chip = f'<span class="chip {_side_class(lg.side)}">{_side_glyph(lg.side)}</span>'
            leg_rows.append(
                "<tr>"
                f'<td class="l"><span class="check">☐</span> {chip} '
                f"{_fmt_shares(lg.shares)} 股{odd}</td>"
                f'<td class="l"><span class="sym-code">{_esc(lg.symbol)}</span>'
                f'<span class="sym-name">{_esc(lg.name)}</span></td>'
                f'<td class="num">{_amount_ccy(lg.amount, lg.ccy)}</td>'
                f'<td class="num">{_fmt_amount(lg.fee + lg.tax, lg.ccy)}</td>'
                "</tr>"
            )
        for ccy in sorted(subtotal):  # per-account subtotal per currency
            leg_rows.append(
                '<tr class="subtotal">'
                '<td class="l" colspan="2">小計</td>'
                f'<td class="num">{_amount_ccy(subtotal[ccy], ccy)}</td>'
                "<td></td></tr>"
            )
        sections.append(
            f'<div class="acct-sec"><p class="acct-name">{_esc(acct_name)}</p>'
            f"<table><thead>{head}</thead><tbody>{''.join(leg_rows)}</tbody></table></div>"
        )
    return f"<section><h2>執行清單（依帳戶）</h2>{''.join(sections)}</section>"


def _totals_html(
    *,
    sum_target: Decimal,
    cash_level: Decimal,
    turnover: Decimal,
    total_fees: Decimal,
    reporting_ccy: str,
    over_allocated: bool,
    excluded: list[str],
    excluded_with_target: list[str],
    rebate_estimate_total: Decimal | None,
) -> str:
    """彙總 — target sum / cash level / turnover / fees, plus over-alloc + excluded notes."""
    kv = [
        ("目標合計", _fmt_pct(sum_target)),
        ("現金水位", _fmt_pct(cash_level)),
        ("預估周轉額", _amount_ccy(turnover, reporting_ccy)),
        ("預估總費稅", _amount_ccy(total_fees, reporting_ccy)),
    ]
    grid = "".join(
        f'<div class="kv"><span class="k">{_esc(k)}</span>'
        f'<span class="v num">{v}</span></div>'
        for k, v in kv
    )
    notes: list[str] = []
    # FE-D1 forecast HINT (informational, 不計入成本): TW charge-first rebate on next month's
    # refund. Reporting-ccy amount; only rendered when a TW leg actually rebates.
    if rebate_estimate_total is not None and rebate_estimate_total > _ZERO:
        notes.append(
            '<p class="note">預估次月折讓合計 '
            f"{_amount_ccy(rebate_estimate_total, reporting_ccy)}"
            "（台股先收後退，<b>不計入成本</b>，僅供參考）。</p>"
        )
    if over_allocated:
        notes.append('<p class="warn">⚠ 目標合計超過 100% — 請下調部分標的。</p>')
    if excluded:
        notes.append(f'<p class="note">缺價排除：{_esc("、".join(excluded))}</p>')
    if excluded_with_target:
        notes.append(
            '<p class="note">已設目標但未參與試算（未持有或缺價）：'
            f'{_esc("、".join(excluded_with_target))}</p>'
        )
    return (
        f'<section><h2>彙總</h2><div class="sum-grid">{grid}</div>{"".join(notes)}</section>'
    )


def _footer_html() -> str:
    note = (
        "費稅依各帳戶費率規則計算；股數以整數股為單位，馬股 100 股一手；"
        "缺價標的排除。試算不寫入帳本。"
    )
    return _page_footer(note)


def build_rebalance_report_html(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    targets: dict[str, Decimal],
) -> ExportArtifact:
    """Build the print-optimized 再平衡試算執行報告 for the current preview (no writes)."""
    data = build_dashboard(conn, now=now, reporting=reporting)
    result = compute_rebalance(conn, now=now, reporting=reporting, targets=targets)

    name_by_symbol = {h.symbol: h.name for h in data.holdings}
    rows = _parse_rows(cast(list[dict[str, object]], result["rows"]), name_by_symbol)
    summary = cast(dict[str, object], result["summary"])
    reporting_ccy = reporting.value

    # 目標合計 = Σ submitted targets (matches the drawer). 現金水位 = 1 − Σ, floored at 0.
    sum_target = _ZERO
    for ratio in targets.values():
        sum_target += ratio
    cash_level = _ONE - sum_target
    if cash_level < _ZERO:
        cash_level = _ZERO

    body = "\n".join(
        [
            _header_html(now, reporting_ccy, data.kpis.total_market_value),
            _summary_table_html(rows),
            _execution_html(rows),
            _totals_html(
                sum_target=sum_target,
                cash_level=cash_level,
                turnover=cast(Decimal, summary["turnover_reporting"]),
                total_fees=cast(Decimal, summary["total_fees_reporting"]),
                reporting_ccy=reporting_ccy,
                over_allocated=cast(bool, summary["over_allocated"]),
                excluded=cast(list[str], summary["excluded"]),
                excluded_with_target=cast(list[str], summary["excluded_with_target"]),
                rebate_estimate_total=cast(
                    "Decimal | None", summary.get("rebate_estimate_total")),
            ),
            _footer_html(),
        ]
    )
    filename = f"rebalance-plan-{now.strftime('%Y%m%d-%H%M')}.html"
    return ExportArtifact(
        filename, "text/html; charset=utf-8",
        _document("再平衡試算執行指南", body).encode("utf-8"),
    )
