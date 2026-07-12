/* portfolio-dash — 通知通道設定 (WP 3B). Wired to /api/notify/*.

   Boot: GET /api/notify/config -> { ntfy, telegram, email, quiet_hours,
   subscriptions:{ruleId:bool}, rule_catalog:[{id,label,severity}] }. Secrets arrive
   MASKED (token_masked / bot_token_masked / password_masked) with a *_set flag; the
   masked string is pre-filled into the secret input so an unchanged save round-trips the
   mask -> the backend keeps the existing value (placeholder-preserving, mirrors the LLM
   key convention). In PROTECTED mode the ntfy TOPIC is shown in full (it is the read
   secret you copy to the phone). All writes go through pdApi; success -> toast +
   re-fetch; PdApiError -> toast(message,'fail',code); every handler is try/caught so a
   failure never throws an unhandled rejection. Per-node wiring is guarded (if (node) ...)
   so a missing element on a partially-rendered page is a no-op, not a crash.

   GUEST (demo) mode — security review F1: the backend locks notify writes down
   (PUT/test -> 403) and the GET wire carries topic_masked/topic_set INSTEAD of the full
   topic. This page detects that wire shape and renders an honest read-only state: every
   control disabled, a 示範站不開放通知設定 notice, and the masked topic in the topic
   field. A 403 from a save/test (defense in depth) toasts the same honest message. */
(function () {
  'use strict';
  const api = window.pdApi;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  function _toast(msg, kind, code) { if (window.toast) window.toast(msg, kind, code); }

  /* Field id maps per channel — kept declarative so save/load share one source. */
  const setVal = (id, v) => { const n = $(id); if (n) n.value = v == null ? '' : v; };
  const getVal = (id) => { const n = $(id); return n ? n.value : ''; };
  const setToggle = (id, on) => { const n = $(id); if (n) n.classList.toggle('on', !!on); };
  const getToggle = (id) => { const n = $(id); return !!(n && n.classList.contains('on')); };

  /* A secret input shows the mask when set, empty otherwise. On save we send its raw
     value: the mask (contains •••) => keep; '' => clear; anything else => new secret. */
  function setSecret(id, masked, isSet) { setVal(id, isSet ? (masked || '•••') : ''); }

  let CATALOG = [];  // [{id,label,severity}] from the backend (subscription checkboxes)
  let GUEST = false; // demo lockdown: backend sent topic_masked instead of topic (F1)

  const GUEST_MSG = '示範站不開放通知設定，請於正式站（受保護模式）設定';

  /* Every interactive control of the two notify sections — disabled wholesale in the
     guest demo state so the UI never invites a write the backend will 403. */
  const CONTROL_IDS = [
    'nt-ntfy-enabled', 'nt-ntfy-server', 'nt-ntfy-topic', 'nt-ntfy-topic-copy',
    'nt-ntfy-token', 'nt-ntfy-test', 'nt-ntfy-save',
    'nt-tg-enabled', 'nt-tg-token', 'nt-tg-chat', 'nt-tg-test', 'nt-tg-save',
    'nt-em-enabled', 'nt-em-host', 'nt-em-port', 'nt-em-tls', 'nt-em-user',
    'nt-em-pass', 'nt-em-from', 'nt-em-to', 'nt-em-test', 'nt-em-save',
    'nt-qh-enabled', 'nt-qh-start', 'nt-qh-end', 'nt-prefs-save',
  ];

  function applyGuestState() {
    if (!GUEST) return;
    CONTROL_IDS.forEach((id) => { const n = $(id); if (n) n.disabled = true; });
    document.querySelectorAll('#nt-subs input[type="checkbox"]').forEach((cb) => {
      cb.disabled = true;
    });
    if (!$('nt-demo-note')) {
      const anchor = $('nt-ntfy-server');
      const section = anchor && anchor.closest('section');
      if (section) {
        const note = el('div', 'nt-demo-note', GUEST_MSG + '。');
        note.id = 'nt-demo-note';
        const head = section.querySelector('.panel-head');
        if (head && head.nextSibling) section.insertBefore(note, head.nextSibling);
        else section.insertBefore(note, section.firstChild);
      }
    }
  }

  function fill(cfg) {
    if (!cfg) return;
    const n = cfg.ntfy || {}, t = cfg.telegram || {}, e = cfg.email || {}, q = cfg.quiet_hours || {};
    /* Guest wire shape: no full topic — only topic_masked/topic_set (read secret). */
    GUEST = !('topic' in n) && (('topic_set' in n) || ('topic_masked' in n));
    setToggle('nt-ntfy-enabled', n.enabled);
    setVal('nt-ntfy-server', n.server);
    if (GUEST) setVal('nt-ntfy-topic', n.topic_set ? (n.topic_masked || '•••') : '');
    else setVal('nt-ntfy-topic', n.topic);
    setSecret('nt-ntfy-token', n.token_masked, n.token_set);

    setToggle('nt-tg-enabled', t.enabled);
    setVal('nt-tg-chat', t.chat_id);
    setSecret('nt-tg-token', t.bot_token_masked, t.bot_token_set);

    setToggle('nt-em-enabled', e.enabled);
    setVal('nt-em-host', e.host);
    setVal('nt-em-port', e.port);
    setVal('nt-em-tls', e.tls || 'starttls');
    setVal('nt-em-user', e.username);
    setSecret('nt-em-pass', e.password_masked, e.password_set);
    setVal('nt-em-from', e.from_addr);
    setVal('nt-em-to', e.to_addr);

    setToggle('nt-qh-enabled', q.enabled);
    setVal('nt-qh-start', q.start || '22:00');
    setVal('nt-qh-end', q.end || '08:00');

    CATALOG = cfg.rule_catalog || [];
    renderSubs(cfg.subscriptions || {});
    applyGuestState();
  }

  function renderSubs(subs) {
    const wrap = $('nt-subs');
    if (!wrap) return;
    wrap.replaceChildren();
    CATALOG.forEach((r) => {
      const row = el('label', 'nt-sub');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.dataset.rule = r.id;
      cb.checked = subs[r.id] !== false;  // default subscribed
      row.appendChild(cb);
      row.appendChild(el('span', 'nt-sub-sev sev-' + (r.severity || 'info')));
      row.appendChild(el('span', null, r.label || r.id));
      wrap.appendChild(row);
    });
  }

  async function load() {
    if (!api) return;
    try {
      fill(await api.get('/api/notify/config'));
    } catch (err) {
      _toast(err.message, 'fail', err.code);
    }
  }

  async function put(body) {
    const res = await api.put('/api/notify/config', body);
    fill(res);
    return res;
  }

  /* Per-channel save: only that channel's sub-object is sent (the backend merges). */
  function saveNtfy() {
    return put({ ntfy: {
      enabled: getToggle('nt-ntfy-enabled'),
      server: getVal('nt-ntfy-server').trim() || 'https://ntfy.sh',
      topic: getVal('nt-ntfy-topic').trim(),
      token: getVal('nt-ntfy-token'),
    } });
  }
  function saveTelegram() {
    return put({ telegram: {
      enabled: getToggle('nt-tg-enabled'),
      bot_token: getVal('nt-tg-token'),
      chat_id: getVal('nt-tg-chat').trim(),
    } });
  }
  function saveEmail() {
    return put({ email: {
      enabled: getToggle('nt-em-enabled'),
      host: getVal('nt-em-host').trim(),
      port: Number(getVal('nt-em-port')) || 587,
      tls: getVal('nt-em-tls'),
      username: getVal('nt-em-user').trim(),
      password: getVal('nt-em-pass'),
      from_addr: getVal('nt-em-from').trim(),
      to_addr: getVal('nt-em-to').trim(),
    } });
  }
  function savePrefs() {
    const subs = {};
    document.querySelectorAll('#nt-subs input[type="checkbox"]').forEach((cb) => {
      subs[cb.dataset.rule] = cb.checked;
    });
    return put({
      quiet_hours: {
        enabled: getToggle('nt-qh-enabled'),
        start: getVal('nt-qh-start') || '22:00',
        end: getVal('nt-qh-end') || '08:00',
      },
      subscriptions: subs,
    });
  }

  /* 403 = the guest-mode lockdown (F1): show the honest demo message, not a raw error. */
  function _writeErrToast(err) {
    if (err && err.status === 403) _toast(GUEST_MSG, 'fail', 'forbidden');
    else _toast(err.message, 'fail', err.code);
  }

  async function doSave(fn, okMsg) {
    if (!api) return;
    try { await fn(); _toast(okMsg, 'ok'); }
    catch (err) { _writeErrToast(err); }
  }

  async function doTest(channel) {
    if (!api) return;
    try {
      const res = await api.post('/api/notify/test', { channel: channel });
      if (res && res.ok) _toast('測試訊息已送出', 'ok', res.detail);
      else _toast('測試失敗', 'fail', (res && res.detail) || '');
    } catch (err) {
      _writeErrToast(err);
    }
  }

  function copyTopic() {
    const topic = getVal('nt-ntfy-topic');
    if (!topic) { _toast('尚無主題', 'fail'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(topic).then(
        () => _toast('主題已複製', 'ok'),
        () => _toast('複製失敗，請手動選取', 'fail')
      );
    } else {
      _toast('瀏覽器不支援自動複製，請手動選取', 'fail');
    }
  }

  /* Toggles AUTO-SAVE on click (2026-07-12 field report: a class-flip-only toggle
     looked enabled/disabled but never persisted until 儲存 — "can't turn it off").
     Optimistic flip → minimal PUT {channel:{enabled}} → revert + toast on failure. */
  const TOGGLE_KEY = {
    'nt-ntfy-enabled': 'ntfy',
    'nt-tg-enabled': 'telegram',
    'nt-em-enabled': 'email',
    'nt-qh-enabled': 'quiet_hours',
  };
  Object.keys(TOGGLE_KEY).forEach((id) => {
    const n = $(id);
    if (!n) return;
    n.addEventListener('click', async () => {
      if (GUEST) return;
      const next = !n.classList.contains('on');
      n.classList.toggle('on', next); // optimistic; fill() from the PUT echo confirms
      try {
        await put({ [TOGGLE_KEY[id]]: { enabled: next } });
        _toast(next ? '已啟用' : '已停用', 'ok');
      } catch (err) {
        n.classList.toggle('on', !next); // revert — the server state did not change
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      }
    });
  });

  const wire = (id, fn) => { const n = $(id); if (n) n.addEventListener('click', fn); };
  wire('nt-ntfy-save', () => doSave(saveNtfy, 'ntfy 已儲存'));
  wire('nt-tg-save', () => doSave(saveTelegram, 'Telegram 已儲存'));
  wire('nt-em-save', () => doSave(saveEmail, 'Email 已儲存'));
  wire('nt-prefs-save', () => doSave(savePrefs, '靜音與訂閱已儲存'));
  wire('nt-ntfy-test', () => doTest('ntfy'));
  wire('nt-tg-test', () => doTest('telegram'));
  wire('nt-em-test', () => doTest('email'));
  wire('nt-ntfy-topic-copy', copyTopic);

  load();
})();
