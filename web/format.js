/* portfolio-dash — formatting helpers.
   ALL number/date formatting lives here so the later Jinja2/backend
   integration can swap these server-side cleanly.

   EXACT DECIMAL FORMATTING (audit L5, 2026-07-26)
   -----------------------------------------------
   Every money value arrives from the API as a canonical Decimal STRING. These helpers used
   to render it with `Number(v).toLocaleString(...)`, i.e. they round in binary float. That
   is wrong in two ways that matter here:

   1. A value already quantized by the backend must survive display BYTE-FOR-BYTE. Fees and
      taxes are quantized by the fee engine using the ACCOUNT'S OWN rounding mode — TW fee
      and 證交稅 are 無條件捨去 (ROUND_DOWN to integer NT$, 財政部 FE-D3: 群益 142.5 -> 142),
      while US/MY components are ROUND_HALF_UP to the cent. The display layer must never
      re-round those with a mode of its own; formatting at the same scale must be identity.
      With exact string arithmetic it provably is.
   2. For values that are NOT pre-quantized (unrealized P&L, market value, ratios), the
      rounding must match what the backend would do — `Decimal.quantize(ROUND_HALF_UP)`.
      Float rounding disagrees at the .xx5 boundary (`0.145` is stored as
      0.1449999999999999900…, so float yields 0.14 where Decimal yields 0.15).

   So the arithmetic below is done on the DIGIT STRING: split on the point, round half-up by
   inspecting the first dropped digit, carry with a string increment. No float is involved
   anywhere in the money path. `Number()` survives only as a fallback for a non-plain-decimal
   input (scientific notation), which the wire never carries.

   NOTE ON THE FLOOR RULE: this module deliberately does NOT implement 無條件捨去. Floor is a
   property of the TW fee/tax ENGINE (`data_ingestion/fees.py`, driven by each rule set's
   `rounding` field), not of presentation — those values reach the frontend already floored
   to integer NT$, and formatting them at TWD's 0 dp reproduces them exactly. A display-side
   floor would be wrong for every OTHER TWD amount (proceeds, P&L, market value), which the
   backend quantizes ROUND_HALF_UP. */
window.fmt = (function () {
  const CCY_DP = { TWD: 0, USD: 2, MYR: 2 };
  const NULL_GLYPH = '—'; // em-dash
  const MINUS = '−';      // U+2212 minus sign
  const PLAIN_DECIMAL = /^[+-]?(\d+(\.\d*)?|\.\d+)$/;

  /* null/undefined only — deliberately NOT '' , so this refactor changes no caller's
     behaviour (an empty string still falls through to the numeric fallback as before). */
  function isNil(v) { return v === null || v === undefined; }

  /** Increment a pure digit string by one, carrying left ("199" -> "200"). */
  function _inc(digits) {
    let i = digits.length - 1;
    const out = digits.split('');
    while (i >= 0) {
      if (out[i] === '9') { out[i] = '0'; i -= 1; } else { out[i] = String(+out[i] + 1); break; }
    }
    return i < 0 ? '1' + out.join('') : out.join('');
  }

  /** Thousands separators on a pure integer-digit string. */
  function _group(intDigits) {
    return intDigits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /** Sign of a plain decimal string: -1 / 0 / 1 (exact — no coercion). */
  function _sign(s) {
    if (!/[1-9]/.test(s)) return 0;          // all zeros (with or without a point)
    return s.charAt(0) === '-' ? -1 : 1;
  }

  /** Absolute value of a plain decimal string (drops the sign character only). */
  function _abs(s) { return s.replace(/^[+-]/, ''); }

  /** Multiply a plain decimal string by 100 by SHIFTING the point (exact). */
  function _x100(s) {
    const sign = s.charAt(0) === '-' ? '-' : '';
    let body = _abs(s);
    let [i, f] = body.indexOf('.') === -1 ? [body, ''] : body.split('.');
    while (f.length < 2) f += '0';
    i = (i + f.slice(0, 2)).replace(/^0+(?=\d)/, '');
    f = f.slice(2);
    return sign + i + (f ? '.' + f : '');
  }

  /** Round a plain decimal string to `dp` places, HALF-UP, exactly. Returns
      {sign, int, frac} with `int` ungrouped digits and `frac` exactly `dp` digits. */
  function _round(s, dp) {
    const neg = s.charAt(0) === '-';
    const body = _abs(s);
    let [i, f] = body.indexOf('.') === -1 ? [body, ''] : body.split('.');
    if (i === '') i = '0';
    if (f.length <= dp) {
      while (f.length < dp) f += '0';
    } else {
      const keep = f.slice(0, dp);
      const roundUp = f.charAt(dp) >= '5';
      let joined = i + keep;
      if (roundUp) joined = _inc(joined);
      // the carry may have grown the integer part by one digit
      const intLen = joined.length - dp;
      i = joined.slice(0, intLen) || '0';
      f = dp ? joined.slice(intLen) : '';
    }
    i = i.replace(/^0+(?=\d)/, '');
    return { neg: neg, int: i, frac: f };
  }

  /** Plain number with thousands separators (exact; see the module note). */
  function num(v, dp) {
    if (isNil(v)) return NULL_GLYPH;
    dp = dp === undefined ? 0 : dp;
    const s = String(v).trim();
    if (!PLAIN_DECIMAL.test(s)) {
      // Not a plain decimal (e.g. scientific notation) — the wire never carries this, but
      // degrade to the float path rather than rendering nothing.
      const n = Number(v);
      if (!isFinite(n)) return NULL_GLYPH;
      return n.toLocaleString('en-US', {
        minimumFractionDigits: dp, maximumFractionDigits: dp
      });
    }
    const r = _round(s, dp);
    const body = _group(r.int) + (dp ? '.' + r.frac : '');
    // "-0" / "-0.00" is noise, not information — a rounded-away negative reads as zero.
    if (r.neg && /[1-9]/.test(r.int + r.frac)) return '-' + body;
    return body;
  }

  /** Share COUNT: up to 6 dp, trailing zeros trimmed (owner ruling 2026-08-06).
   *
   * Shares are NOT money and must not inherit `num`'s 0-dp default. A DRIP/STOCK reinvest is
   * `net / price` — an unrounded quotient — so at 0 dp a real 0.0457-share reinvest rendered
   * as "0", and the symbol drawer's reconciliation footer printed "期初 0 ＋買 95 −賣 0 ＋配股/
   * DRIP 0 ＝ 部位摘要 95 股" beside a ⚠ 對帳不一致 it had no way to explain (measured on the
   * demo site 2026-08-05). It cut the dangerous way too: a REAL sub-0.5-share break also
   * printed as a perfectly balanced equation.
   *
   * ONE definition for every surface. Before this there were four (0 dp, 2 dp, 4 dp, and two
   * hand-rolled "4 dp if it has a fraction" branches), so the same position's share count
   * disagreed between the dashboard table, the drawer, the ledger and the picker.
   *
   * 6 dp matches the server's `_SHARE_EPS` reconciliation tolerance, so a share gap the
   * backend flags can never round away to "0" on screen. The trim keeps an ordinary
   * whole-share ledger reading "95", not "95.000000"; the no-dot guard is load-bearing —
   * without it "10" would lose its trailing zero and render as "1".
   */
  function shares(v) {
    return exact(v);
  }

  /** A Decimal string shown AS SENT: grouped, up to 6 dp, trailing zeros stripped.

      For any value where a display rounding would contradict what actually gets stored.
      `price(v, 'USD')` renders 28.5714 as "28.57" — correct for a quote the owner only
      reads, and wrong for D44's restated target level, which the owner is deciding whether
      to SAVE: they would tick a box labelled 28.57 and the ledger would receive 28.5714.
      Same reason `shares` (which is this function, named for its first caller) never
      rounded a fractional DRIP share count. */
  function exact(v) {
    const s = num(v, 6);
    if (s === NULL_GLYPH) return s;
    return s.indexOf('.') < 0 ? s : s.replace(/\.?0+$/, '');
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
    const s = String(v).trim();
    const sign = PLAIN_DECIMAL.test(s) ? _sign(s) : (Number(v) > 0 ? 1 : Number(v) < 0 ? -1 : 0);
    const body = money(PLAIN_DECIMAL.test(s) ? _abs(s) : Math.abs(Number(v)), ccy);
    if (sign > 0) return '+' + body;
    if (sign < 0) return MINUS + body;
    return body;
  }

  /** Signed plain number with fixed dp (for rates/deltas). */
  function signedNum(v, dp) {
    if (isNil(v)) return NULL_GLYPH;
    const s = String(v).trim();
    const sign = PLAIN_DECIMAL.test(s) ? _sign(s) : (Number(v) > 0 ? 1 : Number(v) < 0 ? -1 : 0);
    const body = num(PLAIN_DECIMAL.test(s) ? _abs(s) : Math.abs(Number(v)), dp);
    if (sign > 0) return '+' + body;
    if (sign < 0) return MINUS + body;
    return body;
  }

  /** Ratio -> percentage with 2 dp: 0.2147 -> "21.47%". */
  function pct(v) {
    if (isNil(v)) return NULL_GLYPH;
    const s = String(v).trim();
    if (!PLAIN_DECIMAL.test(s)) return (Number(v) * 100).toFixed(2) + '%';
    return num(_x100(s), 2) + '%';
  }

  /** Signed percentage. */
  function signedPct(v) {
    if (isNil(v)) return NULL_GLYPH;
    const s = String(v).trim();
    if (!PLAIN_DECIMAL.test(s)) {
      const n = Number(v);
      const b = (Math.abs(n) * 100).toFixed(2) + '%';
      return n > 0 ? '+' + b : n < 0 ? MINUS + b : b;
    }
    const sign = _sign(s);
    const body = num(_x100(_abs(s)), 2) + '%';
    if (sign > 0) return '+' + body;
    if (sign < 0) return MINUS + body;
    return body;
  }

  /** FX rate, 2–4 dp depending on magnitude. */
  function rate(v) {
    if (isNil(v)) return NULL_GLYPH;
    return num(v, Number(v) < 10 ? 4 : 2);
  }

  /** FX rate at a FIXED precision (default 4 dp) — the 換匯 ledgers' 隱含匯率 columns
      (M3-08, 2026-09-06). `rate()`'s magnitude switch printed one column in two precisions
      (4.6000 beside 28.00), and 28.00 is a rate rounded like money: rates are NOT money
      (data-and-pricing.md — 4–6 dp). `rate()` itself is unchanged; the dashboard's 換匯損益
      card keeps its 2 dp by owner ruling. */
  function rateExact(v, dp) {
    if (isNil(v)) return NULL_GLYPH;
    return num(v, dp === undefined ? 4 : dp);
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
    const s = String(v).trim();
    const sign = PLAIN_DECIMAL.test(s) ? _sign(s) : (Number(v) > 0 ? 1 : Number(v) < 0 ? -1 : 0);
    if (sign > 0) return 'sign-up';
    if (sign < 0) return 'sign-down';
    return 'sign-flat';
  }

  /** Unified AI attribution line for every LLM-generated surface (2026-07-07):
      "haiku-4.5 · token 1,234 · $0.1234". Segments degrade independently —
      legacy rows without token counts omit the token segment; cost "0" still shows. */
  function aiAttrib(model, tokensIn, tokensOut, costUsd) {
    const parts = [];
    if (model) parts.push(String(model));
    const tokens = (Number(tokensIn) || 0) + (Number(tokensOut) || 0);
    if (tokens > 0) parts.push('token ' + num(tokens, 0));
    if (!isNil(costUsd)) parts.push('$' + num(costUsd, 4));
    return parts.join(' · ');
  }

  return { num, shares, exact, money, price, signed, signedNum, pct, signedPct, rate, rateExact,
           date, datetime, signClass, aiAttrib, NULL_GLYPH };
})();
