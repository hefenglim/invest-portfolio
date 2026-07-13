/* portfolio-dash — 「新功能」announcement panel (WP-WN, 2026-07-13).
   Lazy-loaded by shell.js AFTER the /api/health version resolves (so the ?v= stamp is
   applied). All network goes through window.pdApi (the single fetch layer); every path
   degrades silently — this must never break a page. Exposes window.pdWhatsNew.
   { init, open }. Counts + strings only; no money. */
(function () {
  'use strict';

  var _payload = null;   // cached GET /api/whats-new response, for open()
  var _acked = false;    // POST /whats-new/seen fired at most once per page load

  function _api() { return window.pdApi || null; }
  function _btn() { return document.getElementById('wn-btn'); }

  function _el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function _currentPage() {
    return (window.location.pathname.split('/').pop() || 'index.html');
  }

  /* ---- badge (topbar dot + sidebar NEW pill) ---- */
  function _showBadge() {
    var btn = _btn();
    if (btn && !btn.querySelector('.wn-dot')) {
      btn.classList.add('wn-has-dot');
      btn.appendChild(_el('span', 'wn-dot'));
    }
    /* pill INSIDE .brand-ver so it sits inline next to the version tag (a sidebar-level
       sibling becomes its own stretched flex row) and hides with the collapsed rail. */
    document.querySelectorAll('.brand-ver').forEach(function (ver) {
      if (ver.querySelector('.wn-pill')) return;
      ver.appendChild(_el('span', 'wn-pill', 'NEW'));
    });
  }

  function _clearBadge() {
    /* scoped to the two ambient badges — the panel's own group NEW pills must survive
       the open-ack (they tell the user WHICH versions are new on this very open). */
    document.querySelectorAll('#wn-btn .wn-dot, .brand-ver .wn-pill')
      .forEach(function (n) { n.remove(); });
    var btn = _btn();
    if (btn) btn.classList.remove('wn-has-dot');
  }

  /* ---- arrival highlight + in-page callout (via 前往) ------------------------------
     Arriving from a 前往 click flashes the exact section that changed for ~10s AND drops a
     dismissible callout card right before it (WHAT changed, WHERE). A tab switch
     (hashchange OR the settings replaceState-based pd-settings-tab event) or leaving the
     page cancels the whole thing immediately, and — since teardown removes the callout
     from the DOM — it never resurfaces on switch-back; only a fresh 前往 re-arms it. */
  var FLASH_MS = 11000;         // safety net: a hair past the 10s CSS animation
  var MARKER_FRESH_MS = 30000;  // a marker older than this is stale -> discard unconsumed
  var _arrival = null;          // the single active arrival, or null

  function _hashOf(href) {
    if (!href) return '';
    var parts = String(href).split('#');
    return parts.length > 1 ? parts[1] : '';
  }

  /* Resolve the element to flash / anchor the callout to. A precise `target` selector
     wins (resolved up to its enclosing panel so a small/inline match still wraps a
     meaningful block); else fall back to the settings tab section (existing behaviour).
     Null -> nothing precise to point at (degrade silently: no flash, no callout). */
  function _resolveAnchor(feat) {
    if (feat && feat.target) {
      var el = null;
      try { el = document.querySelector(feat.target); } catch (e) { el = null; }
      if (el) return el.closest('section.panel') || el;
    }
    var hash = _hashOf(feat && feat.href);
    if (_currentPage() === 'settings.html') {
      return document.getElementById('view-' + hash)
        || document.querySelector('.set-view.active')
        || (hash ? document.getElementById(hash) : null);
    }
    return hash ? document.getElementById(hash) : null;
  }

  function _buildCallout(feat) {
    var card = _el('div', 'wn-callout');
    var head = _el('div', 'wn-callout-head');
    head.appendChild(_el('span', 'wn-callout-tag', '✦ 新功能'));
    head.appendChild(_el('span', 'wn-callout-title', feat.title || ''));
    var x = _el('button', 'wn-callout-x', '✕');
    x.type = 'button';
    x.title = '關閉';
    x.addEventListener('click', _endArrival);
    head.appendChild(x);
    card.appendChild(head);
    if (feat.desc) card.appendChild(_el('div', 'wn-callout-desc', feat.desc));
    if (feat.area) card.appendChild(_el('div', 'wn-callout-area', feat.area));
    return card;
  }

  /* Tear the active arrival down completely: drop the flash, remove the callout, and
     unwire every listener/timer. Idempotent; leaves nothing that can resurface. */
  function _endArrival() {
    var a = _arrival;
    if (!a) return;
    _arrival = null;
    if (a.timer) { window.clearTimeout(a.timer); }
    if (a.flashEl) {
      a.flashEl.classList.remove('wn-flash');
      a.flashEl.removeEventListener('animationend', a.onAnimEnd);
    }
    if (a.callout && a.callout.parentNode) { a.callout.parentNode.removeChild(a.callout); }
    window.removeEventListener('hashchange', a.onCancel);
    window.removeEventListener('pd-settings-tab', a.onCancel);
  }

  function _startArrival(feat) {
    _endArrival();  // one at a time — a re-arrival replaces any existing callout/flash
    var anchor = _resolveAnchor(feat);
    if (!anchor) return;  // nothing precise to point at -> degrade silently

    var a = { flashEl: anchor, callout: null, timer: null, onCancel: null, onAnimEnd: null };
    _arrival = a;

    /* callout inserted BEFORE the anchor, in normal flow, so it pushes content down and
       can never overlay a control. */
    var callout = _buildCallout(feat);
    if (anchor.parentNode) {
      anchor.parentNode.insertBefore(callout, anchor);
      a.callout = callout;
    }

    /* cancel on switch: a settings tab change (hashchange OR the replaceState-based
       pd-settings-tab event) ends the arrival at once. Full page navigation destroys the
       DOM, which removes it inherently. */
    a.onCancel = function () { _endArrival(); };
    window.addEventListener('hashchange', a.onCancel);
    window.addEventListener('pd-settings-tab', a.onCancel);

    /* ~10s flash (CSS holds then fades). Reduced-motion suppresses the animation (no
       animationend), so a timer clears the class too. Neither clears the callout — the
       callout persists until ✕ or a cancel. */
    anchor.classList.add('wn-flash');
    a.onAnimEnd = function () { if (a.flashEl) a.flashEl.classList.remove('wn-flash'); };
    anchor.addEventListener('animationend', a.onAnimEnd);
    a.timer = window.setTimeout(a.onAnimEnd, FLASH_MS);

    /* scroll after the tab switch settles (next frame). The callout leads straight into
       the anchor, so bringing it to the top keeps both the explanation and the flashed
       block in view. Async-rendered content ABOVE the anchor (fetched lists, e.g. the
       alert-rules editor) can grow the layout after this first scroll and push the
       target back below the fold — re-assert briefly until it settles. */
    var scrollTo = function () {
      var into = a.callout || anchor;
      try { into.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
      catch (e) { try { into.scrollIntoView(); } catch (e2) { /* noop */ } }
    };
    window.requestAnimationFrame(scrollTo);
    [800, 1600].forEach(function (delay) {
      window.setTimeout(function () {
        if (_arrival !== a) return;  // arrival ended -> never fight the user's scroll
        var into = a.callout || anchor;
        var r = into.getBoundingClientRect();
        if (r.top < 0 || r.top > window.innerHeight * 0.5) scrollTo();
      }, delay);
    });
  }

  /* On arrival: consume the sessionStorage marker (JSON) and, if fresh and for THIS page,
     run the arrival routine. Malformed/legacy markers are discarded silently. */
  function _consumeFlash() {
    var raw = null;
    try { raw = sessionStorage.getItem('pd_wn_flash'); } catch (e) { raw = null; }
    if (!raw) return;
    var feat = null;
    try { feat = JSON.parse(raw); } catch (e) { feat = null; }
    if (!feat || typeof feat !== 'object' || !feat.href) {
      // malformed / legacy string marker -> discard silently
      try { sessionStorage.removeItem('pd_wn_flash'); } catch (e2) { /* noop */ }
      return;
    }
    var page = String(feat.href).split('#')[0];
    if (page !== _currentPage()) return;  // for a different page; leave it for that page
    // matching page: always consume now, whether we fire or it has expired.
    try { sessionStorage.removeItem('pd_wn_flash'); } catch (e3) { /* noop */ }
    var ts = typeof feat.ts === 'number' ? feat.ts : 0;
    if (!ts || (Date.now() - ts) > MARKER_FRESH_MS) return;  // stale -> discard unconsumed
    _startArrival(feat);
  }

  /* ---- panel rendering ---- */
  function _renderFeature(f, dismiss) {
    var row = _el('div', 'wn-feat');
    var main = _el('div', 'wn-feat-main');
    main.appendChild(_el('div', 'wn-feat-title', f.title));
    if (f.desc) main.appendChild(_el('div', 'wn-feat-desc', f.desc));
    if (f.area) main.appendChild(_el('div', 'wn-feat-area', f.area));
    row.appendChild(main);
    if (f.href) {
      var go = _el('button', 'wn-go', '前往 →');
      go.type = 'button';
      go.addEventListener('click', function () { _navigate(f, dismiss); });
      row.appendChild(go);
    }
    return row;
  }

  function _renderGroup(grp, dismiss) {
    var wrap = _el('div', 'wn-group');
    var head = _el('div', 'wn-group-head');
    var label = 'v' + grp.version + (grp.date ? ' · ' + grp.date : '');
    head.appendChild(_el('span', 'wn-ver', label));
    if (grp.unseen) head.appendChild(_el('span', 'wn-pill', 'NEW'));
    wrap.appendChild(head);
    (grp.features || []).forEach(function (f) {
      wrap.appendChild(_renderFeature(f, dismiss));
    });
    return wrap;
  }

  function _renderPanel(p) {
    if (document.querySelector('.wn-backdrop')) return;  // guard double-open
    var backdrop = _el('div', 'modal-backdrop wn-backdrop');
    var modal = _el('div', 'modal wn-modal');
    var head = _el('div', 'modal-head');
    head.appendChild(_el('h3', 'modal-title', '✦ 新功能'));
    var close = _el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    var body = _el('div', 'modal-body wn-body');
    modal.appendChild(body);
    backdrop.appendChild(modal);

    var onKey = function (e) { if (e.key === 'Escape') dismiss(); };
    var dismiss = function () {
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
    };
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) dismiss(); });
    document.addEventListener('keydown', onKey);

    var versions = (p && p.versions) || [];
    if (!versions.length) {
      body.appendChild(_el('div', 'wn-empty', '目前沒有可顯示的新功能'));
    } else {
      versions.forEach(function (grp) { body.appendChild(_renderGroup(grp, dismiss)); });
    }
    document.body.appendChild(backdrop);
  }

  /* ---- 前往 navigation ---- */
  function _navigate(feat, dismiss) {
    /* JSON marker carries WHAT/WHERE + a freshness timestamp, so arrival can render the
       detailed callout and reject a stale marker on a later organic visit. */
    var marker = {
      href: feat.href, id: feat.id, title: feat.title,
      desc: feat.desc, area: feat.area, target: feat.target || null,
      ts: Date.now()
    };
    try { sessionStorage.setItem('pd_wn_flash', JSON.stringify(marker)); } catch (e) { /* noop */ }
    var page = String(feat.href).split('#')[0];
    if (page === _currentPage()) {
      /* same page: no reload runs init(), so switch the hash (the settings page's own
         hashchange handler switches tabs) and run the SAME arrival routine (callout +
         scroll + flash) directly — not just a bare flash. */
      if (dismiss) dismiss();
      var hash = _hashOf(feat.href);
      if (hash) { window.location.hash = hash; }
      try { sessionStorage.removeItem('pd_wn_flash'); } catch (e2) { /* noop */ }
      /* defer past our OWN hash/tab-switch events (two frames) so the arrival's own cancel
         listeners aren't tripped by the very switch that triggered it. */
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () { _startArrival(marker); });
      });
    } else {
      window.location.href = feat.href;
    }
  }

  /* ---- acknowledge on open (optimistic; tolerate failure) ---- */
  function _ack(p) {
    _clearBadge();  // optimistic: the badge goes the moment the panel opens
    if (_acked) return;
    _acked = true;
    var api = _api();
    if (!api || !p || !p.current_version) return;
    api.post('/api/whats-new/seen', { version: p.current_version })
      .then(function (resp) { if (resp && resp.versions) _payload = resp; })
      .catch(function () { /* silent: the badge is already cleared optimistically */ });
  }

  function _openWith(p) {
    _renderPanel(p);
    _ack(p);
  }

  /* ---- public API ---- */
  function open() {
    if (_payload) { _openWith(_payload); return; }
    var api = _api();
    if (!api) { _renderPanel(null); return; }  // no fetch layer -> empty-state panel
    api.get('/api/whats-new')
      .then(function (p) { _payload = p; _openWith(p); })
      .catch(function () { _renderPanel(null); });
  }

  function init() {
    _consumeFlash();  // arrival highlight is independent of the API
    var api = _api();
    if (!api) return;
    api.get('/api/whats-new')
      .then(function (p) {
        _payload = p;
        if (p && typeof p.unseen_count === 'number' && p.unseen_count > 0) _showBadge();
      })
      .catch(function () { /* silent: badge is a hint, not a gate */ });
  }

  window.pdWhatsNew = { init: init, open: open };
})();
