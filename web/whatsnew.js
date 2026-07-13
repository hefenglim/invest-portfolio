/* portfolio-dash — 「新功能」announcement panel + 版本發佈資訊 history browser (WP-WN).
   Lazy-loaded by shell.js AFTER the /api/health version resolves (so the ?v= stamp is
   applied). All network goes through window.pdApi (the single fetch layer); every path
   degrades silently — this must never break a page. Exposes window.pdWhatsNew
   { init, open, openHistory }. Counts + strings only; no money.

   Round 3 seen semantics: a feature's NEW state clears ONLY when the user acts on it —
   前往 (navigate), 知道了 (dismiss, href-less features), or 全部標示已讀 (whole window).
   Opening the panel no longer acknowledges anything. State is server truth
   (whatsnew_seen); optimistic UI updates the cached payload + DOM, then POSTs. */
(function () {
  'use strict';

  var _payload = null;   // cached GET /api/whats-new response, for open() + optimistic edits

  function _api() { return window.pdApi || null; }
  function _btn() { return document.getElementById('wn-btn'); }
  function _panelBody() { return document.querySelector('.wn-backdrop .wn-body'); }

  function _el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function _currentPage() {
    return (window.location.pathname.split('/').pop() || 'index.html');
  }

  /* ---- ambient badge (topbar dot + sidebar NEW pill) ---- */
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
    /* scoped to the two ambient badges (topbar dot + sidebar pill); the panel's own
       per-feature / per-group pills are removed separately by the seen handlers. */
    document.querySelectorAll('#wn-btn .wn-dot, .brand-ver .wn-pill')
      .forEach(function (n) { n.remove(); });
    var btn = _btn();
    if (btn) btn.classList.remove('wn-has-dot');
  }

  /* Reconcile the ambient badge with the cached payload's unseen_count. */
  function _syncBadge() {
    if (_payload && typeof _payload.unseen_count === 'number' && _payload.unseen_count > 0) {
      _showBadge();
    } else {
      _clearBadge();
    }
  }

  /* ---- arrival highlight + in-page callout (via 前往) ------------------------------
     Arriving from a 前往 click blinks the exact section that changed (1s in / 1s out ×10,
     ~20s) AND drops a dismissible callout card right before it (WHAT changed, WHERE). A tab
     switch (hashchange OR the settings replaceState-based pd-settings-tab event) or leaving
     the page cancels the whole thing immediately, and — since teardown removes the callout
     from the DOM — it never resurfaces on switch-back; only a fresh 前往 re-arms it. */
  var FLASH_MS = 21000;         // safety net: a hair past the 20s CSS blink (2s × 10)
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

    /* ~20s blink (1s in / 1s out × 10). Reduced-motion suppresses the animation (no
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

  /* ---- seen-state (per feature) --------------------------------------------------- */

  /* Optimistically flip a feature to seen in the cached payload, recomputing each group's
     `unseen` flag and the top-level `unseen_count` (the ambient-badge source of truth). */
  function _markSeenLocal(key) {
    if (!_payload || !_payload.versions) return;
    var count = 0;
    _payload.versions.forEach(function (g) {
      var groupUnseen = false;
      (g.features || []).forEach(function (f) {
        if (f.key === key) f.seen = true;
        if (!f.seen) { groupUnseen = true; count += 1; }
      });
      g.unseen = groupUnseen;
    });
    _payload.unseen_count = count;
  }

  /* Remove a feature row's NEW pill in the open panel, and drop its group-head pill once
     the group has no unseen rows left. No-op when the panel is closed. */
  function _removeRowPill(key) {
    var body = _panelBody();
    if (!body) return;
    var row = body.querySelector('.wn-feat[data-wn-key="' + key + '"]');
    if (!row) return;
    var pill = row.querySelector('.wn-new-pill');
    if (pill) pill.remove();
    var grp = row.closest('.wn-group');
    if (grp && !grp.querySelector('.wn-new-pill')) {
      var head = grp.querySelector('.wn-group-head .wn-pill');
      if (head) head.remove();
    }
  }

  /* Mark a single feature seen: optimistic local + DOM + ambient badge, then POST
     fire-and-forget (refresh _payload from the response; silent on failure). Returns the
     POST promise so the cross-page 前往 can await it before the page unloads. */
  function _markSeen(key, applyDom) {
    _markSeenLocal(key);
    if (applyDom) _removeRowPill(key);
    _syncBadge();
    var api = _api();
    if (!api) return Promise.resolve();
    return api.post('/api/whats-new/seen', { features: [key] })
      .then(function (resp) { if (resp && resp.versions) _payload = resp; })
      .catch(function () { /* silent: the pill is already cleared optimistically */ });
  }

  /* 全部標示已讀: mark every feature in the payload seen, clear all pills + the badge, POST
     {all:true}. Optimistic and silent-on-failure like the single case. */
  function _markAll() {
    if (_payload && _payload.versions) {
      _payload.versions.forEach(function (g) {
        (g.features || []).forEach(function (f) { f.seen = true; });
        g.unseen = false;
      });
      _payload.unseen_count = 0;
    }
    var body = _panelBody();
    if (body) {
      body.querySelectorAll('.wn-new-pill, .wn-pill').forEach(function (n) { n.remove(); });
    }
    _clearBadge();
    var api = _api();
    if (!api) return;
    api.post('/api/whats-new/seen', { all: true })
      .then(function (resp) { if (resp && resp.versions) _payload = resp; })
      .catch(function () { /* silent */ });
  }

  /* ---- panel rendering ---- */
  function _renderFeature(f, dismiss) {
    var row = _el('div', 'wn-feat');
    row.dataset.wnKey = f.key;
    var main = _el('div', 'wn-feat-main');
    var title = _el('div', 'wn-feat-title', f.title);
    if (!f.seen) title.appendChild(_el('span', 'wn-new-pill', 'NEW'));
    main.appendChild(title);
    if (f.desc) main.appendChild(_el('div', 'wn-feat-desc', f.desc));
    if (f.area) main.appendChild(_el('div', 'wn-feat-area', f.area));
    row.appendChild(main);
    if (f.href) {
      var go = _el('button', 'wn-go', '前往 →');
      go.type = 'button';
      go.addEventListener('click', function () { _navigate(f, dismiss); });
      row.appendChild(go);
    } else {
      /* href-less feature (e.g. the what's-new panel itself): no page to go to, so
         「知道了」 marks it seen in place (panel stays open). */
      var ack = _el('button', 'wn-go wn-ack', '知道了');
      ack.type = 'button';
      ack.addEventListener('click', function () { _markSeen(f.key, true); });
      row.appendChild(ack);
    }
    return row;
  }

  function _renderGroup(grp, dismiss) {
    var wrap = _el('div', 'wn-group');
    wrap.dataset.wnVersion = grp.version;
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
      /* footer: 全部標示已讀 clears every pill + the ambient badge. */
      var foot = _el('div', 'modal-foot wn-foot');
      var allBtn = _el('button', 'btn', '全部標示已讀');
      allBtn.type = 'button';
      allBtn.addEventListener('click', _markAll);
      foot.appendChild(allBtn);
      modal.appendChild(foot);
    }
    backdrop.appendChild(modal);
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
    var page = String(feat.href).split('#')[0];
    if (page === _currentPage()) {
      /* same page: no reload runs init(), so mark seen (fire-and-forget), switch the hash
         (the settings page's own hashchange handler switches tabs) and run the SAME
         arrival routine (callout + scroll + flash) directly — not just a bare flash. */
      _markSeen(feat.key, true);
      if (dismiss) dismiss();
      var hash = _hashOf(feat.href);
      if (hash) { window.location.hash = hash; }
      /* defer past our OWN hash/tab-switch events (two frames) so the arrival's own cancel
         listeners aren't tripped by the very switch that triggered it. */
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () { _startArrival(marker); });
      });
    } else {
      /* cross-page: the navigation unloads this page, which would race (and usually cancel)
         a fire-and-forget POST. AWAIT the seen POST (bounded) so the feature reliably
         persists as seen before we leave — then navigate. */
      try { sessionStorage.setItem('pd_wn_flash', JSON.stringify(marker)); } catch (e) { /* noop */ }
      var navigated = false;
      var go = function () {
        if (navigated) return;
        navigated = true;
        window.location.href = feat.href;
      };
      _markSeen(feat.key, false).then(go, go);
      window.setTimeout(go, 1500);  // safety: navigate even if the POST stalls
    }
  }

  /* ---- public API ---- */
  function _openWith(p) {
    _renderPanel(p);  // opening no longer acknowledges anything (round 3)
  }

  function open() {
    if (_payload) { _openWith(_payload); return; }
    var api = _api();
    if (!api) { _renderPanel(null); return; }  // no fetch layer -> empty-state panel
    api.get('/api/whats-new')
      .then(function (p) { _payload = p; _openWith(p); })
      .catch(function () { _renderPanel(null); });
  }

  /* ---- 版本發佈資訊 (full version-history browser) --------------------------------- */
  function _renderHistoryVersion(v) {
    var grp = _el('div', 'wnh-group');
    var head = _el('div', 'wnh-head');
    var label = 'v' + v.version + (v.date ? ' · ' + v.date : '');
    head.appendChild(_el('span', 'wnh-ver', label));
    grp.appendChild(head);
    (v.features || []).forEach(function (f) {
      var row = _el('div', 'wnh-feat');
      row.appendChild(_el('div', 'wnh-title', f.title));
      if (f.desc) row.appendChild(_el('div', 'wnh-desc', f.desc));
      if (f.area) row.appendChild(_el('div', 'wnh-area', f.area));
      grp.appendChild(row);
    });
    return grp;
  }

  /* Modal that pages through the FULL catalog (newest first). Loaded pages are APPENDED
     (never re-rendered), so an ever-growing catalog cannot degrade this view. */
  function openHistory() {
    if (document.querySelector('.wnh-backdrop')) return;  // guard double-open
    var backdrop = _el('div', 'modal-backdrop wnh-backdrop');
    var modal = _el('div', 'modal wnh-modal');
    var head = _el('div', 'modal-head');
    head.appendChild(_el('h3', 'modal-title', '版本發佈資訊'));
    var close = _el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    var body = _el('div', 'modal-body wnh-body');
    modal.appendChild(body);
    var foot = _el('div', 'modal-foot wnh-foot');
    var moreBtn = _el('button', 'btn', '載入更早版本');
    moreBtn.type = 'button';
    foot.appendChild(moreBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    var onKey = function (e) { if (e.key === 'Escape') dismiss(); };
    var dismiss = function () {
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
    };
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) dismiss(); });
    document.addEventListener('keydown', onKey);

    var LIMIT = 5;
    var loaded = 0;
    var total = null;
    var busy = false;

    function loadNext() {
      if (busy) return;
      var api = _api();
      if (!api) {
        if (!loaded) body.appendChild(_el('div', 'wnh-empty', '目前無法載入版本資訊'));
        moreBtn.style.display = 'none';
        return;
      }
      busy = true;
      moreBtn.disabled = true;
      moreBtn.textContent = '載入中…';
      api.get('/api/whats-new/history', { offset: loaded, limit: LIMIT })
        .then(function (p) {
          total = (p && typeof p.total === 'number') ? p.total : loaded;
          var vers = (p && p.versions) || [];
          vers.forEach(function (v) { body.appendChild(_renderHistoryVersion(v)); });
          loaded += vers.length;
          busy = false;
          moreBtn.disabled = false;
          moreBtn.textContent = '載入更早版本';
          if (!loaded) body.appendChild(_el('div', 'wnh-empty', '目前沒有版本資訊'));
          if (loaded >= total || !vers.length) { moreBtn.style.display = 'none'; }
        })
        .catch(function () {
          busy = false;
          moreBtn.disabled = false;
          moreBtn.textContent = '載入更早版本';
          if (!loaded) body.appendChild(_el('div', 'wnh-empty', '目前無法載入版本資訊'));
        });
    }
    moreBtn.addEventListener('click', loadNext);
    document.body.appendChild(backdrop);
    loadNext();  // first page
  }

  /* ---- boot ---- */
  function init() {
    _consumeFlash();  // arrival highlight is independent of the API
    var api = _api();
    if (!api) return;
    api.get('/api/whats-new')
      .then(function (p) {
        _payload = p;
        _syncBadge();
      })
      .catch(function () { /* silent: badge is a hint, not a gate */ });
  }

  window.pdWhatsNew = { init: init, open: open, openHistory: openHistory };
})();
