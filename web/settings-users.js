/* portfolio-dash — 授權用戶管理 (wired to /api/users, spec 9.3 / 19 Task 2.7b).

   可登入本系統的帳號清單；投資人稱號用於儀表板問候。資料來源為後端（伺服器端帶雜湊儲存）：
   - GET    /api/users            列出授權用戶（含 is_current）
   - POST   /api/users            新增用戶（第一位用戶 → 應用由訪客模式翻轉為「受保護模式」）
   - DELETE /api/users/{username} 移除用戶（204）

   設計稿時期的 localStorage 帳密流程（window.pdAuth.getUsers/saveUsers/setSession）已退役：
   工作階段現由後端 GET /api/auth/session 為準（shell.js），本頁不再偽造 client session。
   新增第一位用戶後，僅提示「受保護模式已啟用，下次需登入」— 真正的登入由後端強制。 */
(function () {
  'use strict';
  if (!document.getElementById('users-wrap')) return;
  const api = window.pdApi;
  if (!api) return;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  function _toast(msg, kind, code) { if (window.toast) window.toast(msg, kind, code); }

  let USERS = []; /* [{username, name, created_at, is_current}] from GET /api/users */
  /* M9-06: set once the FIRST user has been created from this (still unauthenticated)
     browser. The app is protected from that instant — every /api/users call now 401s and
     api.js's global interceptor sends the tab to login.html. That enforcement is correct
     and stays exactly as it is; what was missing is the explanation. While locked we do
     NOT re-fetch, so the page stops navigating out from under the user and the panel says
     what happened instead. */
  let LOCKED = false;

  function render() {
    const wrap = $('#users-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    if (!USERS.length) {
      wrap.appendChild(el('div', 'users-empty',
        '尚無授權用戶 — 目前為訪客（公開瀏覽）模式，登入頁接受任意帳號密碼。'
        + '新增第一個用戶後即啟用帳密保護（下次進入需登入）。'));
      return;
    }
    const table = el('table', 'data');
    const thead = el('thead');
    const hr = el('tr');
    ['投資人稱號', '帳號', '建立時間', '狀態', ''].forEach((h, i) => {
      hr.appendChild(el('th', i <= 1 ? 'col-text' : null, h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    const tb = el('tbody');
    USERS.forEach((u) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', u.name || u.username));
      const tdU = el('td', 'col-text');
      tdU.appendChild(el('span', 'cron-code', u.username));
      tr.appendChild(tdU);
      tr.appendChild(el('td', 'num', u.created_at ? String(u.created_at).slice(0, 10) : '—'));
      const tdS = el('td');
      if (u.is_current) {
        tdS.appendChild(el('span', 'pill pill-ok', '目前登入'));
      } else {
        tdS.appendChild(el('span', 'sign-nil', '—'));
      }
      tr.appendChild(tdS);
      const tdX = el('td');
      const rm = el('button', 'btn', '移除');
      rm.type = 'button';
      rm.style.fontSize = '10px';
      rm.style.padding = '1px 8px';
      rm.addEventListener('click', () => confirmRemove(u));
      tdX.appendChild(rm);
      tr.appendChild(tdX);
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
  }

  /* DELETE /api/users/{username} -> re-fetch. Removing the current user ends the session
     server-side (cookie cleared); the next protected call / reload routes to login. */
  function confirmRemove(u) {
    const doit = async () => {
      try {
        await api.del('/api/users/' + encodeURIComponent(u.username));
        _toast('已移除用戶', 'ok', (u.name || u.username));
        if (u.is_current) {
          _toast('已移除目前登入帳號', 'ok', '下次操作將需重新登入');
        }
        await boot();
      } catch (err) {
        _toast((err && err.message) || '移除失敗', 'fail', err && err.code);
      }
    };
    if (window.confirmDialog) {
      window.confirmDialog({
        title: '移除授權用戶',
        body: '確定移除「' + (u.name || u.username) + '」？此帳號將無法再登入本系統。',
        confirmLabel: '移除',
        danger: true,
        onConfirm: doit
      });
    } else {
      doit();
    }
  }

  /* The first user has just been created and this browser has no session yet: GET /api/users
     would 401 and bounce the tab to login.html before any toast could be read. Render the
     explanation in place and let the USER decide when to go and log in. */
  function renderProtectionEnabled(label, username) {
    const wrap = $('#users-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    const box = el('div', 'users-empty');
    box.appendChild(el('div', null,
      '✓ 已啟用帳密保護 — 「' + label + '」已建立，本系統即刻起需要登入。'));
    box.appendChild(el('div', null,
      '此瀏覽器尚未登入，因此用戶清單與新增用戶暫時無法使用（伺服器已開始要求登入，'
      + '這是預期行為）。請以帳號「' + username + '」登入後再回到本頁管理用戶。'));
    const go = el('button', 'btn btn-primary', '前往登入');
    go.type = 'button';
    go.style.marginTop = '8px';
    go.addEventListener('click', () => { window.location.href = 'login.html'; });
    box.appendChild(go);
    wrap.appendChild(box);
  }

  /* POST /api/users -> re-fetch. The FIRST user flips the app guest -> protected mode;
     we tell the user login is now required (NOT a faked client session). */
  async function add() {
    if (LOCKED) return;
    const name = $('#nu-name').value.trim();
    const user = $('#nu-user').value.trim();
    const pass = $('#nu-pass').value;
    if (!user || !pass) {
      _toast('資料不完整', 'fail', '帳號與密碼為必填');
      return;
    }
    const wasEmpty = USERS.length === 0;
    const addBtn = $('#nu-add');
    if (addBtn) addBtn.disabled = true;
    try {
      await api.post('/api/users', { name: name || user, username: user, password: pass });
      $('#nu-name').value = '';
      $('#nu-user').value = '';
      $('#nu-pass').value = '';
      if (wasEmpty) {
        /* M9-06: do NOT boot() here. Protection is live from this instant, this browser has
           no session, so GET /api/users answers 401 and api.js replaces the location with
           login.html — destroying the toast before it can be read and leaving the user with
           「按了新增用戶，畫面莫名跳到登入頁」. The 401 interceptor is the app's enforcement
           point and is deliberately untouched; we simply stop asking a question we know we
           are no longer allowed to ask, and explain instead. */
        LOCKED = true;
        _toast('已啟用帳密保護', 'ok',
          '「' + (name || user) + '」已建立；本系統現為受保護模式，下次進入需以此帳號登入');
        renderProtectionEnabled(name || user, user);
      } else {
        _toast('已新增用戶', 'ok', (name || user) + ' 可登入本系統');
        await boot();
      }
    } catch (err) {
      _toast((err && err.message) || '新增失敗', 'fail', err && err.code);
    } finally {
      /* stays disabled once locked — a second POST would 401 and bounce the tab too */
      if (addBtn) addBtn.disabled = LOCKED;
    }
  }

  /* ===== boot: GET /api/users, then render. Graceful: on failure surface ONE toast and
     render the empty shell (never an unhandled rejection — the e2e smoke asserts ZERO
     console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    try {
      const resp = await api.get('/api/users');
      USERS = Array.isArray(resp) ? resp : [];
    } catch (err) {
      USERS = [];
      _toast('用戶清單載入失敗', 'fail', (err && err.message) || undefined);
    }
    render();
  }

  $('#nu-add').addEventListener('click', add);
  $('#nu-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  boot();
})();
