/* portfolio-dash — shared app shell: sidebar, topbar, toasts, confirm dialog.
   Usage: <body data-page="dashboard" data-title="儀表板" data-chips="1" data-refresh="1">
   with <div class="shell"><aside id="sidebar"></aside><div class="shell-main">
   <header id="topbar" class="topbar"></header><div class="page">…</div></div></div>.
   Load BEFORE page scripts. */
(function () {
  'use strict';
  const NAV = [
    { id: 'dashboard',   href: 'index.html',       label: '儀表板',   ico: '◫' },
    { id: 'ledger',      href: 'trades.html',       label: '交易帳本', ico: '≣' },
    { id: 'cash',        href: 'cash.html',          label: '資金管理', ico: '＄' },
    { id: 'instruments', href: 'instruments.html',  label: '觀察清單', ico: '◎' },
    { id: 'insights',    href: 'insights.html',     label: 'AI 洞察',  ico: '◈' },
    { id: 'pipeline',    href: 'pipeline-hub.html', label: '洞察管線', ico: '⧉' },
    { id: 'news',        href: 'news.html',          label: '新聞庫',   ico: '⊞' },
    { id: 'settings',    href: 'settings.html',     label: '系統設定', ico: '⚙' }
  ];
  const LS_KEY = 'pd_sidebar_collapsed';
  const page = document.body.dataset.page || '';

  /* Favicon: point every shell-bearing page at favicon.svg so the browser uses it
     instead of requesting the default /favicon.ico (which 404s app-wide). Synchronous
     DOM, no fetch; guarded against double-injection. login.html sets its own <link>. */
  if (!document.querySelector('link[rel="icon"]')) {
    const _fav = document.createElement('link');
    _fav.rel = 'icon';
    _fav.type = 'image/svg+xml';
    _fav.href = 'favicon.svg';
    document.head.appendChild(_fav);
  }

  /* ===== 工作階段：以後端 GET /api/auth/session 為準（war-game Finding 7） =====
     後端回傳三態：
       · {mode:'guest'}                              → 無授權用戶，公開瀏覽，不導頁。
       · {mode:'user', username:'…', name, locked}   → 受保護且已登入。
       · {mode:'user', username:null, …}             → 受保護但未登入，需導回登入。
     同步取得的 window.pdAuth.getSession()/displayName() 在 shell 啟動後以「快取的後端
     工作階段」回答；啟動前回傳安全的訪客預設值，避免相依頁面崩潰。
     ⚠️ 一律經由 window.pdApi（單一 fetch 層），不直接呼叫 fetch。
     授權用戶管理已於 Task 2.7b 接線至後端 /api/users；localStorage 的 getUsers/saveUsers
     僅保留為無害的舊存取器，已非事實來源。 */
  const DEFAULT_NAME = '投資人';
  let _backendSession = { mode: 'guest', username: null, name: DEFAULT_NAME, locked: false };

  function pdGetUsers() {
    try { return JSON.parse(localStorage.getItem('pd_users') || '[]') || []; } catch (e) { return []; }
  }
  function pdSaveUsers(list) {
    try { localStorage.setItem('pd_users', JSON.stringify(list || [])); } catch (e) { /* noop */ }
  }
  function pdGetSession() { return _backendSession; }
  function pdDisplayName() {
    return (_backendSession && _backendSession.name) ? _backendSession.name : DEFAULT_NAME;
  }
  /* true once shell boot has confirmed: no authorized users (public-browse). */
  function pdIsGuest() { return !_backendSession || _backendSession.mode === 'guest'; }
  /* setSession is RETIRED (Task 2.7b): user management (settings-users.js) is now wired to
     the backend (GET/POST/DELETE /api/users) and no longer mutates a client session, so the
     transitional no-op shim is gone. The session is BACKEND-sourced (GET /api/auth/session)
     and read-only from the shell. getUsers/saveUsers remain only as harmless legacy
     accessors (no page uses them as a source of truth anymore). */
  window.pdAuth = {
    getUsers: pdGetUsers, saveUsers: pdSaveUsers,
    getSession: pdGetSession, displayName: pdDisplayName, isGuest: pdIsGuest
  };

  /* api.js (the single fetch layer) may not yet be on the page — no HTML currently
     loads it before shell.js. Lazily ensure it so the shell still routes EVERY call
     through window.pdApi (never raw fetch). Resolves to true if pdApi is available. */
  let _apiPromise = null;
  function pdEnsureApi() {
    if (window.pdApi) return Promise.resolve(true);
    if (!_apiPromise) {
      _apiPromise = pdLoadScript('api.js')
        .then(() => !!window.pdApi)
        .catch(() => false);
    }
    return _apiPromise;
  }

  /* 鎖定畫面 / 登出：經由 pdApi（單一 fetch 層），完成後導回登入。失敗仍導回登入，
     避免使用者卡在「按了登出卻沒走」的狀態（api.js 已負責真正的 401 強制）。 */
  function pdLockAndLeave() {
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.post('/api/auth/lock') : null))
      .catch(() => { /* swallow: still leave to login below */ })
      .then(() => { window.location.href = 'login.html'; });
  }
  function pdLogoutAndLeave() {
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.post('/api/auth/logout') : null))
      .catch(() => { /* swallow: still leave to login below */ })
      .then(() => { window.location.href = 'login.html'; });
  }

  /* Async session guard (replaces the old synchronous localStorage guard).
     · login page: skip entirely (it owns POST /api/auth/login).
     · fetch GET /api/auth/session via pdApi; cache the result.
     · protected + signed-out (mode==='user' && username==null) → redirect to login.
     · guest OR signed-in → stay; api.js's 401 interceptor is the real enforcement
       for protected API calls. A failed session fetch degrades to guest/no-redirect
       so the page still renders (never hard-crash the shell). */
  function pdInitSession() {
    if (page === 'login') return;
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.get('/api/auth/session') : null))
      .then((sess) => {
        if (sess && typeof sess === 'object') {
          _backendSession = {
            mode: sess.mode === 'user' ? 'user' : 'guest',
            username: sess.username != null ? sess.username : null,
            name: sess.name != null ? sess.name : DEFAULT_NAME,
            locked: !!sess.locked
          };
        }
        if (_backendSession.mode === 'user' && _backendSession.username == null) {
          window.location.replace('login.html');
          return;
        }
        _refreshGreeting();
        _refreshUserMenu();
      })
      .catch(() => {
        /* graceful degradation: treat as guest, no redirect — api.js redirects on a
           real 401 from protected calls. Keep the synchronous scaffold as-is. */
      });
  }

  /* ===== 全域個股抽屜：任何頁面點擊代號都「就地」彈出，不再跳轉儀表板 =====
     抽屜本體在 detail.js。非儀表板頁首次呼叫時惰性載入相依檔（api／echarts／格式），
     之後即時開啟；唯有載入失敗才退回深連結導頁。抽屜自行 fetch
     GET /api/symbol/{symbol}/detail + 共用的 /api/dashboard（Task 2.3），不再載入
     mock-data.js／history-mock.js（那些檔案的退役為 Phase-3 的 Task 3.1）。 */
  function pdScriptLoaded(src) {
    const name = src.split('/').pop().split('?')[0];
    return Array.from(document.scripts).some((s) =>
      s.src && s.src.split('/').pop().split('?')[0] === name);
  }
  function pdLoadScript(src) {
    return new Promise((resolve, reject) => {
      if (pdScriptLoaded(src)) return resolve();
      const s = document.createElement('script');
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('load fail: ' + src));
      document.head.appendChild(s);
    });
  }
  function pdLoadCss(href) {
    const has = Array.from(document.querySelectorAll('link[rel="stylesheet"]'))
      .some((l) => l.href && l.href.indexOf(href) >= 0);
    if (has) return;
    const l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = href;
    document.head.appendChild(l);
  }
  /* 「新功能」panel (WP-WN): whatsnew.js has no HTML <script> tag, so the ?v= stamper
     never sees it — stamp it here with the backend version (set by pdInitVersion) so a
     deploy flushes its cache. Loaded on first ✦ click OR by pdInitVersion at boot. */
  let _appVersion = '';
  let _wnPromise = null;
  function pdEnsureWhatsNew() {
    if (window.pdWhatsNew) return Promise.resolve(true);
    if (!_wnPromise) {
      const q = _appVersion ? ('?v=' + _appVersion) : '';
      _wnPromise = pdLoadScript('whatsnew.js' + q)
        .then(() => !!window.pdWhatsNew)
        .catch(() => false);
    }
    return _wnPromise;
  }
  /* small hook so other pages (e.g. settings.html's 版本發佈資訊 button) can lazy-ensure
     whatsnew.js through the same version-stamped loader before calling window.pdWhatsNew. */
  window.pdEnsureWhatsNew = pdEnsureWhatsNew;
  let pdDrawerPromise = null;
  function pdEnsureDrawer() {
    if (window.openSymbolDrawer) return Promise.resolve();
    if (pdDrawerPromise) return pdDrawerPromise;
    pdLoadCss('detail.css');
    const echartsCdn = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
    /* detail.js fetches its own data via window.pdApi, so ensure the fetch layer + format
       + echarts are present; it no longer needs mock-data.js / history-mock.js. */
    pdDrawerPromise = pdEnsureApi()
      .then(() => (window.echarts ? null : pdLoadScript(echartsCdn)))
      .then(() => (window.fmt ? null : pdLoadScript('format.js')))
      .then(() => pdLoadScript('detail.js'));
    return pdDrawerPromise;
  }
  /* 全域入口：所有頁面的代號點擊都走這裡 */
  window.pdOpenSymbol = function (symbol) {
    if (!symbol) return;
    if (window.openSymbolDrawer) { window.openSymbolDrawer(symbol); return; }
    const fallback = 'index.html#sym=' + encodeURIComponent(symbol);
    pdEnsureDrawer().then(() => {
      if (window.openSymbolDrawer) window.openSymbolDrawer(symbol);
      else window.location.href = fallback;
    }).catch(() => { window.location.href = fallback; });
  };
  /* known instruments registry — loaded from the backend (GET /api/instruments).
     The old hardcoded design-mock list is retired (2026-07-02): search now reflects
     the REAL 觀察清單. Cached per page load; degrades to an empty list (search shows
     the register hint) if the API is unreachable. */
  let SYMBOLS = [];
  let _symbolsPromise = null;
  const MKT_LABEL = { TW: '台股', US: '美股', MY: '馬股' };
  function pdLoadSymbols() {
    if (_symbolsPromise) return _symbolsPromise;
    _symbolsPromise = pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.get('/api/instruments') : null))
      .then((resp) => {
        if (resp && Array.isArray(resp.list)) {
          SYMBOLS = resp.list.map((i) => ({
            sym: i.symbol,
            name: i.name || i.symbol,
            mkt: MKT_LABEL[i.market] || i.market || '',
            held: !!i.held
          }));
        }
        return SYMBOLS;
      })
      .catch(() => SYMBOLS);
    return _symbolsPromise;
  }
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ---- sidebar ---- */
  const sb = document.getElementById('sidebar');
  if (sb) {
    sb.className = 'sidebar' + (localStorage.getItem(LS_KEY) === '1' ? ' collapsed' : '');
    const brand = el('div', 'brand');
    brand.innerHTML = 'p<span class="full">ortfolio</span><span class="tld">-dash</span>';
    sb.appendChild(brand);
    sb.appendChild(el('div', 'brand-ver'));  // version tag under the brand; filled by pdInitVersion()

    const mkItem = (item, child) => {
      const a = el('a', 'sb-item' + (item.id === page ? ' active' : ''));
      a.href = item.href;
      if (!child) a.appendChild(el('span', 'ico', item.ico || '·'));
      a.appendChild(el('span', 'label', item.label));
      if (item.badge) a.appendChild(el('span', 'sb-badge', item.badge));
      return a;
    };
    NAV.forEach((item) => {
      if (item.children) {
        const group = el('div', 'sb-group');
        const head = el('a', 'sb-item');
        head.href = item.children[0].href;
        head.appendChild(el('span', 'ico', item.ico));
        head.appendChild(el('span', 'label', item.label));
        group.appendChild(head);
        const kids = el('div', 'sb-children');
        item.children.forEach((c) => kids.appendChild(mkItem(c, true)));
        group.appendChild(kids);
        sb.appendChild(group);
      } else {
        sb.appendChild(mkItem(item));
      }
    });

    sb.appendChild(el('div', 'sb-spacer'));
    const col = el('button', 'sb-collapse', localStorage.getItem(LS_KEY) === '1' ? '»' : '«');
    col.type = 'button';
    col.title = '收合 / 展開側欄';
    col.addEventListener('click', () => {
      const collapsed = sb.classList.toggle('collapsed');
      localStorage.setItem(LS_KEY, collapsed ? '1' : '0');
      col.textContent = collapsed ? '»' : '«';
    });
    sb.appendChild(col);
  }

  /* ---- mobile nav drawer (R7, layout only): hamburger toggles the off-canvas
     sidebar; backdrop / nav click closes. Hidden on desktop via CSS. ---- */
  function pdInitMobileNav(tbEl) {
    if (!sb || !tbEl) return;
    const navBtn = el('button', 'btn-refresh', '☰');
    navBtn.id = 'mobile-nav-btn';
    navBtn.type = 'button';
    navBtn.title = '選單';
    let backdrop = null;
    const closeNav = () => {
      sb.classList.remove('mobile-open');
      if (backdrop) { backdrop.remove(); backdrop = null; }
    };
    navBtn.addEventListener('click', () => {
      if (sb.classList.contains('mobile-open')) { closeNav(); return; }
      sb.classList.add('mobile-open');
      backdrop = el('div', 'nav-backdrop');
      backdrop.addEventListener('click', closeNav);
      document.body.appendChild(backdrop);
    });
    sb.addEventListener('click', (e) => { if (e.target.closest('a')) closeNav(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeNav(); });
    tbEl.insertBefore(navBtn, tbEl.firstChild);
  }

  /* ---- topbar ---- */
  const tb = document.getElementById('topbar');
  /* hooks the async session resolver calls once GET /api/auth/session returns. */
  let _refreshGreeting = function () { /* set below when topbar exists */ };
  let _refreshUserMenu = function () { /* set below when topbar exists */ };
  if (tb) {
    /* dashboard greeting depends on the backend session name; render a placeholder
       ('投資人') synchronously, then update after the async session resolves. */
    const titleEl = el('h1', 'page-title', document.body.dataset.title || '');
    _refreshGreeting = function () {
      if (page !== 'dashboard') return;
      const hr = new Date().getHours();
      const greet = hr < 5 ? '夜深了' : hr < 11 ? '早安' : hr < 18 ? '午安' : '晚安';
      titleEl.textContent = greet + '，' + pdDisplayName();
    };
    _refreshGreeting();
    tb.appendChild(titleEl);
    if (document.body.dataset.chips === '1') {
      const asof = el('span', 'asof');
      asof.appendChild(el('span', 'label', '資料時間'));
      const v = el('span', 'num'); v.id = 'asof-value';
      asof.appendChild(v);
      tb.appendChild(asof);
      const ccy = el('span', 'badge badge-ccy'); ccy.id = 'report-ccy';
      tb.appendChild(ccy);
      const fresh = el('a', 'badge'); fresh.id = 'fresh-chip'; fresh.href = '#freshness';
      tb.appendChild(fresh);
    }
    tb.appendChild(el('span', 'header-spacer'));
    /* global symbol search (Cmd+K) */
    const searchBtn = el('button', 'search-btn');
    searchBtn.type = 'button';
    searchBtn.title = '搜尋標的（Cmd/Ctrl + K）';
    searchBtn.appendChild(el('span', null, '⌕ 搜尋標的'));
    searchBtn.appendChild(el('kbd', null, '⌘K'));
    searchBtn.addEventListener('click', openSearch);
    tb.appendChild(searchBtn);
    /* ✦ 新功能 panel trigger (WP-WN): lazy-loads whatsnew.js on first click, then opens. */
    const wnBtn = el('button', 'btn-refresh');
    wnBtn.id = 'wn-btn';
    wnBtn.type = 'button';
    wnBtn.title = '新功能';
    /* crisp four-pointed sparkle (the ✦ text glyph rendered blurry at this size). Inline
       SVG, currentColor, aria-hidden; the absolutely-positioned .wn-dot overlays it. */
    wnBtn.innerHTML = '<svg class="wn-ico" viewBox="0 0 24 24" width="15" height="15" '
      + 'fill="currentColor" aria-hidden="true">'
      + '<path d="M12 2 L14.1 9.9 L22 12 L14.1 14.1 L12 22 L9.9 14.1 L2 12 L9.9 9.9 Z"/></svg>';
    wnBtn.addEventListener('click', () => {
      pdEnsureWhatsNew().then((ok) => { if (ok && window.pdWhatsNew) window.pdWhatsNew.open(); });
    });
    tb.appendChild(wnBtn);
    /* theme toggle */
    const themeBtn = el('button', 'btn-refresh');
    themeBtn.type = 'button';
    const setLabel = () => {
      const cur = document.documentElement.dataset.theme || 'dark';
      themeBtn.textContent = cur === 'dark' ? '☾ 深色' : '☀ 淺色';
      themeBtn.title = '切換深色 / 淺色主題';
    };
    setLabel();
    themeBtn.addEventListener('click', () => {
      const next = (document.documentElement.dataset.theme || 'dark') === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('pd_theme', next); } catch (e) { /* noop */ }
      setLabel();
      window.dispatchEvent(new CustomEvent('pd-theme-change', { detail: next }));
    });
    tb.appendChild(themeBtn);
    if (document.body.dataset.refresh === '1') {
      const wrap = el('div', 'refresh-wrap');
      const btn = el('button', 'btn-refresh', '⟳ 重新整理 ▾');
      btn.type = 'button';
      btn.title = '更新報價或重建統計（後端接線後生效）';
      const menu = el('div', 'refresh-menu');
      menu.hidden = true;
      const mkOpt = (label, sub, fn) => {
        const o = el('button', 'refresh-opt');
        o.type = 'button';
        o.appendChild(el('span', 'l', label));
        o.appendChild(el('span', 's', sub));
        o.addEventListener('click', () => { menu.hidden = true; fn(); });
        return o;
      };
      /* 更新報價：POST /api/actions/refresh-quotes（同步執行 quotes_tw/us/my，
         real provider fetch — 約 10 秒）。進度以常駐 toastProgress 顯示（Progress 系統），
         成功後自動重載以顯示新價。 */
      let refreshBusy = false;
      menu.appendChild(mkOpt('更新報價', '報告模式：抓取最新報價與匯率（約 10 秒）', () => {
        if (refreshBusy) return;
        refreshBusy = true;
        const prog = window.toastProgress('報價更新中…', '正在向 TW / US / MY 資料來源抓取最新報價與匯率（約 10 秒）');
        pdEnsureApi()
          .then((ok) => {
            if (!ok) throw new Error('API 層載入失敗');
            return window.pdApi.post('/api/actions/refresh-quotes', {});
          })
          .then((resp) => {
            refreshBusy = false;
            const jobs = (resp && resp.jobs) ? resp.jobs.join(' / ') : 'quotes';
            prog.done('報價更新完成', jobs + ' 已執行，正在重新整理…');
            setTimeout(() => { window.location.reload(); }, 900);
          })
          .catch((e) => {
            refreshBusy = false;
            prog.fail('報價更新失敗', (e && e.message) || '請稍後再試');
          });
      }));
      menu.appendChild(mkOpt('重算（重建統計）', '由四帳本完整重建所有統計 — 較耗時', () => {
        if (window.confirmDialog) {
          window.confirmDialog({
            title: '重算（重建統計）',
            body: '將由期初庫存、交易、股利、換匯四帳本完整重建所有持倉與報酬統計。帳本本身不會被修改。',
            confirmLabel: '開始重算',
            onConfirm: () => {
              const prog = window.toastProgress('重算中…', '正在由四帳本重建所有統計');
              pdEnsureApi()
                .then((ok) => {
                  if (!ok) throw new Error('API 層載入失敗');
                  return window.pdApi.post('/api/actions/recompute');
                })
                .then(() => {
                  prog.done('重算完成', '四帳本重放驗證通過，正在重新整理…');
                  setTimeout(() => { window.location.reload(); }, 900);
                })
                .catch((e) => {
                  prog.fail('重算失敗', (e && e.message) || '請稍後再試');
                });
            }
          });
        }
      }));
      btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; });
      document.addEventListener('click', (e) => { if (!menu.hidden && !wrap.contains(e.target)) menu.hidden = true; });
      wrap.appendChild(btn);
      wrap.appendChild(menu);
      tb.appendChild(wrap);
    }
    /* ---- user menu ---- (rebuilt from the backend session; render guest placeholder
       synchronously, then re-render via _refreshUserMenu once the session resolves) */
    const uwrap = el('div', 'user-wrap');
    const av = el('button', 'avatar');
    av.type = 'button';
    av.title = '使用者選單';
    const umenu = el('div', 'user-menu');
    umenu.hidden = true;
    const mkU = (label, sub, fn, danger) => {
      const o = el('button', 'user-opt' + (danger ? ' danger' : ''));
      o.type = 'button';
      o.appendChild(el('span', 'l', label));
      if (sub) o.appendChild(el('span', 's', sub));
      o.addEventListener('click', () => { umenu.hidden = true; fn(); });
      return o;
    };
    _refreshUserMenu = function () {
      const wasOpen = !umenu.hidden;
      const uname = pdDisplayName();
      av.textContent = (uname || 'PD').trim().slice(0, 2).toUpperCase();
      const guest = pdIsGuest();
      const username = _backendSession && _backendSession.username;
      umenu.replaceChildren();
      const uhead = el('div', 'user-head');
      uhead.appendChild(el('div', 'user-name', uname));
      uhead.appendChild(el('div', 'user-id',
        guest ? '公開瀏覽 · 未啟用帳密保護' : '@' + (username || '')));
      umenu.appendChild(uhead);
      if (guest) {
        umenu.appendChild(mkU('啟用帳密保護', '新增授權用戶後啟用登入與鎖定', () => {
          window.location.href = 'settings.html#accounts';
        }));
      } else {
        umenu.appendChild(mkU('帳戶與費率', '授權用戶、費率與一般設定', () => {
          window.location.href = 'settings.html#accounts';
        }));
        umenu.appendChild(mkU('鎖定畫面', '保留身分，重新輸入密碼解鎖', () => {
          pdLockAndLeave();
        }));
        umenu.appendChild(mkU('登出', '結束工作階段', () => {
          pdLogoutAndLeave();
        }, true));
      }
      umenu.hidden = !wasOpen;
    };
    _refreshUserMenu();
    av.addEventListener('click', (e) => { e.stopPropagation(); umenu.hidden = !umenu.hidden; });
    document.addEventListener('click', (e) => { if (!umenu.hidden && !uwrap.contains(e.target)) umenu.hidden = true; });
    uwrap.appendChild(av);
    uwrap.appendChild(umenu);
    tb.appendChild(uwrap);
    pdInitMobileNav(tb);  // R7: hamburger first-child, CSS-hidden on desktop
  }

  /* ---- global symbol search (Cmd+K) ---- */
  function openSearch() {
    if (document.querySelector('.search-backdrop')) return;
    const backdrop = el('div', 'search-backdrop');
    const box = el('div', 'search-box');
    const inputRow = el('div', 'search-input-row');
    inputRow.appendChild(el('span', 'glyph', '⌕'));
    const input = el('input', 'search-input');
    input.placeholder = '輸入代號或名稱… 例如 2330、Apple';
    input.setAttribute('spellcheck', 'false');
    inputRow.appendChild(input);
    inputRow.appendChild(el('kbd', null, 'esc'));
    box.appendChild(inputRow);
    const list = el('div', 'search-list');
    box.appendChild(list);
    backdrop.appendChild(box);
    document.body.appendChild(backdrop);
    let active = 0;
    let matches = [];
    const dismiss = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
    const go = (m) => {
      dismiss();
      window.pdOpenSymbol(m.sym);
    };
    const render = () => {
      const qq = input.value.trim().toLowerCase();
      matches = SYMBOLS.filter((s) => !qq || s.sym.toLowerCase().includes(qq) || s.name.toLowerCase().includes(qq));
      if (active >= matches.length) active = 0;
      list.replaceChildren();
      if (!matches.length) {
        list.appendChild(el('div', 'search-empty', '查無標的 — 可至「觀察清單」註冊新標的'));
        return;
      }
      matches.forEach((m, i) => {
        const item = el('button', 'search-item' + (i === active ? ' active' : ''));
        item.type = 'button';
        item.appendChild(el('span', 'sym-code', m.sym));
        item.appendChild(el('span', 'sym-name', m.name));
        item.appendChild(el('span', 'search-mkt', m.mkt + (m.held ? '・持倉' : '・觀察')));
        item.addEventListener('click', () => go(m));
        item.addEventListener('mousemove', () => { if (active !== i) { active = i; render(); } });
        list.appendChild(item);
      });
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { dismiss(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, matches.length - 1); render(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
      else if (e.key === 'Enter') { if (matches[active]) go(matches[active]); }
    };
    document.addEventListener('keydown', onKey);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    input.addEventListener('input', () => { active = 0; render(); });
    render();
    input.focus();
    /* real registry loads async on first open; re-render when it arrives */
    pdLoadSymbols().then(() => { if (document.body.contains(backdrop)) render(); });
  }
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openSearch();
    }
  });

  /* ---- global network progress bar (Progress system, 2026-07-02) ----
     Fed by the `pd-net` events api.js dispatches around EVERY pdApi request, so any
     network wait anywhere in the app shows the slim top bar — no per-page wiring.
     A 150ms show-delay keeps sub-perceptual requests from flickering it. */
  const netBar = el('div', 'net-progress');
  netBar.hidden = true;
  document.body.appendChild(netBar);
  let netShowTimer = null;
  document.addEventListener('pd-net', (e) => {
    const pending = (e.detail && e.detail.pending) || 0;
    if (pending > 0) {
      if (netBar.hidden && !netShowTimer) {
        netShowTimer = setTimeout(() => { netBar.hidden = false; netShowTimer = null; }, 150);
      }
    } else {
      if (netShowTimer) { clearTimeout(netShowTimer); netShowTimer = null; }
      netBar.hidden = true;
    }
  });

  /* pdBusy(btn, busyLabel): put an action button into a spinner/disabled busy state;
     returns a restore() fn. Guards double-clicks by construction (disabled while busy). */
  window.pdBusy = function (btn, busyLabel) {
    if (!btn || btn.dataset.pdBusy === '1') return function () {};
    btn.dataset.pdBusy = '1';
    const prev = btn.textContent;
    btn.disabled = true;
    btn.classList.add('is-busy');
    btn.textContent = '';
    btn.appendChild(el('span', 'busy-spin'));
    btn.appendChild(el('span', null, busyLabel || prev));
    return function restore() {
      delete btn.dataset.pdBusy;
      btn.disabled = false;
      btn.classList.remove('is-busy');
      btn.textContent = prev;
    };
  };

  /* ---- toasts ---- */
  const host = el('div', 'toast-host');
  document.body.appendChild(host);
  window.toast = function (msg, kind, sub) {
    const t = el('div', 'toast ' + (kind === 'fail' ? 'toast-fail' : 'toast-ok'));
    t.appendChild(el('span', null, kind === 'fail' ? '✕' : '✓'));
    const txt = el('div');
    txt.appendChild(el('div', 'msg', msg));
    if (sub) txt.appendChild(el('div', 'sub', sub));
    t.appendChild(txt);
    const x = el('button', 'x', '✕');
    x.type = 'button';
    x.addEventListener('click', () => t.remove());
    t.appendChild(x);
    host.appendChild(t);
    if (kind !== 'fail') setTimeout(() => t.remove(), 4200); /* 失敗訊息常駐直到關閉 */
  };

  /* toastProgress(msg, sub): a persistent spinner toast for LONG network operations
     (quote refresh, recompute, history backfill). Stays until the caller settles it:
       const p = window.toastProgress('報價更新中…', '約 10 秒');
       … p.done('報價更新完成', detail) / p.fail('更新失敗', detail) / p.update(msg, sub)
     Settling replaces it with a normal ok/fail toast (ok auto-dismisses). */
  window.toastProgress = function (msg, sub) {
    const t = el('div', 'toast toast-progress');
    t.appendChild(el('span', 'busy-spin'));
    const txt = el('div');
    const mEl = el('div', 'msg', msg);
    txt.appendChild(mEl);
    const sEl = el('div', 'sub', sub || '');
    if (sub) txt.appendChild(sEl);
    t.appendChild(txt);
    host.appendChild(t);
    let settled = false;
    const settle = (kind, msg2, sub2) => {
      if (settled) return;
      settled = true;
      t.remove();
      window.toast(msg2 || msg, kind, sub2);
    };
    return {
      update: (msg2, sub2) => {
        if (settled) return;
        if (msg2) mEl.textContent = msg2;
        if (sub2) { sEl.textContent = sub2; if (!sEl.parentNode) txt.appendChild(sEl); }
      },
      done: (msg2, sub2) => settle('ok', msg2, sub2),
      fail: (msg2, sub2) => settle('fail', msg2, sub2)
    };
  };

  /* ---- confirm dialog ---- */
  window.confirmDialog = function (opts) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', opts.title || '確認'));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    const body = el('div', 'modal-body');
    body.appendChild(el('div', null, opts.body || ''));
    modal.appendChild(body);
    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'btn', '取消'); cancel.type = 'button';
    const ok = el('button', 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary'),
      opts.confirmLabel || '確認');
    ok.type = 'button';
    foot.appendChild(cancel); foot.appendChild(ok);
    modal.appendChild(foot);
    backdrop.appendChild(modal);
    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    cancel.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    ok.addEventListener('click', () => { dismiss(); if (opts.onConfirm) opts.onConfirm(); });
    document.body.appendChild(backdrop);
  };

  /* ---- async session guard (runs LAST, after the synchronous scaffold above is
     rendered: sidebar, topbar, toast, confirmDialog, search, pdOpenSymbol are all
     defined/usable immediately; the session-dependent UI updates after this resolves) */
  pdInitSession();

  /* App build identity — single source: backend GET /api/health
     ({version, commit, release}). Fills the sidebar brand tag (every page) AND the
     settings 一般 read-only row (#gen-version), so both share ONE source. A build whose
     HEAD is not exactly a release tag shows an amber「未發行」suffix on every page, so
     running non-released code (e.g. on prod by mistake) is visible at a glance.
     Non-critical: degrades silently if the health call fails. */
  function pdInitVersion() {
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.get('/api/health') : null))
      .then((h) => {
        if (!h || !h.version) return;
        const commit = h.commit && h.commit !== 'unknown' ? h.commit : '';
        const released = !!h.release && h.release !== 'unreleased';
        const brief = 'v' + h.version + (commit ? ' · ' + commit : '');
        const full = brief + (released ? '（正式發行 ' + h.release + '）' : '（未發行版）');
        document.querySelectorAll('.brand-ver').forEach((n) => {
          n.textContent = brief + (released ? '' : ' · 未發行');
          n.classList.toggle('unreleased', !released);
          n.title = full;
        });
        document.querySelectorAll('#gen-version').forEach((n) => { n.textContent = full; });
        /* 「新功能」badge/panel (WP-WN): stamp whatsnew.js with this version, then init
           (badge dot + arrival flash). Skip on login; non-critical, silent on failure. */
        _appVersion = h.version;
        if (page !== 'login') {
          pdEnsureWhatsNew().then((ok) => {
            if (ok && window.pdWhatsNew) window.pdWhatsNew.init();
          });
        }
      })
      .catch(() => { /* silent: version tag is non-critical */ });
  }
  pdInitVersion();

  /* ---- UI preferences (WPC 2026-07-07): backend-persisted display prefs ----
     window.pdPrefs is readable synchronously by every pager consumer at boot:
     localStorage cache (pd_prefs) gives the pre-fetch value; GET /api/ui-prefs
     converges it (and refreshes the cache). Counts only — never money. */
  window.pdPrefs = { page_size: 50 };
  try {
    const cachedPrefs = JSON.parse(localStorage.getItem('pd_prefs') || 'null');
    if (cachedPrefs && typeof cachedPrefs.page_size === 'number') {
      window.pdPrefs.page_size = cachedPrefs.page_size;
    }
  } catch (e) { /* cache is best-effort */ }
  function pdInitPrefs() {
    if (page === 'login') return;
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.get('/api/ui-prefs') : null))
      .then((p) => {
        if (p && typeof p.page_size === 'number') {
          window.pdPrefs.page_size = p.page_size;
          try {
            localStorage.setItem('pd_prefs', JSON.stringify({ page_size: p.page_size }));
          } catch (e) { /* noop */ }
        }
      })
      .catch(() => { /* silent: prefs default to 50 */ });
  }
  pdInitPrefs();

  /* 待確認匯入 sidebar badge (R6 item 4): pending-count on the 交易帳本 nav item
     so detections are visible from ANY page. Non-critical: silent on failure. */
  function pdInitInboxBadge() {
    if (page === 'login') return;
    pdEnsureApi()
      .then((ok) => (ok ? window.pdApi.get('/api/dividend-inbox/count') : null))
      .then((resp) => {
        const n = resp && resp.count;
        if (!n) return;
        const items = document.querySelectorAll('#sidebar .sb-item');
        for (const a of items) {
          if (a.getAttribute('href') === 'trades.html') {
            const b = el('span', 'sb-badge sb-badge-alert', String(n));
            b.title = n + ' 筆偵測到的配息待確認';
            a.appendChild(b);
            break;
          }
        }
      })
      .catch(() => { /* silent: the badge is a hint, not a gate */ });
  }
  pdInitInboxBadge();
})();
