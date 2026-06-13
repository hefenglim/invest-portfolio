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
    { id: 'instruments', href: 'instruments.html',  label: '觀察清單', ico: '◎' },
    { id: 'insights',    href: 'insights.html',     label: 'AI 洞察',  ico: '◈' },
    { id: 'pipeline',    href: 'AI Pipeline Hub.html', label: '洞察管線', ico: '⧉' },
    { id: 'settings',    href: 'settings.html',     label: '系統設定', ico: '⚙' }
  ];
  const LS_KEY = 'pd_sidebar_collapsed';
  const page = document.body.dataset.page || '';

  /* ===== 授權用戶與工作階段（mock：localStorage；後端接線後改 server session） ===== */
  function pdGetUsers() {
    try { return JSON.parse(localStorage.getItem('pd_users') || '[]') || []; } catch (e) { return []; }
  }
  function pdSaveUsers(list) {
    try { localStorage.setItem('pd_users', JSON.stringify(list || [])); } catch (e) { /* noop */ }
  }
  function pdGetSession() {
    try { return JSON.parse(localStorage.getItem('pd_session') || 'null'); } catch (e) { return null; }
  }
  function pdSetSession(s) {
    try {
      if (s) localStorage.setItem('pd_session', JSON.stringify(s));
      else localStorage.removeItem('pd_session');
    } catch (e) { /* noop */ }
  }
  function pdDisplayName() {
    const s = pdGetSession();
    if (s && s.name) return s.name;
    const u = pdGetUsers();
    if (u.length && u[0].name) return u[0].name;
    return '投資人';
  }
  window.pdAuth = {
    getUsers: pdGetUsers, saveUsers: pdSaveUsers,
    getSession: pdGetSession, setSession: pdSetSession, displayName: pdDisplayName
  };

  /* 工作階段守門：
     · 尚無授權用戶 → 自動啟用訪客工作階段，全瀏覽、不鎖定（公開瀏覽不跳登入）。
     · 已有授權用戶 → 需有效登入（非訪客、未鎖定、帳號存在），否則導回登入。 */
  if (page && page !== 'login') {
    const _users = pdGetUsers();
    const _sess = pdGetSession();
    if (_users.length === 0) {
      if (!_sess || !_sess.guest) {
        pdSetSession({ username: null, name: '投資人', guest: true, ts: Date.now() });
      }
    } else {
      const _valid = _sess && !_sess.guest && !_sess.locked &&
        _users.some((u) => u.username === _sess.username);
      if (!_valid) { window.location.replace('login.html'); return; }
    }
  }

  /* ===== 全域個股抽屜：任何頁面點擊代號都「就地」彈出，不再跳轉儀表板 =====
     抽屜本體在 detail.js。非儀表板頁首次呼叫時惰性載入相依檔（echarts／格式／
     mock／history），之後即時開啟；唯有載入失敗才退回深連結導頁。 */
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
  let pdDrawerPromise = null;
  function pdEnsureDrawer() {
    if (window.openSymbolDrawer) return Promise.resolve();
    if (pdDrawerPromise) return pdDrawerPromise;
    pdLoadCss('detail.css');
    const echartsCdn = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
    pdDrawerPromise = (window.echarts ? Promise.resolve() : pdLoadScript(echartsCdn))
      .then(() => (window.fmt ? null : pdLoadScript('format.js')))
      .then(() => (window.DASHBOARD_DATA ? null : pdLoadScript('mock-data.js')))
      .then(() => (window.PD_HISTORY ? null : pdLoadScript('history-mock.js')))
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
  /* known instruments registry (instruments table mirror; 後端接線後由 server 提供) */
  const SYMBOLS = [
    { sym: '2330', name: '台積電', mkt: '台股', held: true },
    { sym: '0056', name: '元大高股息', mkt: '台股', held: true },
    { sym: '00919', name: '群益台灣精選高息', mkt: '台股', held: true },
    { sym: 'AAPL', name: 'Apple', mkt: '美股', held: true },
    { sym: 'MSFT', name: 'Microsoft', mkt: '美股', held: true },
    { sym: 'NVDA', name: 'NVIDIA', mkt: '美股', held: true },
    { sym: '1155.KL', name: 'Maybank', mkt: '馬股', held: true },
    { sym: '6488', name: '環球晶', mkt: '台股', held: false },
    { sym: '8069', name: '元太', mkt: '台股', held: false }
  ];
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

  /* ---- topbar ---- */
  const tb = document.getElementById('topbar');
  if (tb) {
    let titleText = document.body.dataset.title || '';
    if (page === 'dashboard') {
      const hr = new Date().getHours();
      const greet = hr < 5 ? '夜深了' : hr < 11 ? '早安' : hr < 18 ? '午安' : '晚安';
      titleText = greet + '，' + pdDisplayName();
    }
    tb.appendChild(el('h1', 'page-title', titleText));
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
      menu.appendChild(mkOpt('更新報價', '報告模式：抓取最新報價與匯率，重新產出快照', () => {
        if (window.toast) {
          window.toast('已觸發報價更新', 'ok', '排程 quotes_* 立即執行（設計預覽 — 後端接線後生效）');
        }
      }));
      menu.appendChild(mkOpt('重算（重建統計）', '由四帳本完整重建所有統計 — 較耗時', () => {
        if (window.confirmDialog) {
          window.confirmDialog({
            title: '重算（重建統計）',
            body: '將由期初庫存、交易、股利、換匯四帳本完整重建所有持倉與報酬統計。帳本本身不會被修改。',
            confirmLabel: '開始重算',
            onConfirm: () => { if (window.toast) window.toast('重算已開始', 'ok', '完成後儀表板將自動更新（設計預覽）'); }
          });
        }
      }));
      btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; });
      document.addEventListener('click', (e) => { if (!menu.hidden && !wrap.contains(e.target)) menu.hidden = true; });
      wrap.appendChild(btn);
      wrap.appendChild(menu);
      tb.appendChild(wrap);
    }
    /* ---- user menu ---- */
    const uwrap = el('div', 'user-wrap');
    const uname = pdDisplayName();
    const initials = (uname || 'PD').trim().slice(0, 2).toUpperCase();
    const av = el('button', 'avatar');
    av.type = 'button';
    av.textContent = initials;
    av.title = '使用者選單';
    const umenu = el('div', 'user-menu');
    umenu.hidden = true;
    const sess0 = pdGetSession();
    const isGuest = !sess0 || sess0.guest;
    const uhead = el('div', 'user-head');
    uhead.appendChild(el('div', 'user-name', uname));
    uhead.appendChild(el('div', 'user-id', isGuest ? '公開瀏覽 · 未啟用帳密保護' : '@' + sess0.username));
    umenu.appendChild(uhead);
    const mkU = (label, sub, fn, danger) => {
      const o = el('button', 'user-opt' + (danger ? ' danger' : ''));
      o.type = 'button';
      o.appendChild(el('span', 'l', label));
      if (sub) o.appendChild(el('span', 's', sub));
      o.addEventListener('click', () => { umenu.hidden = true; fn(); });
      return o;
    };
    if (isGuest) {
      umenu.appendChild(mkU('啟用帳密保護', '新增授權用戶後啟用登入與鎖定', () => {
        window.location.href = 'settings.html#accounts';
      }));
    } else {
      umenu.appendChild(mkU('帳戶與費率', '授權用戶、費率與一般設定', () => {
        window.location.href = 'settings.html#accounts';
      }));
      umenu.appendChild(mkU('鎖定畫面', '保留身分，重新輸入密碼解鎖', () => {
        const s = pdGetSession();
        if (s) { s.locked = true; pdSetSession(s); }
        window.location.href = 'login.html';
      }));
      umenu.appendChild(mkU('登出', '結束工作階段', () => {
        pdSetSession(null);
        window.location.href = 'login.html';
      }, true));
    }
    av.addEventListener('click', (e) => { e.stopPropagation(); umenu.hidden = !umenu.hidden; });
    document.addEventListener('click', (e) => { if (!umenu.hidden && !uwrap.contains(e.target)) umenu.hidden = true; });
    uwrap.appendChild(av);
    uwrap.appendChild(umenu);
    tb.appendChild(uwrap);
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
  }
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openSearch();
    }
  });

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
})();
