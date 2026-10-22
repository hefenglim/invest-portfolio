"""R6 / QA-27: the chart layer must not round money through a float.

``web/format.js`` rounds on the DIGIT STRING (``_round`` / ``_inc`` / ``_x100``) precisely so
a value that crossed the wire as an exact ``Decimal`` string is not pushed through IEEE-754
on its way to the screen. ``charts.js``'s rebased-index tooltip used ``Number(...).toFixed(2)``
instead, and the two disagreed on boundary values — ``100.005`` renders ``'100.00'`` via
``toFixed`` and ``'100.01'`` via ``fmt.num`` — so the same figure read differently depending
on which surface the owner happened to look at. ``toFixed`` also cannot suppress a
rounded-away ``-0.00``, so a delta of ``-0.001`` printed a minus sign in front of a zero.

Asserted on the SOURCE rather than in a browser because there is no JS unit runner here, and
because the property worth pinning is structural: this file formats through ``fmt``, full
stop. ``web/app.js:451`` is deliberately NOT covered — its ``toFixed(1)`` builds SVG polyline
coordinates, which are geometry, not money.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"

#: A `.toFixed(` call, ignoring the prose mentions in comments that WARN against it.
_TOFIXED = re.compile(r"\.toFixed\s*\(")
_PROSE = re.compile(r"(NOT|NEVER|never|not)\s+`?\.toFixed")


def _code_lines(name: str) -> list[tuple[int, str]]:
    text = (_WEB / name).read_text(encoding="utf-8")
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if _TOFIXED.search(line) and not _PROSE.search(line)]


def test_charts_js_formats_through_fmt_not_tofixed() -> None:
    offenders = _code_lines("charts.js")
    assert offenders == [], (
        "web/charts.js must format via window.fmt, which rounds on the digit string; "
        f"float rounding found at: {offenders}")


def test_the_rebased_tooltip_reads_the_wire_string_not_a_float() -> None:
    """The positive half: it is not enough that ``toFixed`` is gone.

    ``f.num(Number(s.value), 2)`` would also pass the test above while still round-tripping
    through a float. The value handed to ``fmt`` must be ``s.value`` itself.
    """
    text = (_WEB / "charts.js").read_text(encoding="utf-8")
    assert "f.num(s.value, 2)" in text, "the index must be formatted from the wire string"
    assert "f.signedNum(delta, 2)" in text, "the delta must share fmt's rounding and -0 rule"


def test_the_trend_card_formats_in_the_series_reporting_currency() -> None:
    """2026-10-22: the trend card (its tooltip and the 含現金 header) formatted every figure
    with a hard-coded ``'TWD'`` — 0 dp — so a USD-reporting ledger lost its cents and the
    server's 2-dp ``pnl`` was rounded a second time (12.497 → ``12.50`` → ``+13``). The card
    formats with the series' own ``reporting_currency``; no money call in this file names a
    currency literal."""
    text = (_WEB / "charts.js").read_text(encoding="utf-8")
    literal = re.findall(r"f\.(?:money|signed|num|price)\([^()]*'(?:TWD|USD|MYR)'\)", text)
    assert literal == [], literal
    assert "const ccy = t.reporting_currency;" in text
    for call in ("f.money(pt.total_value, ccy)", "f.money(pt.net_invested, ccy)",
                 "f.money(pt.net_worth, ccy)", "f.signed(pt.pnl, ccy)",
                 "f.money(curNw, ccy)"):
        assert call in text, call
