"""E2E: web/format.js formats Decimal STRINGS exactly (audit L5, 2026-07-26).

Two properties, both load-bearing for a money UI:

1. **Identity on pre-quantized values.** Fees and taxes are quantized by the fee engine with
   the ACCOUNT'S rounding mode — TW fee/證交稅 are 無條件捨去 to integer NT$ (財政部 FE-D3;
   群益 142.5 -> 142), US/MY components are ROUND_HALF_UP to the cent. The display layer must
   reproduce those byte-for-byte and never re-round them under a different mode.

2. **Agreement with `Decimal.quantize(ROUND_HALF_UP)`** on values that are NOT pre-quantized
   (unrealized P&L, market value, ratios). The old float path disagreed at the .xx5 boundary:
   `0.145` is stored as 0.1449999999999999900…, so `toLocaleString` gave 0.14 where the
   backend gives 0.15.

The cases run in the REAL browser against the REAL served `format.js`.
"""

import pytest
from playwright.sync_api import Page

# (helper, value, arg, expected)
_CASES: list[tuple[str, str, object, str]] = [
    # --- property 1: already-quantized values survive untouched -------------------
    ("money", "142", "TWD", "142"),            # TW fee floored by the engine (142.5 -> 142)
    ("money", "20", "TWD", "20"),              # TW min fee
    ("money", "5", "TWD", "5"),                # floor(5.5) -> 5 before the min-fee floor
    ("money", "0.07", "USD", "0.07"),          # US SEC component, cent-quantized
    ("money", "9.79", "USD", "9.79"),          # TAF cap
    ("money", "3.00", "MYR", "3.00"),          # MY platform fee
    ("money", "1000", "MYR", "1,000.00"),      # MY stamp cap — MYR carries 2 dp
    # --- property 2: half-up agreement with the backend --------------------------
    ("money", "0.145", "USD", "0.15"),         # float path gave 0.14
    ("money", "2.675", "USD", "2.68"),         # classic float-representation trap
    ("money", "1.005", "USD", "1.01"),
    ("money", "9.995", "USD", "10.00"),        # carry across the integer boundary
    ("money", "142.5", "TWD", "143"),          # a NON-fee TWD amount rounds half-up
    ("money", "-0.4", "TWD", "0"),             # rounded-away negative is not "-0"
    ("money", "-1234.6", "TWD", "-1,235"),
    # --- precision beyond float's 15-17 significant digits -----------------------
    ("money", "3799829.0000", "TWD", "3,799,829"),
    ("num", "0.1960784313725490196078431373", 4, "0.1961"),
    # --- percentages: exact point shift, not a float multiply --------------------
    ("pct", "0.2147", None, "21.47%"),
    ("pct", "0.12345", None, "12.35%"),        # float path gave 12.34%
    ("pct", "1", None, "100.00%"),
    ("signedPct", "-0.019607", None, "−1.96%"),
    ("signedPct", "0.1960784313725490196078431373", None, "+19.61%"),
    # --- signed money keeps the exact magnitude ----------------------------------
    ("signed", "223473.0000", "TWD", "+223,473"),
    ("signed", "-15973", "TWD", "−15,973"),
    ("signed", "0", "TWD", "0"),
    # --- prices: MY needs 3 dp ---------------------------------------------------
    ("price", "10.005", "MYR", "10.005"),
    ("price", "600.0855", "TWD", "600.09"),
]


@pytest.mark.e2e
def test_format_js_is_exact(live_server: str, browser_page: Page) -> None:
    page = browser_page
    page.goto(live_server + "/index.html", wait_until="load")
    page.wait_for_function("() => !!window.fmt")

    failures: list[str] = []
    for fn, value, arg, expected in _CASES:
        got = page.evaluate(
            "([fn, v, a]) => (a === null ? window.fmt[fn](v) : window.fmt[fn](v, a))",
            [fn, value, arg],
        )
        if got != expected:
            failures.append(f"fmt.{fn}({value!r}, {arg!r}) -> {got!r}, expected {expected!r}")
    assert not failures, "exact-formatting mismatches:\n  " + "\n  ".join(failures)


@pytest.mark.e2e
def test_format_js_sign_classes(live_server: str, browser_page: Page) -> None:
    """signClass must read the SIGN off the string, including values float would flatten."""
    page = browser_page
    page.goto(live_server + "/index.html", wait_until="load")
    page.wait_for_function("() => !!window.fmt")
    cases = [
        ("0.0000000000000000000000000001", "sign-up"),
        ("-0.0000000000000000000000000001", "sign-down"),
        ("0", "sign-flat"),
        ("0.0000", "sign-flat"),
        (None, "sign-nil"),
    ]
    for value, expected in cases:
        got = page.evaluate("(v) => window.fmt.signClass(v)", value)
        assert got == expected, f"signClass({value!r}) -> {got!r}, expected {expected!r}"
