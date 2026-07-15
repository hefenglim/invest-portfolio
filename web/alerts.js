/* portfolio-dash — 風險預警 (規則引擎，非 LLM) + 常駐 AI 額度 chip.

   The topbar bell + AI-quota chip render on EVERY page. The rule engine itself lives in
   the backend (strategy/, spec 03); this file is a pure RENDERER:

   - DASHBOARD (`body[data-page=dashboard]`): consumes the shared /api/dashboard payload
     (D.alerts + D.llm_quota.remaining_usd) — no client recompute (Task 2.2; unchanged).
   - OTHER PAGES: read GET /api/alerts ({as_of, alerts}) for the bell, and GET
     /api/llm/config (quota.remaining_usd + alert_threshold_usd) for the chip. Both are
     async, wrapped in try/catch, and degrade silently (the per-page smokes assert ZERO
     console errors). The legacy client-compute path + localStorage rule mocks are retired
     (spec 19/03 I1).

   Money discipline: the chip value is a Decimal STRING — formatted via window.fmt
   (never `.toFixed` on a wire string). pdApi is the only fetch layer used here. */
(function () {
  'use strict';
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* Is this the dashboard? Only there do we consume the backend D.alerts + D.llm_quota
     (from the shared /api/dashboard promise). Other pages fetch /api/alerts +
     /api/llm/config directly. */
  const isDashboard = document.body.dataset.page === 'dashboard';

  /* Map a backend Alert.href onto the static-frontend routing scheme. Backend hrefs are
     server-route shaped (/symbol/{sym}, /settings, …) with no matching StaticFiles page,
     so we translate them HERE in the renderer (route knowledge stays out of api.js).
     Returns { href, sym? }: sym set when the alert points at a symbol (→ drawer). */
  function mapAlertHref(href) {
    if (!href) return { href: '#' };
    const sym = href.match(/^\/symbol\/(.+)$/);
    if (sym) return { href: 'index.html#sym=' + encodeURIComponent(sym[1]), sym: sym[1] };
    if (href.match(/#sym=(.+)$/)) {
      return { href: href, sym: decodeURIComponent(href.match(/#sym=(.+)$/)[1]) };
    }
    if (href === '/settings') return { href: 'settings.html' };
    if (href === '/settings#llm') return { href: 'settings.html#llm' };
    if (href === '/insights') return { href: 'insights.html' };
    if (href === '/pipeline') return { href: 'pipeline-hub.html' };
    return { href: href };  // already a static page (e.g. settings.html#llm) — pass through
  }

  let alerts = [];
  window.PD_ALERTS = alerts;

  /* ---- bell read-state (3A): a client-side seen-set of alert ids ----
     The bell dot lights ONLY for UNSEEN alert ids (deterministic ids like
     "single_weight:2330" / "quota_low"). Opening the panel marks all CURRENT ids seen;
     a NEW alert id re-lights the dot. Persisted in localStorage (capped) and synced across
     tabs via the storage listener. This is a pure READ-state overlay — it never touches
     alert_events.consumed/notified_at (those drive AI cards + push, a different concern). */
  const SEEN_KEY = 'pd_alerts_seen';
  const SEEN_CAP = 200;
  function loadSeen() {
    try {
      const arr = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]');
      return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : [];
    } catch (e) { return []; }
  }
  let seenIds = loadSeen();  // insertion-ordered array, capped at SEEN_CAP
  function markCurrentSeen() {
    const set = new Set(seenIds);
    let changed = false;
    alerts.forEach((a) => {
      if (a && a.id && !set.has(a.id)) { set.add(a.id); seenIds.push(a.id); changed = true; }
    });
    if (!changed) return;
    if (seenIds.length > SEEN_CAP) seenIds = seenIds.slice(seenIds.length - SEEN_CAP);
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(seenIds)); } catch (e) { /* noop */ }
  }

  /* ---- topbar UI: quota chip + bell ---- */
  const tb = document.getElementById('topbar');
  if (!tb) return;
  const spacer = tb.querySelector('.header-spacer');

  /* AI 額度 chip（常駐）。Value is a Decimal STRING (D.llm_quota.remaining_usd on the
     dashboard, quota.remaining_usd from /api/llm/config elsewhere) formatted via fmt —
     NEVER `.toFixed` on the wire string. `remaining` may be null = 無上限. */
  const chip = el('a', 'badge quota-chip');
  chip.href = 'settings.html#llm';
  chip.title = '剩餘 AI 額度（點擊前往額度設定）';
  /* 2-dp amount for the chip via fmt. Pages that load alerts.js WITHOUT format.js
     (a thin shell) fall back to a Number formatter (never `.toFixed` on a wire string). */
  function money2(v) {
    if (window.fmt) return window.fmt.num(v, 2);
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function renderQuotaChip(remaining, threshold, aiActive) {
    chip.className = 'badge quota-chip';
    chip.replaceChildren();
    /* 3B: when the wire says AI is inactive (no role bound to an enabled model), show a
       neutral 「AI 未啟用」 state — NOT 「AI 額度 $0」+warning dot. quota_low is likewise
       gated off in this state, so the chip and the bell agree. Treat only an explicit
       false as inactive; undefined (a thin page / older payload) keeps the amount view. */
    if (aiActive === false) {
      chip.textContent = 'AI 未啟用';
      chip.title = 'AI 尚未啟用（點擊前往 AI 與額度設定）';
      return;
    }
    if (remaining === null || remaining === undefined) {
      chip.classList.add('badge-fresh-ok');
      chip.textContent = 'AI 額度 無上限';
      return;
    }
    const rNum = Number(remaining);            // Decimal string → number for compare only
    const tNum = threshold == null ? null : Number(threshold);
    const amt = money2(remaining);             // Decimal STRING through fmt
    if (rNum <= 0) {
      chip.classList.add('badge-missing');
      chip.textContent = 'AI 額度 $' + money2(0);
    } else if (tNum !== null && rNum < tNum) {
      chip.classList.add('badge-fresh-stale');
      chip.appendChild(el('span', 'dot'));
      chip.appendChild(document.createTextNode('AI 額度 $' + amt));
    } else {
      chip.textContent = 'AI 額度 $' + amt;
    }
  }
  /* Neutral placeholder until data arrives (the real value comes from the dashboard
     payload or /api/llm/config). If neither is reachable, this is the final state. */
  chip.textContent = 'AI 額度 …';

  /* bell */
  const bellWrap = el('div', 'bell-wrap');
  const bell = el('button', 'bell-btn');
  bell.type = 'button';
  bell.title = '風險預警（規則引擎，每次資料更新時重算）';
  bell.appendChild(el('span', 'bell-ico', '⚠'));
  bellWrap.appendChild(bell);
  /* The panel is PORTALED to <body>, NOT nested in the topbar (2026-07-07 iPhone fix):
     .topbar carries backdrop-filter, and per the CSS spec (enforced by Safari/iOS) a
     backdrop-filter ancestor becomes the CONTAINING BLOCK for position:fixed
     descendants — a fixed panel inside the topbar mispositions/clips on real iOS
     Safari even though desktop Chromium renders it fine. With <body> as the parent no
     ancestor filter/transform can hijack its coordinates. Geometry (top/left/right)
     is set from the bell's rect on open + on resize; skin/sizing stay in styles.css. */
  const panel = el('div', 'bell-panel');
  panel.hidden = true;
  document.body.appendChild(panel);

  function positionPanel() {
    const r = bell.getBoundingClientRect();
    panel.style.top = Math.round(r.bottom + 8) + 'px';
    if (window.matchMedia('(max-width: 640px)').matches) {
      /* mobile: pin to the viewport edges so the whole list is on screen */
      panel.style.left = '8px';
      panel.style.right = '8px';
    } else {
      /* Anchor by LEFT — the only offset that shares the bell rect's client
         coordinate space. A `right` offset resolves against the fixed-position
         viewport, whose width differs from clientWidth/innerWidth when the root's
         scrollbar-gutter reserves space (measured live: ICB 1425 vs both 1440),
         which skewed the panel by the gutter width. Called with the panel already
         unhidden so its CSS width (380px) is measurable. */
      panel.style.right = 'auto';
      const w = panel.getBoundingClientRect().width || 380;
      panel.style.left = Math.max(8, Math.round(r.right - w)) + 'px';
    }
  }

  function renderCount() {
    const old = bell.querySelector('.bell-count');
    if (old) old.remove();
    if (!alerts.length) return;
    /* 3A: the badge (dot) shows ONLY while at least one current alert id is UNSEEN.
       The numeric badge keeps the TOTAL count + the existing risk coloring; it simply
       hides once every current id has been seen (panel opened), and reappears when a new
       alert id arrives. */
    const seen = new Set(seenIds);
    const hasUnseen = alerts.some((a) => !a || !a.id || !seen.has(a.id));
    if (!hasUnseen) return;
    const riskCount = alerts.filter((a) => a.sev === 'risk').length;
    bell.appendChild(el('span', 'bell-count' + (riskCount ? ' risk' : ''), String(alerts.length)));
  }

  function fillPanel() {
    panel.replaceChildren();
    const head = el('div', 'bell-head');
    head.appendChild(el('span', null, '風險預警'));
    head.appendChild(el('span', 'bell-sub', '規則引擎・非 AI 生成'));
    panel.appendChild(head);
    if (!alerts.length) {
      panel.appendChild(el('div', 'bell-empty', '目前無預警事項'));
      return;
    }
    alerts.forEach((a) => {
      const mapped = mapAlertHref(a.href);
      const item = el('a', 'bell-item sev-' + a.sev);
      item.href = mapped.href || '#';
      /* 含個股代號的預警 → 就地彈出抽屜，不跳轉（保留 href 供中鍵/可及性） */
      if (mapped.sym && window.pdOpenSymbol) {
        item.addEventListener('click', (ev) => {
          ev.preventDefault();
          panel.hidden = true;
          window.pdOpenSymbol(mapped.sym);
        });
      }
      item.appendChild(el('span', 'sev-dot'));
      const txt = el('div', 'bell-txt');
      txt.appendChild(el('div', 'bell-title', a.title));
      txt.appendChild(el('div', 'bell-detail', a.detail));
      item.appendChild(txt);
      panel.appendChild(item);
    });
  }

  renderCount();
  fillPanel();

  bell.addEventListener('click', (e) => {
    e.stopPropagation();
    const opening = panel.hidden;
    panel.hidden = !panel.hidden;
    /* position AFTER unhiding (layout exists → width measurable); JS runs to
       completion before the browser paints, so no mispositioned frame shows. */
    if (opening) {
      positionPanel();
      /* opening the panel = the user has SEEN the current alerts → clear the dot (3A). */
      markCurrentSeen();
      renderCount();
    }
  });
  window.addEventListener('resize', () => {
    if (!panel.hidden) positionPanel();
  });
  if (document.fonts && document.fonts.addEventListener) {
    /* The FIRST open can lazily load the mono font face the panel items use, which
       re-metrics the topbar (its clock/chips share that face) and shifts the bell
       ~15px after the panel was anchored. Re-anchor when a font load settles. */
    document.fonts.addEventListener('loadingdone', () => {
      if (!panel.hidden) positionPanel();
    });
  }
  document.addEventListener('click', (e) => {
    /* panel lives on <body> now — clicks inside it must not count as "outside" */
    if (!panel.hidden && !bellWrap.contains(e.target) && !panel.contains(e.target)) {
      panel.hidden = true;
    }
  });

  /* 跨分頁同步：dashboard 重新整理時寫入 pd_alerts_cache；其他已開啟的非 dashboard
     分頁透過 storage 事件即時跟上（無需重新整理）。dashboard 以後端 D.alerts 為準。 */
  window.addEventListener('storage', (e) => {
    /* 3A: a seen-set change in another tab (panel opened there) clears/relights the dot
       here — on EVERY page, dashboard included. */
    if (e.key === SEEN_KEY) {
      seenIds = loadSeen();
      renderCount();
      return;
    }
    if (isDashboard || e.key !== 'pd_alerts_cache') return;
    try { alerts = JSON.parse(e.newValue || '[]'); } catch (err) { alerts = []; }
    if (!Array.isArray(alerts)) alerts = [];
    window.PD_ALERTS = alerts;
    renderCount();
    fillPanel();
  });

  if (spacer && spacer.nextSibling) {
    tb.insertBefore(chip, spacer.nextSibling);
    tb.insertBefore(bellWrap, chip);
  } else {
    tb.appendChild(bellWrap);
    tb.appendChild(chip);
  }

  /* ---- dashboard boot: consume the SAME shared /api/dashboard payload (Task 2.2) ---- */
  /* On the dashboard, fill the bell + chip from backend data: D.alerts (the embedded
     rule-engine output — no client recompute) and D.llm_quota.remaining_usd (Decimal
     string). Cache the alerts so other open pages' bells stay in sync. On failure
     (non-401; api.js handles 401) leave the placeholder — never throw (the e2e smoke
     asserts ZERO console errors / pageerrors). */
  function bootDashboard() {
    (async function bootDashboardAlerts() {
      let D;
      try {
        D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
      } catch (e) {
        return;  // app.js surfaces the load-failure UI; bell/chip stay in their default state.
      }
      alerts = Array.isArray(D.alerts) ? D.alerts : [];
      window.PD_ALERTS = alerts;
      try { localStorage.setItem('pd_alerts_cache', JSON.stringify(alerts)); } catch (e) { /* noop */ }
      const quota = D.llm_quota || {};
      renderQuotaChip(quota.remaining_usd, null, quota.ai_active);
      renderCount();
      fillPanel();
    })();
  }

  /* ---- non-dashboard boot: read /api/alerts (bell) + /api/llm/config (chip) ---- */
  /* Async + try/catch: a missing/failed fetch degrades to the default empty bell +
     placeholder chip. pdApi may be absent on a few thin pages (no api.js loaded) — skip
     the fetches there rather than throw. */
  function bootOffDashboard() {
    if (!window.pdApi) return;  // thin page without api.js — keep the empty/placeholder UI
    (async function bootAlerts() {
      try {
        const res = await window.pdApi.get('/api/alerts');
        alerts = res && Array.isArray(res.alerts) ? res.alerts : [];
        window.PD_ALERTS = alerts;
        try { localStorage.setItem('pd_alerts_cache', JSON.stringify(alerts)); } catch (e) { /* noop */ }
        renderCount();
        fillPanel();
      } catch (e) { /* degrade: keep the empty bell */ }
    })();
    (async function bootQuota() {
      try {
        const cfg = await window.pdApi.get('/api/llm/config');
        const quota = (cfg && cfg.quota) || {};
        renderQuotaChip(quota.remaining_usd, quota.alert_threshold_usd, quota.ai_active);
      } catch (e) { /* degrade: keep the placeholder chip */ }
    })();
  }

  if (isDashboard) bootDashboard();
  else bootOffDashboard();
})();
