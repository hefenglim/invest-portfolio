/* demo-cycle report kit — the interactive-demo harness.
 *
 * Every claim in a demo-cycle report is either MEASURED in front of the reader or
 * CHECKED against the real engine in front of the reader. This file is what makes both
 * cheap enough that there is no excuse to write prose instead.
 *
 *   PD.sandbox(frame, {css, html, probe})  live iframe running the REAL cascade;
 *                                          `probe` is measured inside and posted back
 *   PD.modes(el, cb)                       before/after switch
 *   PD.sizer(range, frame, label)          viewport-width slider for a sandbox
 *   PD.check(name, pass, detail)           self-check registry -> window.__demoCycleChecks
 *   PD.checkPanel(el)                      renders the self-check strip
 *   PD.ro(el, value, state)                write one readout tile
 *   PD.verdict(el, state, html)            write the verdict line
 *
 * Money rule (CLAUDE.md invariant 3): never compute currency with JS floats. Either
 * print a Decimal STRING the backend produced, or — if a lab must recompute — do it in
 * integer minor units and PD.check() the result against a real-engine figure.
 */
window.PD = (function () {
  'use strict';

  const $ = (s, root) => (root || document).querySelector(s);
  const $$ = (s, root) => Array.from((root || document).querySelectorAll(s));

  /* ---------------------------------------------------------------- sandbox */

  const inbox = {};
  let seq = 0;
  window.addEventListener('message', function (e) {
    const d = e.data;
    if (d && d.__pd && inbox[d.__pd]) inbox[d.__pd](d);
  });

  /**
   * Render `html` + `css` inside `frame` as a real document, and evaluate `probe`
   * (a JS object-literal expression) inside it on load/resize/mutation, posting the
   * result to `onData`. The point is that the browser resolves the cascade, not us.
   */
  function sandbox(frame, opts) {
    const id = opts.id || ('pd' + (++seq));
    if (opts.onData) inbox[id] = opts.onData;
    const base = 'html,body{margin:0;background:#0e1116;color:#e6e9ee;font-size:13px;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC","PingFang TC",sans-serif}';
    const probe = opts.probe
      ? '(function(){function m(){try{var d=' + opts.probe +
        ';d.__pd=' + JSON.stringify(id) + ';parent.postMessage(d,"*")}catch(e){}}' +
        'addEventListener("resize",m);new ResizeObserver(m).observe(document.body);' +
        'requestAnimationFrame(function(){m();setTimeout(m,60)})})()'
      : '';
    frame.srcdoc = '<!doctype html><meta charset="utf-8"><style>' + base +
      (opts.css || '') + '</style>' + (opts.html || '') +
      (probe ? '<scr' + 'ipt>' + probe + '</scr' + 'ipt>' : '');
    return id;
  }

  /* ------------------------------------------------------------ mode switch */

  /** Wire a `.modes` button row. `cb(mode)` fires on click and once immediately. */
  function modes(el, cb) {
    el.addEventListener('click', function (e) {
      const b = e.target.closest('button');
      if (!b) return;
      const m = +b.dataset.m;
      $$('button', el).forEach(function (x) {
        x.className = (x === b) ? ('on ' + (x.dataset.tone || '')) : '';
      });
      cb(m);
    });
    const first = $('button', el);
    if (first) cb(+first.dataset.m || 0);
  }

  /** Width slider driving a sandbox frame. Reports the width the DEMO runs at. */
  function sizer(range, frame, label) {
    function apply() {
      frame.style.width = range.value + 'px';
      if (label) label.textContent = range.value;
    }
    range.addEventListener('input', apply);
    apply();
  }

  /* ------------------------------------------------------------- readouts */

  function ro(el, value, state) {
    if (!el) return;
    const v = el.classList.contains('v') ? el : $('.v', el);
    if (v) v.textContent = value;
    const tile = el.closest ? (el.closest('.ro') || el) : el;
    if (state) tile.className = 'ro ' + state;
  }

  function verdict(el, state, html) {
    if (!el) return;
    el.className = 'verdict' + (state ? ' ' + state : '');
    el.innerHTML = html;
  }

  /* ----------------------------------------------------------- self-checks */

  const checks = [];
  window.__demoCycleChecks = checks;

  /**
   * Record a self-check. Use it whenever a lab reimplements app logic offline:
   * the expected value must come from the REAL engine (an /api response, a pytest
   * fixture, a stress-audit oracle line), pasted in as a constant.
   * verify_report.py fails the report if any check is false.
   */
  function check(name, pass, detail) {
    checks.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
    if (checkPanelEl) renderChecks();
    return !!pass;
  }

  /** Convenience: assert an offline computation equals the real engine's string. */
  function checkEq(name, actual, expected, detail) {
    const a = String(actual), e = String(expected);
    return check(name, a === e, (detail ? detail + ' — ' : '') + 'got ' + a + ', engine says ' + e);
  }

  let checkPanelEl = null;
  function checkPanel(el) {
    checkPanelEl = el;
    renderChecks();
  }
  function renderChecks() {
    const pass = checks.filter(c => c.pass).length;
    const rows = checks.map(c =>
      '<tr><td>' + (c.pass ? '<span class="pill pass">PASS</span>' : '<span class="pill rej">FAIL</span>') +
      '</td><td>' + esc(c.name) + '</td><td class="dim">' + esc(c.detail) + '</td></tr>').join('');
    checkPanelEl.innerHTML =
      '<div class="hd"><b>自我校驗 — 本頁離線重算 vs 真實引擎</b>' +
      '<span class="sum">' + pass + '/' + checks.length + ' pass</span></div>' +
      '<div class="tw"><table><thead><tr><th>結果</th><th>檢查</th><th>細節</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="3" class="dim">no checks registered</td></tr>') +
      '</tbody></table></div>';
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  /* --------------------------------------------------------------- numbers */

  /** Thousands separators for a Decimal STRING, without ever touching Number. */
  function group(decStr) {
    const s = String(decStr).trim();
    const m = /^(-?)(\d+)(\.\d+)?$/.exec(s);
    if (!m) return s;
    return m[1] + m[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (m[3] || '');
  }

  /* ------------------------------------------------------------------ misc */

  // Sticky-TOC scroll spy: no dependency, keeps a long report navigable.
  function tocSpy() {
    const links = $$('.toc a');
    if (!links.length) return;
    const map = links.map(a => ({ a: a, el: $(a.getAttribute('href')) })).filter(x => x.el);
    function upd() {
      let cur = map[0];
      for (const m of map) if (m.el.getBoundingClientRect().top < 90) cur = m;
      map.forEach(m => m.a.style.color = (m === cur ? 'var(--accent)' : ''));
    }
    addEventListener('scroll', upd, { passive: true });
    upd();
  }
  if (document.readyState !== 'loading') tocSpy();
  else addEventListener('DOMContentLoaded', tocSpy);

  return { $: $, $$: $$, sandbox, modes, sizer, ro, verdict, check, checkEq, checkPanel,
           group, esc };
})();
