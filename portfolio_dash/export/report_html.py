"""Shared scaffolding for print-optimized, self-contained HTML report builders.

Extracted from ``export.rebalance_report`` so the sibling print reports — 持倉報告
(:mod:`export.holdings_report`), 帳本報告 (:mod:`export.ledgers_report`) and the
再平衡試算執行報告 (:mod:`export.rebalance_report`) — share ONE stylesheet, one document
skeleton, one number-formatting vocabulary, and one header/footer shape.

These helpers only FORMAT already-computed ``Decimal`` values (thousands separators,
per-currency minor unit) and ESCAPE every dynamic string — they compute no money. The
CSS is inline (zero external assets/fonts/scripts), print-first (white bg, near-black
text, A4 page box, page-break-safe sections/rows, grayscale-safe exact-ink chips) so a
report opens offline and prints cleanly.
"""

import html
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash import __version__

# Per-currency minor unit (matches web/format.js CCY_DP): TWD whole NT$, USD/MYR 2 dp.
_CCY_DP = {"TWD": 0, "USD": 2, "MYR": 2}
_NULL = "—"  # em dash


# --- formatting (numbers of record already computed; this only presents them) -------------


def _esc(value: object) -> str:
    """html.escape() any dynamic string — XSS discipline for every interpolated value."""
    return html.escape(str(value))


def _fmt_amount(value: Decimal | None, ccy: str) -> str:
    """A money amount in *ccy*'s minor unit with thousands separators (— when absent)."""
    if value is None:
        return _NULL
    dp = _CCY_DP.get(ccy, 2)
    q = value.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    return f"{q:,.{dp}f}"


def _fmt_shares(value: Decimal | None) -> str:
    """An integer share count with thousands separators (— when absent)."""
    if value is None:
        return _NULL
    q = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{q:,.0f}"


def _fmt_pct(value: Decimal | None) -> str:
    """A ratio as a 2-dp percentage (— when absent)."""
    if value is None:
        return _NULL
    return f"{value * 100:,.2f}%"


def _amount_ccy(value: Decimal | None, ccy: str) -> str:
    """`<amount> <CCY>` with the currency code escaped."""
    return f"{_fmt_amount(value, ccy)} {_esc(ccy)}"


# --- header / footer (parameterized; shared shape) ----------------------------------------


def _version_line() -> str:
    """The `版本 portfolio-dash vX.Y.Z` meta line (version escaped)."""
    return f"版本 portfolio-dash v{_esc(__version__)}"


def _page_header(*, title: str, meta_lines: Sequence[str], nature: str | None = None) -> str:
    """`<header>`: an h1 title, one `<p class="meta">` per pre-built meta line, and an
    optional nature note. *meta_lines* are trusted HTML fragments (numbers + already-escaped
    dynamic parts, e.g. via :func:`_amount_ccy`); *nature* is plain text and is escaped here.
    """
    parts = ["<header>", f"<h1>{_esc(title)}</h1>"]
    parts.extend(f'<p class="meta">{line}</p>' for line in meta_lines)
    if nature:
        parts.append(f'<p class="nature">{_esc(nature)}</p>')
    parts.append("</header>")
    return "".join(parts)


def _page_footer(note: str) -> str:
    """`<footer>`: a plain-text note (escaped) plus the version production stamp."""
    return (
        f'<footer><p>{_esc(note)}</p>'
        f"<p>portfolio-dash v{_esc(__version__)} 產出</p></footer>"
    )


# All CSS inline (self-contained). Print-first: white bg, near-black text, zh-TW font
# fallbacks, A4 page box, page-break-safe sections/rows, exact-ink direction chips.
_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #ffffff; color: #16181d;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei",
    "Noto Sans TC", "PingFang TC", "Heiti TC", sans-serif;
  font-size: 13px; line-height: 1.5;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.wrap { max-width: 800px; margin: 0 auto; padding: 24px 20px 48px; }
h1 { font-size: 20px; margin: 0 0 6px; }
h2 {
  font-size: 15px; margin: 26px 0 8px; padding-bottom: 4px;
  border-bottom: 2px solid #16181d;
}
.meta { color: #3a3f47; font-size: 12px; margin: 2px 0; }
.nature {
  margin: 12px 0 0; padding: 8px 10px; background: #f4f5f7;
  border-left: 3px solid #8a9099; font-size: 12px; color: #3a3f47;
}
table { width: 100%; border-collapse: collapse; margin: 0; }
th, td {
  border-bottom: 1px solid #d7dae0; padding: 6px 8px; text-align: right;
  vertical-align: top;
}
th {
  border-bottom: 2px solid #16181d; font-size: 12px; color: #3a3f47;
  white-space: nowrap;
}
th.l, td.l { text-align: left; }
.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.sym-code { font-weight: 700; }
.sym-name { color: #5a6069; margin-left: 6px; font-size: 12px; }
.cons { color: #5a6069; font-size: 11px; margin-top: 3px; }
.leg-line { white-space: nowrap; padding: 1px 0; }
.chip {
  display: inline-block; border: 1px solid; border-radius: 3px; padding: 0 5px;
  font-size: 11px; font-weight: 700; line-height: 1.6;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.chip.buy { border-color: #b3261e; color: #b3261e; }
.chip.sell { border-color: #1e6b2f; color: #1e6b2f; }
.oddlot { color: #7a5a00; font-size: 11px; margin-left: 4px; }
.check { font-size: 15px; margin-right: 2px; }
.acct-sec { margin-top: 14px; }
.acct-name { font-weight: 700; font-size: 13px; margin: 0 0 4px; }
tr.subtotal td { border-top: 2px solid #16181d; border-bottom: none; font-weight: 700; }
tr.total td { border-top: 2px solid #16181d; border-bottom: none; font-weight: 700; }
.sum-grid { display: flex; flex-wrap: wrap; gap: 10px 28px; margin: 4px 0 0; }
.kv { display: flex; flex-direction: column; }
.kv .k { color: #5a6069; font-size: 11px; }
.kv .v { font-size: 15px; font-weight: 700; }
.warn { color: #b3261e; font-weight: 700; margin: 10px 0 0; }
.note { color: #5a6069; font-size: 12px; margin: 6px 0 0; }
footer {
  margin-top: 32px; padding-top: 10px; border-top: 1px solid #d7dae0;
  color: #5a6069; font-size: 11px;
}
footer p { margin: 2px 0; }
@page { size: A4; margin: 18mm; }
@media print {
  .wrap { max-width: none; padding: 0; }
  section, .acct-sec { page-break-inside: avoid; }
  tr { page-break-inside: avoid; }
  thead { display: table-header-group; }
  h2 { page-break-after: avoid; }
}
"""


def _document(title: str, body: str) -> str:
    """Wrap *body* in a complete, self-contained HTML document with the shared stylesheet."""
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        f'<div class="wrap">\n{body}\n</div>\n'
        "</body>\n</html>\n"
    )
