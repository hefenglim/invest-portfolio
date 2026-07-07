/* portfolio-dash — 新聞庫 (batch ④). Reads GET /api/news + /api/news/filters.
   Filters (stock / source / date range / keyword) → server-filtered list; click a row →
   modal with the full summary + link + token/cost. Cost is a Decimal STRING from the
   API — displayed via f.num, never recomputed.
   WPD (2026-07-07): the 200-row dump is replaced by the shared pdPager over the
   endpoint's limit/offset, and the keyword filter moved SERVER-side (`q` over
   title/summary via LIKE) so it matches the whole library, not just the loaded page. */
'use strict';
(function () {
  const api = window.pdApi;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c;
    if (txt !== undefined) n.textContent = txt; return n; };

  const state = {
    stock: '', source: '', from: '', to: '', q: '',
    offset: 0,
    limit: Math.min((window.pdPrefs && window.pdPrefs.page_size) || 50, 200),
  };
  let pager = null;
  let lastItems = [];          // the current server page
  let lastTotals = { count: 0, total_cost_usd: '0' };
  const instrumentNames = {};  // symbol -> display name (GET /api/instruments)

  function langBadge(lang) {
    if (!lang) return null;
    const b = el('span', 'nw-lang ' + (lang === 'zh' ? 'zh' : 'en'), lang === 'zh' ? '中' : 'EN');
    return b;
  }

  function openModal(item) {
    const m = $('#nw-modal');
    m.replaceChildren();
    const x = el('button', 'nw-x', '✕');
    x.addEventListener('click', closeModal);
    m.appendChild(x);
    m.appendChild(el('h3', null, item.title || '(未命名)'));
    const meta = el('div', 'm-meta');
    meta.appendChild(el('span', null, item.date || ''));
    if (item.source) meta.appendChild(el('span', null, '· ' + item.source));
    const lb = langBadge(item.lang); if (lb) meta.appendChild(lb);
    (item.related_stocks || []).forEach((s) => meta.appendChild(el('span', 'nw-tag', s)));
    m.appendChild(meta);
    m.appendChild(el('div', 'm-summary',
      item.summary || (item.headline_only ? '(此則僅取得標題，未整理內文摘要)' : '')));
    const foot = el('div', 'm-foot');
    if (item.link) {
      const a = el('a', null, '前往原文 ↗'); a.href = item.link; a.target = '_blank';
      a.rel = 'noopener noreferrer'; foot.appendChild(a);
    }
    var attrib = f.aiAttrib(item.model, item.tokens_in, item.tokens_out, item.cost_usd);
    if (attrib) foot.appendChild(el('span', 'ai-attrib', attrib));
    m.appendChild(foot);
    $('#nw-back').classList.add('open');
  }
  function closeModal() { $('#nw-back').classList.remove('open'); }
  $('#nw-back').addEventListener('click', (e) => { if (e.target === $('#nw-back')) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  function rowNode(item) {
    const row = el('div', 'nw-row');
    const top = el('div', 'nw-row-top');
    top.appendChild(el('span', 'nw-date', item.date || ''));
    top.appendChild(el('span', 'nw-title', item.title || '(未命名)'));
    top.appendChild(el('span', 'nw-cost', '$' + f.num(item.cost_usd, 4)));
    row.appendChild(top);
    const meta = el('div', 'nw-row-meta');
    if (item.source) meta.appendChild(el('span', 'nw-src', item.source));
    const lb = langBadge(item.lang); if (lb) meta.appendChild(lb);
    (item.related_stocks || []).slice(0, 6).forEach((s) => meta.appendChild(el('span', 'nw-tag', s)));
    if (item.headline_only) meta.appendChild(el('span', 'nw-hl', '· 僅標題'));
    row.appendChild(meta);
    row.addEventListener('click', () => openModal(item));
    return row;
  }

  function qs() {
    const p = [];
    if (state.stock) p.push('symbol=' + encodeURIComponent(state.stock));
    if (state.source) p.push('source=' + encodeURIComponent(state.source));
    if (state.from) p.push('date_from=' + state.from);
    if (state.to) p.push('date_to=' + state.to);
    if (state.q.trim()) p.push('q=' + encodeURIComponent(state.q.trim()));
    p.push('limit=' + state.limit);
    p.push('offset=' + state.offset);
    return '?' + p.join('&');
  }

  function render() {
    const list = $('#nw-list');
    const items = lastItems;
    $('#nw-totals').replaceChildren();
    $('#nw-totals').append(
      document.createTextNode('符合 '),
      Object.assign(el('b'), { textContent: lastTotals.count }),
      document.createTextNode(' 則 · 整理成本累計 '),
      Object.assign(el('b'), { textContent: '$' + f.num(lastTotals.total_cost_usd, 4) }));
    list.replaceChildren();
    if (pager) pager.update({ offset: state.offset, totalCount: lastTotals.count });
    if (!items.length) { list.appendChild(el('div', 'nw-empty',
      '無符合條件的新聞。每晚 news_daily 批次整理入庫後於此顯示。')); return; }
    items.forEach((it) => list.appendChild(rowNode(it)));
  }

  function load() {
    const list = $('#nw-list');
    api.get('/api/news' + qs()).then((resp) => {
      lastItems = (resp && resp.items) || [];
      lastTotals = (resp && resp.totals) || { count: 0, total_cost_usd: '0' };
      render();
    }).catch((err) => {
      if (pager) pager.update({});
      list.replaceChildren(el('div', 'nw-empty', '新聞載入失敗：' + ((err && err.message) || '')));
    });
  }

  /* shared pager over the server's limit/offset (missing host -> no-op controller) */
  if (window.pdPager) {
    pager = window.pdPager.create({
      host: document.getElementById('nw-pager'),
      limit: state.limit, offset: 0, totalCount: 0,
      onPage: (offset) => { state.offset = offset; load(); },
    });
  }

  function initFilters() {
    /* instrument display names ride along in the stock filter（LOW: 代號＋名稱）；
       both fetches degrade independently. */
    Promise.all([
      api.get('/api/news/filters').catch(() => null),
      api.get('/api/instruments').catch(() => null),
    ]).then(([f2, instruments]) => {
      const rows = instruments && Array.isArray(instruments.list) ? instruments.list : [];
      rows.forEach((i) => {
        if (i && i.symbol && i.name) instrumentNames[i.symbol] = i.name;
      });
      if (!f2) return;
      const stockSel = $('#nw-stock'), srcSel = $('#nw-source');
      (f2.stocks || []).forEach((s) => {
        const label = instrumentNames[s] ? s + ' ' + instrumentNames[s] : s;
        const o = el('option', null, label); o.value = s; stockSel.appendChild(o);
      });
      (f2.sources || []).forEach((s) => { const o = el('option', null, s); o.value = s; srcSel.appendChild(o); });
    }).catch(() => {});
  }

  $('#nw-stock').addEventListener('change', (e) => { state.stock = e.target.value; state.offset = 0; load(); });
  $('#nw-source').addEventListener('change', (e) => { state.source = e.target.value; state.offset = 0; load(); });
  $('#nw-from').addEventListener('change', (e) => { state.from = e.target.value; state.offset = 0; load(); });
  $('#nw-to').addEventListener('change', (e) => { state.to = e.target.value; state.offset = 0; load(); });
  /* keyword is a SERVER filter now (whole library) — debounce the round-trip */
  let qTimer = null;
  $('#nw-q').addEventListener('input', () => {
    if (qTimer) clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      state.q = $('#nw-q').value;
      state.offset = 0;
      load();
    }, 300);
  });
  $('#nw-clear').addEventListener('click', () => {
    state.stock = state.source = state.from = state.to = state.q = '';
    state.offset = 0;
    $('#nw-stock').value = ''; $('#nw-source').value = '';
    $('#nw-from').value = ''; $('#nw-to').value = ''; $('#nw-q').value = '';
    load();
  });

  initFilters();
  load();
})();
