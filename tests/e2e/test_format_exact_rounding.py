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
    # --- share COUNTS: 6 dp, trailing zeros trimmed (owner ruling 2026-08-06) -----
    # `num` defaults to 0 dp, which silently erased fractional DRIP/STOCK reinvest shares
    # (`net / price`, an unrounded quotient) everywhere shares were displayed.
    ("shares", "95", None, "95"),                     # ordinary whole-share ledger stays clean
    ("shares", "1200", None, "1,200"),                # thousands separator survives the trim
    ("shares", "10", None, "10"),                     # the no-dot guard: NOT "1"
    ("shares", "100", None, "100"),
    ("shares", "0", None, "0"),
    ("shares", "1000.000000", None, "1,000"),         # trailing zeros trimmed, group kept
    ("shares", "0.5", None, "0.5"),                   # a plain half share
    ("shares", "0.045000", None, "0.045"),            # inner zeros kept, trailing trimmed
    ("shares", "-25", None, "-25"),                   # an open declared short is negative
    # the exact demo-site DRIP quotients that rendered as "0" at the old 0 dp
    ("shares", "0.01988201878960017478697837011", None, "0.019882"),
    ("shares", "95.04571227709218320061723668", None, "95.045712"),
    # below the 6-dp cut-off the whole value legitimately reads as zero
    ("shares", "0.0000001", None, "0"),
    # --- FX rates at a FIXED precision (M3-08, 2026-09-06) -----------------------
    # `rate()` switches 4 dp / 2 dp on magnitude, so one 隱含匯率 column showed 4.6000 beside
    # 28.00 — and 28.00 is a rate rounded like money. `rateExact` is the 換匯 ledgers' column
    # format: 4 dp whatever the magnitude; `rate()` itself is unchanged (the dashboard's FX
    # card keeps 2 dp by owner ruling).
    ("rateExact", "27.99998642424900642472415771", 4, "28.0000"),
    ("rateExact", "4.6", 4, "4.6000"),
    ("rateExact", "4.5632", 4, "4.5632"),
    ("rateExact", "32", 4, "32.0000"),
    ("rateExact", "0.03125", 4, "0.0313"),         # half-up on the digit string, not float
    ("rate", "27.99998642424900642472415771", None, "28.00"),   # the dashboard card: unchanged
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
