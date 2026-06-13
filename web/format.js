/* portfolio-dash — formatting helpers.
   ALL number/date formatting lives here so the later Jinja2/backend
   integration can swap these server-side cleanly. */
window.fmt = (function () {
  const CCY_DP = { TWD: 0, USD: 2, MYR: 2 };
  const NULL_GLYPH = '\u2014'; // em-dash
  const MINUS = '\u2212';      // U+2212 minus sign

  function isNil(v) { return v === null || v === undefined; }

  /** Plain number with thousands separators. */
  function num(v, dp) {
    if (isNil(v)) return NULL_GLYPH;
    dp = dp === undefined ? 0 : dp;
    return Number(v).toLocaleString('en-US', {
      minimumFractionDigits: dp, maximumFractionDigits: dp
    });
  }

  /** Amount in a given currency: TWD 0 dp, USD/MYR 2 dp. */
  function money(v, ccy) {
    if (isNil(v)) return NULL_GLYPH;
    const dp = CCY_DP[ccy] !== undefined ? CCY_DP[ccy] : 0;
    return num(v, dp);
  }

  /** Per-share price: MY quotes need 3 dp, others 2 dp. */
  function price(v, ccy) {
    if (isNil(v)) return NULL_GLYPH;
    return num(v, ccy === 'MYR' ? 3 : 2);
  }

  /** Signed amount: explicit + for gains, U+2212 for losses. */
  function signed(v, ccy) {
    if (isNil(v)) return NULL_GLYPH;
    const body = money(Math.abs(v), ccy);
    if (v > 0) return '+' + body;
    if (v < 0) return MINUS + body;
    return body;
  }

  /** Signed plain number with fixed dp (for rates/deltas). */
  function signedNum(v, dp) {
    if (isNil(v)) return NULL_GLYPH;
    const body = num(Math.abs(v), dp);
    if (v > 0) return '+' + body;
    if (v < 0) return MINUS + body;
    return body;
  }

  /** Ratio -> percentage with 2 dp: 0.2147 -> "21.47%". */
  function pct(v) {
    if (isNil(v)) return NULL_GLYPH;
    return (v * 100).toFixed(2) + '%';
  }

  /** Signed percentage. */
  function signedPct(v) {
    if (isNil(v)) return NULL_GLYPH;
    const body = (Math.abs(v) * 100).toFixed(2) + '%';
    if (v > 0) return '+' + body;
    if (v < 0) return MINUS + body;
    return body;
  }

  /** FX rate, 2–4 dp depending on magnitude. */
  function rate(v) {
    if (isNil(v)) return NULL_GLYPH;
    return num(v, v < 10 ? 4 : 2);
  }

  /** ISO date/datetime -> YYYY-MM-DD. */
  function date(iso) {
    if (isNil(iso)) return NULL_GLYPH;
    return String(iso).slice(0, 10);
  }

  /** ISO datetime -> YYYY-MM-DD HH:mm (string is already Asia/Taipei). */
  function datetime(iso) {
    if (isNil(iso)) return NULL_GLYPH;
    const s = String(iso);
    return s.slice(0, 10) + ' ' + s.slice(11, 16);
  }

  /** CSS class for P&L sign — Taiwan convention: red = gain, green = loss. */
  function signClass(v) {
    if (isNil(v)) return 'sign-nil';
    if (v > 0) return 'sign-up';
    if (v < 0) return 'sign-down';
    return 'sign-flat';
  }

  return { num, money, price, signed, signedNum, pct, signedPct, rate, date, datetime, signClass, NULL_GLYPH };
})();
