/* portfolio-dash — 數據變數系統 (variable registry), wired to /api/prompt-vars (spec 06/20).

   The single source of truth for the strategy/system-prompt variable "Lego blocks" is
   now the BACKEND: GET /api/prompt-vars returns the live registry (token/name/category/
   scope/desc/available/sample) PLUS per-var tier metadata (required_tier / tier_ok /
   tier_label, spec 20.15.3) so FinMind chips re-gate automatically when a dataset moves
   to a paid tier. The former inline PD_VARS mock data is RETIRED.

   window.PD_VARS exposes the SAME helper surface the prompts UI already uses
   (CATEGORIES / all / find / render / tokensIn), but the rows are now empty until
   `load()` resolves the fetch and populates them in place. settings-prompts.js awaits
   `PD_VARS.load()` before its first render. `render()` is preview-only (substitutes the
   per-var `sample`); the REAL render/values come from POST /api/prompts/preview|test. */
window.PD_VARS = (function () {
  'use strict';

  /* Category display metadata (UI labels only — NOT registry data). The variable rows,
     their availability and tier state all come from the API; this map only supplies the
     human-readable category name + the section's "需後端新增 / 已具備" marker keyed on the
     category id the API returns (position|price|dividend|fx|chips|sentiment|ai|system). */
  const CAT_META = {
    position:  { name: '部位與績效',            source: 'ready' },
    price:     { name: '價格與技術',            source: 'ready' },
    dividend:  { name: '股利',                  source: 'ready' },
    fx:        { name: '匯率',                  source: 'ready' },
    chips:     { name: '籌碼與基本面（FinMind）', source: 'ingest' },
    news:      { name: '個股新聞', source: 'ingest' },
    sentiment: { name: '市場情緒',              source: 'ingest' },
    ai:        { name: 'AI 自身（校正用）',      source: 'ready' },
    system:    { name: '系統狀態',              source: 'ready' },
  };
  /* Stable category order for rendering (matches the backend REGISTRY order). */
  const CAT_ORDER = ['position', 'price', 'dividend', 'fx', 'chips', 'news', 'sentiment', 'ai', 'system'];
  /* API scope (English) -> the Chinese display string the UI compares against. */
  const SCOPE_LABEL = { per_symbol: '單一標的', portfolio: '全組合' };

  /* Populated in place by load() so callers can keep a stable `const V = PD_VARS` ref. */
  const CATEGORIES = [];
  let _index = {};     // token -> var row (flat)
  let _loaded = false;

  /* Build CATEGORIES (grouped, ordered) + the flat token index from the API rows. */
  function _ingest(rows) {
    CATEGORIES.length = 0;
    _index = {};
    const byCat = {};
    (rows || []).forEach((r) => {
      const meta = CAT_META[r.category] || { name: r.category, source: 'ready' };
      const v = {
        token: r.token,
        name: r.name,
        scope: SCOPE_LABEL[r.scope] || r.scope,
        desc: r.desc,
        sample: r.sample,
        available: r.available,
        category: meta.name,
        source: meta.source,
        /* tier metadata (spec 20.15.3): null requirement -> tier_ok true. */
        required_tier: r.required_tier != null ? r.required_tier : null,
        tier_ok: r.tier_ok !== false,
        tier_label: r.tier_label != null ? r.tier_label : null,
      };
      _index[v.token] = v;
      (byCat[r.category] || (byCat[r.category] = [])).push(v);
    });
    /* Emit categories in the stable order, then any unexpected ids the API may add. */
    const seen = {};
    const order = CAT_ORDER.filter((id) => byCat[id]);
    Object.keys(byCat).forEach((id) => { if (order.indexOf(id) === -1) order.push(id); });
    order.forEach((id) => {
      if (seen[id]) return;
      seen[id] = true;
      const meta = CAT_META[id] || { name: id, source: 'ready' };
      CATEGORIES.push({ id: id, name: meta.name, source: meta.source, vars: byCat[id] });
    });
  }

  /* Fetch the registry once (idempotent). Resolves to the PD_VARS object so callers may
     `const V = await PD_VARS.load();`. On failure, leaves an EMPTY registry and rethrows
     so the caller can surface ONE toast (never an unhandled rejection). */
  let _promise = null;
  function load() {
    if (_loaded) return Promise.resolve(api);
    if (_promise) return _promise;
    _promise = window.pdApi.get('/api/prompt-vars').then((rows) => {
      _ingest(rows);
      _loaded = true;
      return api;
    }).catch((err) => {
      _promise = null;          // allow a retry on next call
      throw err;
    });
    return _promise;
  }

  function all() {
    const out = [];
    CATEGORIES.forEach((c) => c.vars.forEach((v) => out.push(v)));
    return out;
  }

  function find(token) {
    return _index[token] || null;
  }

  /* 預覽替換：把 {{token}} 換成 mock sample；未知 token 標紅保留。
     symbol：per_symbol 範圍的代入標的（範例值以 2330 為模板，代入時換成選定代號示意）。
     這只是設計稿的「離線預覽」；正式預覽走 POST /api/prompts/preview（真實計算值）。 */
  function render(text, symbol) {
    return String(text == null ? '' : text).replace(/\{\{([a-z0-9_]+)\}\}/gi, (m, token) => {
      const v = find(token);
      if (!v) return '⚠未知變數 ' + m;
      return symbol && v.scope === '單一標的' ? v.sample.replace(/2330/g, symbol) : v.sample;
    });
  }

  /* 萃取文中引用的變數 token */
  function tokensIn(text) {
    const out = [];
    (String(text == null ? '' : text).match(/\{\{([a-z0-9_]+)\}\}/gi) || []).forEach((m) => {
      const t = m.slice(2, -2);
      if (!out.includes(t)) out.push(t);
    });
    return out;
  }

  const api = { CATEGORIES, all, find, render, tokensIn, load };
  return api;
})();
