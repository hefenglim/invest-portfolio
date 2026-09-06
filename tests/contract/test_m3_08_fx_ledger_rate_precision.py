"""M3-08 (frontend side) — the 換匯 ledgers' 隱含匯率 columns format at a FIXED 4 dp.

``fmt.rate()`` picks 4 dp below 10 and 2 dp above, which printed ONE column in two
precisions (「4.6000」 beside 「28.00」) and, for a TWD-per-USD rate, rounded a ledger figure
like money. Owner ruling (b), 2026-09-06: only the two 換匯紀錄 columns change, through a
dedicated ``fmt.rateExact(v, 4)``; ``rate()`` itself and the dashboard's 換匯損益 card
(``web/app.js``) keep their 2-dp convention.

The 對帳單's fx_in / fx_out description line (``web/cash.js``) prints the SAME
conversion-implied figure — M5-01 made its sentence ``renderFxLedger()``'s verbatim so the
number would read the same on both surfaces — so it follows the ledger column, else the
statement becomes the third convention M5-01 was written to prevent.

Structural pins (the same style as test_r6_charts_no_float_rounding): the value is a wire
string and the browser renders it, so what a contract test can pin is WHICH formatter each
column goes through. The 4-dp arithmetic itself is measured in the real browser by
``tests/e2e/test_format_exact_rounding.py``.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"


def _lines_with(path: Path, needle: str) -> list[str]:
    """Lines that FORMAT `needle` — pass it to a `f.<helper>(` call; a null-guard such as
    `if (r.fx_rate != null)` is not a rendering and is left out."""
    pat = re.compile(r"f\.\w+\(" + re.escape(needle))
    return [line for line in path.read_text(encoding="utf-8").splitlines() if pat.search(line)]


def test_format_js_exports_rate_exact() -> None:
    text = (_WEB / "format.js").read_text(encoding="utf-8")
    assert "function rateExact(" in text
    assert "rateExact," in text.split("return {", 1)[1], "rateExact must be exported on window.fmt"


def test_ledger_fx_column_uses_rate_exact() -> None:
    lines = _lines_with(_WEB / "ledger.js", "x.implied_rate")
    assert lines, "ledger.js must render x.implied_rate"
    assert all("f.rateExact(x.implied_rate, 4)" in line for line in lines), lines


def test_cash_fx_ledger_column_uses_rate_exact() -> None:
    lines = _lines_with(_WEB / "cash.js", "x.implied_rate")
    assert lines, "cash.js must render x.implied_rate"
    assert all("f.rateExact(x.implied_rate, 4)" in line for line in lines), lines


def test_cash_statement_fx_line_follows_the_ledger_column() -> None:
    lines = _lines_with(_WEB / "cash.js", "r.fx_rate")
    assert len(lines) == 2, f"both statement branches must render r.fx_rate: {lines}"
    assert all("f.rateExact(r.fx_rate, 4)" in line for line in lines), lines


def test_rate_itself_and_the_dashboard_card_are_unchanged() -> None:
    """The ruling's other half: `rate()` keeps its magnitude switch, and app.js still uses it."""
    fmt = (_WEB / "format.js").read_text(encoding="utf-8")
    assert "return num(v, Number(v) < 10 ? 4 : 2);" in fmt
    app = (_WEB / "app.js").read_text(encoding="utf-8")
    assert "f.rate(" in app and "rateExact" not in app
