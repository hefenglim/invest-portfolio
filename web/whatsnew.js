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
    document.querySelectorAll('.brand-ver').forEach(function (ver) {
      var parent = ver.parentNode;
      if (!parent || parent.querySelector('.wn-pill')) return;
      var pill = _el('span', 'wn-pill', 'NEW');
      parent.insertBefore(pill, ver.nextSibling);
    });
  }

  function _clearBadge() {
    document.querySelectorAll('.wn-dot, .wn-pill').forEach(function (n) { n.remove(); });
    var btn = _btn();
    if (btn) btn.classList.remove('wn-has-dot');
  }

  /* ---- arrival flash (highlight the section a 前往 link jumped to) ---- */
  function _flashTarget(hash) {
    if (!hash) return null;
    if (_currentPage() === 'settings.html') {
      // Prefer the concrete tab section (present regardless of switch timing), then
      // the active view, then a plain id.
      return document.getElementById('view-' + hash)
        || document.querySelector('.set-view.active')
        || document.getElementById(hash);
    }
    return document.getElementById(hash);
  }

  function _applyFlash(hash) {
    var target = _flashTarget(hash);
    if (!target) return;  // degrade silently when there is nothing to highlight
    target.classList.add('wn-flash');
    var clear = function () {
      target.classList.remove('wn-flash');
      target.removeEventListener('animationend', clear);
    };
    target.addEventListener('animationend', clear);
    /* safety net: reduced-motion suppresses the animation (no animationend), so remove
       the class on a timer too. */
    window.setTimeout(clear, 2000);
  }

  function _consumeFlash() {
    var marker = null;
    try { marker = sessionStorage.getItem('pd_wn_flash'); } catch (e) { marker = null; }
    if (!marker) return;
    var parts = marker.split('#');
    var page = parts[0];
    var hash = parts.length > 1 ? parts[1] : '';
    if (page !== _currentPage()) return;  // marker is for a different page; leave it
    try { sessionStorage.removeItem('pd_wn_flash'); } catch (e) { /* noop */ }
    _applyFlash(hash);
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
      go.addEventListener('click', function () { _navigate(f.href, dismiss); });
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
  function _navigate(href, dismiss) {
    try { sessionStorage.setItem('pd_wn_flash', href); } catch (e) { /* noop */ }
    var parts = href.split('#');
    var page = parts[0];
    var hash = parts.length > 1 ? parts[1] : '';
    if (page === _currentPage()) {
      /* same page: no reload will run init(), so switch the hash (the settings page's
         own hashchange handler switches tabs) and apply the flash directly. */
      if (dismiss) dismiss();
      if (hash) { window.location.hash = hash; }
      try { sessionStorage.removeItem('pd_wn_flash'); } catch (e) { /* noop */ }
      _applyFlash(hash);
    } else {
      window.location.href = href;
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
