"""Annual tax-package export (spec 02): realized gains, dividends, FX realized, summary.

Year-cut by trade date (sell date / dividend date / conversion date). Per-currency
rows are never summed across currencies. Reporting conversion uses trade-date FX.
"""

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_fx_conversions,
    load_ledger_bundle,
)
from portfolio_dash.export.artifact import ExportArtifact, csv_blob, zip_artifact
from portfolio_dash.forex.fx_pnl import realized_fx_rows_as_of
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.pricing.store import get_fx_on
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import FXConversion

_ONE = Decimal("1")
_ZERO = Decimal("0")
# ⚠ TWO realized columns, and the FILING one comes first (review 2026-08-24).
# ``realized_adjusted`` (= proceeds − adjusted_cost_removed) is the PERFORMANCE figure the
# dashboard shows: the locked 2026-06-06 accounting model folds cash dividends into cost, so
# that number already has the dividends netted out of it — while the dividends sheet in this
# same package declares those same dividends as income. Subtotalling it taxes one sum twice.
# ``realized_original`` (= proceeds − original_cost_removed, what was actually paid) is the
# capital gain a filer declares, and it is what ``summary.md`` subtotals. The adjusted column
# stays so the package can still be reconciled against the dashboard line for line.
_REALIZED_COLS = ["sell_date", "account_id", "symbol", "quote_ccy", "shares_sold",
                  "proceeds_net", "original_cost_removed", "adjusted_cost_removed",
                  "realized_original", "realized_adjusted",
                  "rate_used", "reporting_realized_original"]
_DIV_COLS = ["date", "account_id", "symbol", "type", "gross", "withholding", "net", "ccy"]
_FX_COLS = ["date", "account_id", "home_ccy", "foreign_ccy", "foreign_sold",
            "home_received", "rate_used", "realized"]


def _rate_on(conn: sqlite3.Connection, d: date, base: Currency,
             quote: Currency) -> Decimal | None:
    """Trade-date base->quote rate via direct lookup, then inverse fallback. None if absent."""
    if base == quote:
        return _ONE
    direct = get_fx_on(conn, base, quote, on=d)
    if direct is not None:
        return direct.rate
    inverse = get_fx_on(conn, quote, base, on=d)
    if inverse is not None:
        return _ONE / inverse.rate
    return None


def build_tax_package_zip(
    conn: sqlite3.Connection, *, now: datetime, year: int, reporting: Currency
) -> ExportArtifact:
    """Build the annual tax package zip (realized gains + dividends + FX realized + summary).

    ``now`` is accepted for signature parity with sibling exports (audit logging is the
    router's concern); the package content is year-cut, not as-of ``now``.
    """
    bundle = load_ledger_bundle(conn)
    divs = bundle.dividends
    instruments = bundle.instruments
    convs = [FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                          from_amount=s.from_amount, to_ccy=s.to_ccy,
                          to_amount=s.to_amount) for s in list_fx_conversions(conn)]
    moves = list_cash_movements(conn)
    accounts = {a.account_id: a for a in list_accounts(conn)}
    book = build_book(bundle)

    realized_rows: list[list[str]] = []
    realized_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    adjusted_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for r in book.realized.rows:
        if r.sell_date.year != year:
            continue
        # CAPITAL GAINS ONLY. A post-close cash dividend also rides in `realized.rows`
        # (kind="dividend", audit H2) so it reaches 總報酬, but for tax it is INCOME and is
        # already reported on the dividends sheet below, straight from the dividend ledger.
        # Without this filter the same payout would appear on both sheets.
        # A short COVER (2026-07-31) is a capital gain/loss like any sale and belongs here;
        # it has no dividend-ledger counterpart, so excluding it would simply lose it.
        if r.kind not in ("sale", "short_cover"):
            continue
        rate = _rate_on(conn, r.sell_date, r.quote_ccy, reporting)
        # A short cover has no dividend history, so original == adjusted and the two realized
        # columns coincide — the split only separates them on a long that took dividends.
        realized_original = r.proceeds_net - r.original_cost_removed
        reporting_realized = "" if rate is None else str(realized_original * rate)
        realized_rows.append([
            r.sell_date.isoformat(), r.account_id, r.symbol, r.quote_ccy.value,
            str(r.shares_sold), str(r.proceeds_net), str(r.original_cost_removed),
            str(r.adjusted_cost_removed), str(realized_original), str(r.realized),
            "" if rate is None else str(rate), reporting_realized,
        ])
        realized_subtotal[r.quote_ccy] += realized_original
        adjusted_subtotal[r.quote_ccy] += r.realized

    div_rows: list[list[str]] = []
    div_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for d in divs:
        if d.date.year != year or d.type is DividendType.STOCK:
            continue
        ccy = instruments[d.symbol].quote_ccy
        div_rows.append([
            d.date.isoformat(), d.account_id, d.symbol, d.type.value.lower(),
            str(d.gross), str(d.withholding), str(d.net), ccy.value,
        ])
        div_subtotal[ccy] += d.net

    fx_rows: list[list[str]] = []
    fx_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for acct in accounts.values():
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        home, foreign = acct.funding_ccy, acct.settlement_ccy
        acct_convs = [c for c in convs if c.account_id == acct.account_id]
        # Same weighted average the dashboard uses — foreign cash movements that carry a
        # home cost are part of the basis (spec 2026-07-30). Reading a different average
        # here would make the tax package disagree with 換匯損益 on the same reconversion.
        #
        # ⚠ That is not a hypothetical: it HAPPENED. When QA-02 date-bounded the dashboard's
        # realized-FX rate (manual §8.2, 「回換前 avg_rate」) this call site kept the ALL-TIME
        # average, and the two surfaces reported 2,500 vs 10,000 for one 2026-02-20
        # reconversion — the exact disagreement the paragraph above forbids in writing.
        # ``realized_fx_rows_as_of`` is now the ONE entry point, so a future divergence would
        # have to be written on purpose rather than left behind by an unshared argument.
        acct_moves = [m for m in moves if m.account_id == acct.account_id]
        for fr in realized_fx_rows_as_of(acct_convs, acct_moves, home, foreign):
            if fr.date.year != year:
                continue
            fx_rows.append([fr.date.isoformat(), acct.account_id, fr.home_ccy.value,
                            fr.foreign_ccy.value, str(fr.foreign_sold),
                            str(fr.home_received), str(fr.rate_used), str(fr.realized)])
            fx_subtotal[fr.home_ccy] += fr.realized

    files: dict[str, bytes] = {
        f"realized_gains_{year}.csv": csv_blob(_REALIZED_COLS, realized_rows),
        f"dividends_{year}.csv": csv_blob(_DIV_COLS, div_rows),
        f"fx_realized_{year}.csv": csv_blob(_FX_COLS, fx_rows),
        "summary.md": _summary_md(year, realized_subtotal, div_subtotal, fx_subtotal,
                                  adjusted_subtotal),
    }
    return zip_artifact(f"tax_package_{year}.zip", files)


def _subtotal_lines(subtotal: dict[Currency, Decimal]) -> str:
    if not subtotal:
        return "- （無）\n"
    return "".join(
        f"- {ccy.value}: {amt}\n"
        for ccy, amt in sorted(subtotal.items(), key=lambda kv: kv[0].value)
    )


def _summary_md(year: int, realized: dict[Currency, Decimal],
                dividends: dict[Currency, Decimal],
                fx: dict[Currency, Decimal],
                adjusted: dict[Currency, Decimal]) -> bytes:
    """The filing subtotal is the ORIGINAL-basis one; the adjusted basis rides along, labelled.

    Both figures are true, of different questions. Printing only the adjusted one (as this
    package did until 2026-08-24) hands a filer a capital gain that already has the year's
    dividends netted out of it, next to a dividends sheet declaring those same dividends.
    """
    md = (
        f"# Tax Package {year}\n\n"
        "Per-currency subtotals (never summed across currencies).\n\n"
        "申報用的已實現損益是「原始成本基礎」那一欄。\n\n"
        f"## Realized gains — 申報用（原始成本基礎 original_cost）\n"
        f"{_subtotal_lines(realized)}\n"
        f"## Dividends (net)\n{_subtotal_lines(dividends)}\n"
        f"## Realized FX P&L\n{_subtotal_lines(fx)}\n"
        "## 對帳用：績效基礎已實現（調整後成本，股利已折抵）\n"
        "⚠ 非申報數字。此欄的股利已折抵進成本，而上方股利表已就同一筆股利申報一次；"
        "兩者相加會把同一筆錢課兩次。列出僅供與儀表板核對。\n"
        f"{_subtotal_lines(adjusted)}"
    )
    return md.encode("utf-8")
