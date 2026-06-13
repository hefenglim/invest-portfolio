/* portfolio-dash — 授權用戶管理。
   可登入本系統的帳號清單；投資人稱號用於儀表板問候。
   設計稿：存於本機 localStorage（透過 window.pdAuth）；後端接線後改伺服器端帶雜湊儲存。 */
(function () {
  'use strict';
  if (!document.getElementById('users-wrap')) return;
  const A = window.pdAuth;
  if (!A) return;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function render() {
    const wrap = $('#users-wrap');
    wrap.replaceChildren();
    const users = A.getUsers();
    const sess = A.getSession();
    if (!users.length) {
      wrap.appendChild(el('div', 'users-empty',
        '尚無授權用戶 — 目前為首次使用模式，登入頁接受任意帳號密碼。新增第一個用戶後即啟用帳密驗證。'));
      return;
    }
    const table = el('table', 'data');
    const thead = el('thead');
    const hr = el('tr');
    ['投資人稱號', '帳號', '密碼', '狀態', ''].forEach((h, i) => {
      hr.appendChild(el('th', i <= 1 ? 'col-text' : null, h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    const tb = el('tbody');
    users.forEach((u, idx) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', u.name || u.username));
      const tdU = el('td', 'col-text');
      tdU.appendChild(el('span', 'cron-code', u.username));
      tr.appendChild(tdU);
      tr.appendChild(el('td', 'num', '••••••'));
      const tdS = el('td');
      if (sess && sess.username === u.username) {
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
      rm.addEventListener('click', () => confirmRemove(u, idx));
      tdX.appendChild(rm);
      tr.appendChild(tdX);
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
  }

  function confirmRemove(u, idx) {
    const doit = () => {
      const users = A.getUsers();
      users.splice(idx, 1);
      A.saveUsers(users);
      const sess = A.getSession();
      if (sess && sess.username === u.username) A.setSession(null); // 移除自己 → 結束工作階段
      render();
      if (window.toast) window.toast('已移除用戶', 'ok', (u.name || u.username));
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

  function add() {
    const name = $('#nu-name').value.trim();
    const user = $('#nu-user').value.trim();
    const pass = $('#nu-pass').value;
    if (!user || !pass) {
      if (window.toast) window.toast('資料不完整', 'fail', '帳號與密碼為必填');
      return;
    }
    const users = A.getUsers();
    if (users.some((x) => x.username === user)) {
      if (window.toast) window.toast('帳號已存在', 'fail', '請改用其他帳號');
      return;
    }
    users.push({ username: user, password: pass, name: name || user });
    A.saveUsers(users);
    /* 首位授權用戶 → 啟用帳密保護；若目前為訪客則直接以此身分登入，避免被立即導回登入頁 */
    const sess = A.getSession();
    const firstUser = users.length === 1;
    if (firstUser && (!sess || sess.guest)) {
      A.setSession({ username: user, name: name || user, ts: Date.now(), locked: false });
    }
    $('#nu-name').value = '';
    $('#nu-user').value = '';
    $('#nu-pass').value = '';
    render();
    if (window.toast) {
      if (firstUser) window.toast('已啟用帳密保護', 'ok', '目前以「' + (name || user) + '」登入；登出或鎖定後需輸入密碼');
      else window.toast('已新增用戶', 'ok', (name || user) + ' 可登入本系統');
    }
  }

  $('#nu-add').addEventListener('click', add);
  $('#nu-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  render();
})();
