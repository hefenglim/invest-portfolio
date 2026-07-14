/* portfolio-dash — 今日摘要 + 週行動清單 dashboard cards (P3 batch 3 · Wave 1).

   Self-contained, copied from the renderCashMini pattern (app.js): two independent GETs
   (/api/digest/latest?kind=daily|weekly) render into their own panels and degrade to a
   friendly empty state on null / failure — never an unhandled rejection (the e2e smoke
   asserts ZERO console errors). Money/percentages arrive as Decimal STRINGS and route
   through window.fmt (which coerces for display only) — the frontend never computes.

   History opens a load-more modal (copied from web/whatsnew.js openHistory) paging
   GET /api/digest/history?kind=&offset=&limit=. */
(function () {
  'use strict';
  var f = window.fmt;
  var api = window.pdApi;
  var $ = function (sel) { return document.querySelector(sel); };
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  var EMPTY_MSG = '尚未產生摘要 — 於 設定 → 預警規則 → 摘要與週報 啟用，或按「立即產生」。';

  /* ---- daily card ---- */
  function renderDaily(host, d) {
    if (!host) return;
    host.replaceChildren();
    if (!d || !d.payload) { host.appendChild(el('div', 'digest-empty', EMPTY_MSG)); return; }
    var p = d.payload;

    /* headline: value-weighted portfolio day-change (price-only, excludes FX drift). */
    var dc = p.day_change || {};
    var head = el('div', 'digest-headline');
    head.appendChild(el('span', 'digest-head-label', '組合當日'));
    var pctStr = dc.portfolio_pct;
    var pct = el('span', 'digest-pct ' + f.signClass(pctStr == null ? null : pctStr));
    pct.textContent = pctStr == null ? f.NULL_GLYPH : f.signedPct(pctStr);
    head.appendChild(pct);
    if (dc.excluded_count) {
      head.appendChild(el('span', 'digest-sub', '（' + dc.excluded_count + ' 檔資料不足未計入）'));
    }
    host.appendChild(head);

    /* movers: top up + top down chips. */
    var movers = p.movers || { up: [], down: [] };
    var ups = movers.up || [], downs = movers.down || [];
    if (ups.length || downs.length) {
      var chips = el('div', 'digest-movers');
      ups.concat(downs).forEach(function (m) {
        var chip = el('span', 'digest-mover ' + f.signClass(m.pct));
        chip.appendChild(el('span', 'digest-mover-sym', m.symbol));
        chip.appendChild(el('span', 'digest-mover-pct', f.signedPct(m.pct)));
        chip.title = m.name || m.symbol;
        chips.appendChild(chip);
      });
      host.appendChild(chips);
    }

    /* alert / signal counts with jump links. */
    var alertN = (p.alerts_today || []).reduce(function (a, g) {
      return a + (Number(g.count) || 0);
    }, 0);
    var sigN = (p.signals_today || []).length;
    var counts = el('div', 'digest-counts');
    counts.appendChild(_countLink('今日警示', alertN, 'settings.html#alerts'));
    counts.appendChild(_countLink('今日訊號', sigN, 'instruments.html'));
    host.appendChild(counts);

    /* data health line. */
    var dh = p.data_health || { stale: [], failed_jobs: 0 };
    var stale = (dh.stale || []).length;
    var failed = Number(dh.failed_jobs) || 0;
    var healthText = (!stale && !failed)
      ? '資料健康：良好'
      : '資料健康：停滯報價 ' + stale + '・失敗工作 ' + failed;
    var health = el('div', 'digest-health' + ((stale || failed) ? ' warn' : ''), healthText);
    host.appendChild(health);

    /* optional AI one-liner. */
    if (p.llm_note && p.llm_note.text) {
      var note = el('div', 'digest-ai');
      note.appendChild(el('span', 'digest-ai-tag', 'AI'));
      note.appendChild(el('span', null, p.llm_note.text));
      host.appendChild(note);
    }

    host.appendChild(el('div', 'digest-stamp', '產生於 ' + f.datetime(d.generated_at)));
  }

  function _countLink(label, n, href) {
    var a = el('a', 'digest-count', label + ' ' + n);
    a.href = href;
    return a;
  }

  /* ---- weekly card ---- */
  function renderWeekly(host, d) {
    if (!host) return;
    host.replaceChildren();
    if (!d || !d.payload) { host.appendChild(el('div', 'digest-empty', EMPTY_MSG)); return; }
    var items = d.payload.items || [];
    if (!items.length) {
      host.appendChild(el('div', 'digest-empty', '本週無待辦事項 — 一切就緒。'));
      host.appendChild(el('div', 'digest-stamp', '產生於 ' + f.datetime(d.generated_at)));
      return;
    }
    var list = el('div', 'digest-checklist');
    items.forEach(function (it) {
      var row = el('div', 'digest-item');
      row.appendChild(el('span', 'digest-item-ico', it.icon || '•'));
      var main = el('div', 'digest-item-main');
      main.appendChild(el('div', 'digest-item-title', it.title || ''));
      if (it.desc) main.appendChild(el('div', 'digest-item-desc', it.desc));
      row.appendChild(main);
      if (it.href) {
        var btn = el('button', 'btn', '前往');
        btn.type = 'button';
        btn.addEventListener('click', function () { window.location.href = it.href; });
        row.appendChild(btn);
      }
      list.appendChild(row);
    });
    host.appendChild(list);
    host.appendChild(el('div', 'digest-stamp', '產生於 ' + f.datetime(d.generated_at)));
  }

  /* ---- history modal (copied from web/whatsnew.js openHistory) ---- */
  function _histRow(kind, row) {
    var grp = el('div', 'wnh-group');
    var head = el('div', 'wnh-head');
    head.appendChild(el('span', 'wnh-ver', row.digest_date || ''));
    grp.appendChild(head);
    var p = row.payload || {};
    if (kind === 'weekly') {
      var items = p.items || [];
      if (!items.length) { grp.appendChild(el('div', 'wnh-desc', '本週無待辦事項')); }
      items.forEach(function (it) {
        var line = (it.icon || '') + ' ' + (it.title || '') + (it.desc ? ' — ' + it.desc : '');
        grp.appendChild(el('div', 'wnh-desc', line));
      });
    } else {
      var dcp = (p.day_change || {}).portfolio_pct;
      var alertN = (p.alerts_today || []).reduce(function (a, g) {
        return a + (Number(g.count) || 0);
      }, 0);
      var sigN = (p.signals_today || []).length;
      grp.appendChild(el('div', 'wnh-desc',
        '組合 ' + (dcp == null ? f.NULL_GLYPH : f.signedPct(dcp)) +
        '・警示 ' + alertN + '・訊號 ' + sigN));
    }
    return grp;
  }

  function openHistory(kind) {
    if (document.querySelector('.digest-hist-backdrop')) return;  // guard double-open
    var backdrop = el('div', 'modal-backdrop digest-hist-backdrop');
    var modal = el('div', 'modal wnh-modal');
    var head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', kind === 'weekly' ? '週行動清單・歷史' : '今日摘要・歷史'));
    var close = el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    var body = el('div', 'modal-body wnh-body');
    modal.appendChild(body);
    var foot = el('div', 'modal-foot wnh-foot');
    var moreBtn = el('button', 'btn', '載入更早');
    moreBtn.type = 'button';
    foot.appendChild(moreBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    var onKey = function (e) { if (e.key === 'Escape') dismiss(); };
    var dismiss = function () { backdrop.remove(); document.removeEventListener('keydown', onKey); };
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) dismiss(); });
    document.addEventListener('keydown', onKey);

    var LIMIT = 5;
    var loaded = 0;
    var total = null;
    var busy = false;

    function loadNext() {
      if (busy) return;
      if (!api) {
        if (!loaded) body.appendChild(el('div', 'wnh-empty', '目前無法載入摘要歷史'));
        moreBtn.style.display = 'none';
        return;
      }
      busy = true;
      moreBtn.disabled = true;
      moreBtn.textContent = '載入中…';
      api.get('/api/digest/history', { kind: kind, offset: loaded, limit: LIMIT })
        .then(function (p) {
          total = (p && typeof p.total === 'number') ? p.total : loaded;
          var rows = (p && p.rows) || [];
          rows.forEach(function (r) { body.appendChild(_histRow(kind, r)); });
          loaded += rows.length;
          busy = false;
          moreBtn.disabled = false;
          moreBtn.textContent = '載入更早';
          if (!loaded) body.appendChild(el('div', 'wnh-empty', '尚無摘要歷史'));
          if (loaded >= total || !rows.length) { moreBtn.style.display = 'none'; }
        })
        .catch(function () {
          busy = false;
          moreBtn.disabled = false;
          moreBtn.textContent = '載入更早';
          if (!loaded) body.appendChild(el('div', 'wnh-empty', '目前無法載入摘要歷史'));
        });
    }
    moreBtn.addEventListener('click', loadNext);
    document.body.appendChild(backdrop);
    loadNext();
  }

  /* ---- boot ---- */
  function boot() {
    if (!api) return;
    var dailyHost = $('#digest-daily-body');
    var weeklyHost = $('#digest-weekly-body');
    if (dailyHost) {
      api.get('/api/digest/latest', { kind: 'daily' })
        .then(function (d) { renderDaily(dailyHost, d); })
        .catch(function () { renderDaily(dailyHost, null); });
    }
    if (weeklyHost) {
      api.get('/api/digest/latest', { kind: 'weekly' })
        .then(function (d) { renderWeekly(weeklyHost, d); })
        .catch(function () { renderWeekly(weeklyHost, null); });
    }
    var db = $('#digest-daily-history');
    if (db) db.addEventListener('click', function () { openHistory('daily'); });
    var wb = $('#digest-weekly-history');
    if (wb) wb.addEventListener('click', function () { openHistory('weekly'); });
  }

  boot();
})();
