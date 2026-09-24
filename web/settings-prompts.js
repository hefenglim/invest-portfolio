/* portfolio-dash — 設定 · AI 提示詞 (system prompt + vars wired to /api/*, spec 19/06/20).
   定案模型（2026-06-12）：
   - 策略提示詞 = 純設計物件（無排程、無校正掛載），搭配數據變數系統組裝，
     可「預覽提示詞」（POST /api/prompts/preview，真實計算值、不呼叫 LLM）與
     「測試送出」（POST /api/prompts/test，走真實 LiteLLM、費用照記、422/402 錯誤照拋）。
   - 刪除策略 → 檢查組合器引用：被引用則阻擋；未引用則封存（軟刪除）。
   - 洞察類型組合器 = 排程與自我校正的唯一掛載點：系統(可選)＋1..n 策略＋自我校正開關。
     啟動排程 → 週期設定表（與排程工作表共用）→ 寫入排程工作表；刪除組合同步刪排程。
   - 校正提示詞 1:1 掛組合；版本管理器：active=手動選定版，active≠最新版時最新版自動影子評估；
     版本封存制（軟刪除），歸因鏈永不斷。校正產生與回測評分由「AI 大師模型」執行。

   WIRED (Task 2.7b): the global system prompt loads from GET /api/system-prompt and
   saves via PUT /api/system-prompt; the variable registry loads from GET /api/prompt-vars
   (via PD_VARS.load(), with per-var tier-greyout); preview/test hit the real /api/prompts/*
   endpoints. The former window.PROMPTS_DATA + PD_VARS inline mocks are RETIRED. The
   strategy cards / composer / calibration chains remain DESIGN-STAGE objects (no /api/*
   backing yet) — kept inline as local consts, NOT a window global. */

(function () {
  'use strict';
  const V = window.PD_VARS;
  const api = window.pdApi;
  const f = window.fmt;

  /* Strategies load from GET /api/strategy-prompts on boot (wired 2026-07-05 — the
     inline DESIGN-STAGE seeds are retired; the composer/calibration mocks further
     below are still design-stage). */
  const D = {
    /* system_prompt + system_updated_at are filled from GET /api/system-prompt on boot. */
    system_prompt: '',
    system_updated_at: '',
    system_version: null,   // DEF-057: the newest version number (GET /api/system-prompt)
    strategies: [],
    library: [],  // official template rows (W7, AI-D37) — feeds the 同步官方 button
  };
  function _toast(msg, kind, code) { if (window.toast) window.toast(msg, kind, code); }
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ================= 共用 modal ================= */
  function openModal(title, buildBody, wide) {
    const back = el('div', 'pv-backdrop');
    const box = el('div', 'pv-box' + (wide ? ' wide' : ''));
    const head = el('div', 'pv-head');
    head.appendChild(el('span', 'pv-title', title));
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', () => back.remove());
    head.appendChild(x);
    box.appendChild(head);
    const body = el('div', 'pv-body');
    box.appendChild(body);
    buildBody(body, () => back.remove());
    back.appendChild(box);
    back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
    document.body.appendChild(back);
    return back;
  }

  /* 持倉代號（預覽/測試送出時的代入標的選單；正式執行由任務範圍逐檔代入）。 */
  const HELD_SYMBOLS = ['2330', '0056', '00919', 'AAPL', 'MSFT', 'NVDA', '1155.KL'];
  /* ===== async boot: load the variable registry (GET /api/prompt-vars, populates V in
     place) + the global system prompt (GET /api/system-prompt), THEN build the page.
     Graceful: a fetch failure surfaces ONE toast and falls through with whatever loaded
     so the page still renders (never an unhandled rejection — the e2e smoke asserts ZERO
     console errors). Everything below runs inside boot() so V.CATEGORIES is populated. ===== */
  async function boot() {
    try {
      await V.load();              // populates V.CATEGORIES / index from /api/prompt-vars
    } catch (err) {
      _toast('變數總表載入失敗', 'fail', (err && err.message) || undefined);
    }

  /* ================= 系統提示詞 (GET /api/system-prompt) ================= */
  try {
    const sp = await api.get('/api/system-prompt');
    D.system_prompt = (sp && sp.body) || '';
    D.system_updated_at = (sp && sp.updated_at) || '';
    D.system_version = sp && sp.current_version != null ? sp.current_version : null;
  } catch (err) {
    _toast('系統提示詞載入失敗', 'fail', (err && err.message) || undefined);
  }
  try {
    D.strategies = (await api.get('/api/strategy-prompts')) || [];
  } catch (err) {
    _toast('策略提示詞載入失敗', 'fail', (err && err.message) || undefined);
  }
  /* W7 (AI-D37): the library rides boot (the from-library modal fetches lazily, but the
     per-strategy 同步官方 button renders on boot) — a failed fetch just hides the button. */
  try {
    const lib0 = await api.get('/api/prompt-templates');
    D.library = (lib0 && lib0.strategies) || [];
  } catch (err) {
    D.library = [];
  }
  window.pdField.writeIfUntouched($('#sys-prompt'), '', D.system_prompt);   // I-12
  /* DEF-057: the meta line leads with the version, as a strategy card's does (「v3・更新 …」). */
  function sysMetaSet() {
    $('#sys-prompt-meta').textContent =
      (D.system_version != null ? 'v' + D.system_version + '・' : '') +
      '更新 ' + (D.system_updated_at ? f.date(D.system_updated_at) : '—') + '・套用於所有策略提示詞之前';
  }
  sysMetaSet();
  /* save the global system prompt -> PUT /api/system-prompt (toast + restamp meta). */
  const sysSave = document.getElementById('prompts-save');

  /* ================= 策略提示詞卡 ================= */
  const list = $('#tpl-list');

  const hasPerSymbolVars = (text) => V.tokensIn(text).some((tk) => {
    const v = V.find(tk);
    return v && v.scope === '單一標的';
  });
  /* per_symbol 變數的代入標的選擇器（正式執行時由組合範圍逐檔代入，這裡僅供預覽/測試指定） */
  function symbolPicker(onChange) {
    const row = el('div', 'pv-field');
    row.appendChild(el('label', null, '代入標的（本策略含「單一標的」變數）'));
    const sel = el('select', 'select');
    HELD_SYMBOLS.forEach((s) => {
      const o = el('option', null, s);
      o.value = s;
      sel.appendChild(o);
    });
    sel.addEventListener('change', () => onChange(sel.value));
    row.appendChild(sel);
    return { row, sel };
  }

  /* 插入變數輔助列（策略卡與新增策略共用） */
  function varInsertRow(ta) {
    const varRow = el('div', 'tpl-vars');
    varRow.appendChild(el('span', null, '插入變數：'));
    const sel = el('select', 'select tpl-var-sel');
    const opt0 = el('option', null, '選擇數據變數…');
    opt0.value = '';
    sel.appendChild(opt0);
    V.CATEGORIES.forEach((cat) => {
      const og = document.createElement('optgroup');
      og.label = cat.name + (cat.source === 'ingest' ? '（需後端新增）' : '');
      cat.vars.forEach((v) => {
        /* tier-greyout (spec 20.15): a var whose required tier is unavailable is shown
           but DISABLED, with the tier label appended so the user sees why. */
        const tierOk = v.tier_ok !== false;
        const suffix = tierOk ? '' : '（' + (v.tier_label || '方案受限') + '）';
        const o = el('option', null, v.name + '  {{' + v.token + '}}' + suffix);
        o.value = v.token;
        if (!tierOk) o.disabled = true;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.addEventListener('change', () => {
      if (!sel.value) return;
      const ins = '{{' + sel.value + '}}';
      const pos = ta.selectionStart || ta.value.length;
      ta.value = ta.value.slice(0, pos) + ins + ta.value.slice(pos);
      sel.value = '';
      ta.focus();
    });
    varRow.appendChild(sel);
    const catLink = el('button', 'btn-link', '變數總表 ↓');
    catLink.type = 'button';
    catLink.addEventListener('click', () => {
      const p = document.getElementById('vars-panel');
      if (p) {
        p.open = true;
        const top = p.getBoundingClientRect().top + (document.scrollingElement ? document.scrollingElement.scrollTop : 0) - 60;
        window.scrollTo({ top: top, behavior: 'smooth' });
      }
    });
    varRow.appendChild(catLink);
    return varRow;
  }

  /* scope for a prompt body: per_symbol if it references any 單一標的 variable, else portfolio. */
  const scopeOf = (text) => (hasPerSymbolVars(text) ? 'per_symbol' : 'portfolio');

  /* 預覽提示詞 — POST /api/prompts/preview (always 200, real computed values, NO LLM call). */
  function previewPrompt(t, ta) {
    openModal('預覽提示詞 — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '變數已代入目前快照的真實計算值（不呼叫 LLM，零成本）。實際送出 = 系統提示詞 ＋ 本策略；組合器執行時再附加組合的生效校正提示詞。'));
      const out = el('div'); /* re-rendered on each fetch (symbol change) */
      let sym = hasPerSymbolVars(ta.value) ? HELD_SYMBOLS[0] : null;

      const fetchAndRender = async () => {
        out.replaceChildren(el('div', 'pv-testing', '⏳ 載入預覽…'));
        try {
          const resp = await api.post('/api/prompts/preview',
            { body: ta.value, scope: scopeOf(ta.value), symbol: sym });
          out.replaceChildren();
          const sys = el('div', 'pv-section');
          sys.appendChild(el('div', 'pv-sec-label', '系統提示詞'));
          sys.appendChild(el('pre', 'pv-pre', (resp && resp.system_prompt) || ''));
          out.appendChild(sys);
          const st = el('div', 'pv-section');
          st.appendChild(el('div', 'pv-sec-label', '策略提示詞（變數代入後）'));
          st.appendChild(el('pre', 'pv-pre pv-rendered', (resp && resp.rendered) || ''));
          out.appendChild(st);
          /* token-count chip (tokens_used / est_tokens are plain JSON numbers). */
          const meta = el('div', 'pv-cost num',
            '代入變數 ' + ((resp && resp.tokens_used) || 0) + ' 個・估算 ' +
            f.num((resp && resp.est_tokens) || 0) + ' tokens');
          out.appendChild(meta);
          /* unknown / scope-violation diagnostics (preview lists them, never blocks). */
          const unknown = (resp && resp.unknown_tokens) || [];
          const violations = (resp && resp.scope_violations) || [];
          if (unknown.length || violations.length) {
            const warn = el('div', 'pv-toklist');
            warn.appendChild(el('span', 'pv-sec-label',
              '⚠ 送出時會被擋下（unknown：' + unknown.length + '・範圍不符：' + violations.length + '）'));
            unknown.forEach((tk) => {
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = '未知變數';
              warn.appendChild(chip);
            });
            violations.forEach((tk) => {
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = '單一標的變數用於全組合範圍';
              warn.appendChild(chip);
            });
            out.appendChild(warn);
          }
        } catch (err) {
          out.replaceChildren(el('div', 'pv-note', '預覽載入失敗：' + ((err && err.message) || '')));
          _toast((err && err.message) || '預覽載入失敗', 'fail', err && err.code);
        }
      };

      if (hasPerSymbolVars(ta.value)) {
        const pk = symbolPicker((v) => { sym = v; fetchAndRender(); });
        const note = el('div', 'pv-note',
          '正式排程執行時不需選 — 組合範圍為「單一標的」時，系統自動對每檔持倉跑一次並逐檔代入。');
        const wrap = el('div', 'pv-fields');
        wrap.appendChild(pk.row);
        body.appendChild(wrap);
        body.appendChild(note);
      }
      body.appendChild(out);
      fetchAndRender();
    }, true);
  }

  /* 測試送出 — POST /api/prompts/test (real LiteLLM; 422 bad tokens, 402 budget, 409 role).
     cost_usd / quota_remaining arrive as Decimal STRINGS -> rendered via window.fmt only. */
  function testSend(t, ta) {
    openModal('測試送出 — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '以目前快照資料組裝（系統＋本策略），經真實 LiteLLM 送至 Default 模型分析；費用照記入 llm_usage 與額度。不寫入洞察卡。'));
      const out = el('div'); /* re-rendered per run */
      const run = async (sym) => {
        out.replaceChildren(
          el('div', 'pv-testing', '⏳ 送出中…（via LiteLLM' + (sym ? '・代入 ' + sym : '') + '）'));
        try {
          const resp = await api.post('/api/prompts/test',
            { body: ta.value, scope: scopeOf(ta.value), symbol: sym });
          out.replaceChildren();
          const res = el('div', 'pv-section');
          res.appendChild(el('div', 'pv-sec-label',
            '回傳洞察（' + ((resp && resp.model) || 'LLM') + (sym ? '・' + sym : '') + '）'));
          res.appendChild(el('pre', 'pv-pre', (resp && resp.reply) || ''));
          out.appendChild(res);
          /* tokens are plain numbers; cost_usd / quota_remaining are Decimal STRINGS. */
          const cost = el('div', 'pv-cost num',
            '消耗：' + f.num((resp && resp.tokens_in) || 0) + ' tokens in / ' +
            f.num((resp && resp.tokens_out) || 0) + ' out・$' + f.num((resp && resp.cost_usd) || '0', 4) +
            '・已記入額度（剩餘 $' + f.num((resp && resp.quota_remaining) || '0', 2) + '）');
          out.appendChild(cost);
          _toast('測試完成', 'ok',
            t.name + '：費用 $' + f.num((resp && resp.cost_usd) || '0', 4) + ' 已記入 llm_usage');
        } catch (err) {
          out.replaceChildren();
          const note = el('div', 'pv-note', '測試失敗：' + ((err && err.message) || ''));
          out.appendChild(note);
          /* 422 carries per-token issues (unknown_token / scope_violation). */
          const issues = (err && err.issues) || [];
          if (issues.length) {
            const tl = el('div', 'pv-toklist');
            issues.forEach((iss) => {
              const tk = iss && iss.token ? iss.token : '';
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = iss && iss.code === 'scope_violation'
                ? '單一標的變數用於全組合範圍' : '未知變數';
              tl.appendChild(chip);
            });
            out.appendChild(tl);
          }
          _toast((err && err.message) || '測試失敗', 'fail', err && err.code);
        }
      };
      if (hasPerSymbolVars(ta.value)) {
        let sym = HELD_SYMBOLS[0];
        const pk = symbolPicker((v) => { sym = v; run(sym); });
        const wrap = el('div', 'pv-fields');
        wrap.appendChild(pk.row);
        body.appendChild(wrap);
        body.appendChild(out);
        run(sym);
      } else {
        body.appendChild(out);
        run(null);
      }
    }, true);
  }

  function deleteStrategy(t, card) {
    window.confirmDialog({
      title: '封存策略 — ' + t.name,
      body: '封存後從可選清單移除；仍被洞察任務引用時後端會阻擋。歷史洞察仍可反查內文（軟刪除）。',
      confirmLabel: '確認封存', danger: true,
      onConfirm: async () => {
        try {
          await api.del('/api/strategy-prompts/' + t.id);
        } catch (err) {
          _toast((err && err.message) || '無法封存', 'fail', err && err.code);
          return;
        }
        t.archived = true;
        card.remove();
        syncTplEmpty();   /* 封存最後一張卡後回到空狀態，而不是空白 */
        window.toast('已封存', 'ok', t.name + ' 已移出可選清單');
      }
    });
  }

  /* ================= 版本記錄 (DEF-033 strategies · DEF-057 system / news) =================
     Every save keeps a version; the owner can read any version, see what changed, and
     restore ANY version. The history, the zh source labels and the line diff all come from
     the server; this modal renders them and computes nothing. 回復此版 = POST …/{id}/restore,
     which the server records as a NEW version (history is never rewritten), so a restore can
     itself be undone from the same list.

     ONE modal for every versioned prompt (DEF-057 generalised DEF-033's): `o` is the
     prompt's adapter —
       title / applies   the modal title and the 「…下次執行即使用回復後的內文」 sentence
       writeDoors        the prompt's other write doors, for the note (同步官方 / 重置回官方版)
       dirtyWhere        where unsaved edits live (此卡片 / 此提示詞), for the confirm text
       list() / one(id) / diff(id, against) / restore(id)
                         the prompt's own version routes (GET history · GET one · GET diff ·
                         POST restore) — each adapter spells its paths out in full, so the
                         route-caller contract test can see every call it makes
       liveText()        the text in the prompt's textarea NOW (I-12: a reply that lands
                         after the owner kept typing never overwrites the typing)
       storedText()      the body as last saved
       applyRestore(resp, bodyAtConfirm) → { name, version } after a successful restore */
  const tplMetaText = (t) =>
    (t.current_version != null ? 'v' + t.current_version + '・' : '') +
    '更新 ' + f.date(t.updated_at);

  function openVersionHistory(o) {
    openModal('版本記錄 — ' + o.title, (body) => {
      body.appendChild(el('div', 'pv-note',
        '每次儲存、' + o.writeDoors + '或回復都會留下一版。回復＝以選定版本的內文另存為新版本，舊版本不會被改寫，隨時可再回復。'));
      const listBox = el('div', 'ver-list');
      /* The restore confirmation lives INSIDE this modal, not in confirmDialog: that dialog
         stacks at z 72 and this settings modal at 80 (styles.css overlay ladder), so a
         confirmDialog opened from here would sit behind it, unclickable. Inline also lets
         the owner read the diff of exactly what the restore changes while deciding. */
      const confirmBox = el('div', 'ver-confirm');
      confirmBox.hidden = true;
      const diffBox = el('div', 'ver-diff');
      body.appendChild(listBox);
      body.appendChild(confirmBox);
      body.appendChild(diffBox);

      /* 檢視: one version's full text (GET base + id). */
      const showBody = async (v) => {
        diffBox.replaceChildren(el('div', 'pv-testing', '⏳ 載入內文…'));
        try {
          const full = await o.one(v.id);
          diffBox.replaceChildren();
          diffBox.appendChild(el('div', 'pv-sec-label ver-diff-head',
            'v' + full.version + ' 內文（' + f.datetime(full.saved_at) + '・' + full.source_label + '）'));
          diffBox.appendChild(el('pre', 'pv-pre ver-body-pre', full.body || ''));
        } catch (err) {
          diffBox.replaceChildren(el('div', 'pv-note', '內文載入失敗：' + ((err && err.message) || '')));
          _toast((err && err.message) || '內文載入失敗', 'fail', err && err.code);
        }
      };

      const showDiff = async (v, against) => {
        diffBox.replaceChildren(el('div', 'pv-testing', '⏳ 載入差異…'));
        try {
          const d = await o.diff(v.id, against);
          diffBox.replaceChildren();
          const head = el('div', 'pv-sec-label ver-diff-head');
          const fromTxt = d.from ? 'v' + d.from.version : '（無前一版）';
          head.textContent = '差異 ' + fromTxt + ' → v' + d.to.version + '：' +
            (d.identical ? '兩版內容相同' : '＋' + d.added + ' 行・－' + d.removed + ' 行');
          diffBox.appendChild(head);
          const pre = el('pre', 'pv-pre ver-diff-pre');
          (d.lines || []).forEach((ln) => {
            const mark = ln.op === 'add' ? '+ ' : ln.op === 'del' ? '- ' : '  ';
            pre.appendChild(el('span', 'ver-line ver-' + ln.op, mark + ln.text));
          });
          diffBox.appendChild(pre);
        } catch (err) {
          diffBox.replaceChildren(el('div', 'pv-note', '差異載入失敗：' + ((err && err.message) || '')));
          _toast((err && err.message) || '差異載入失敗', 'fail', err && err.code);
        }
      };

      const restore = (v) => {
        const dirty = o.liveText() !== o.storedText();
        showDiff(v, 'current');   // what the restore changes, on screen while deciding
        confirmBox.replaceChildren();
        confirmBox.appendChild(el('div', 'ver-confirm-text',
          '回復至 v' + v.version + '：將以 v' + v.version + ' 的內文另存為新版本；目前內容仍保留在版本記錄中，可再回復。' +
          o.applies +
          (dirty ? o.dirtyWhere + '有尚未儲存的修改，回復後將以 v' + v.version + ' 取代。' : '')));
        const acts = el('div', 'ver-confirm-acts');
        const cancel = el('button', 'btn btn-sm', '取消');
        cancel.type = 'button';
        cancel.addEventListener('click', () => { confirmBox.hidden = true; });
        const ok = el('button', 'btn btn-sm btn-primary ver-confirm-ok', '確認回復至 v' + v.version);
        ok.type = 'button';
        ok.addEventListener('click', () => doRestore(v, ok));
        acts.appendChild(cancel);
        acts.appendChild(ok);
        confirmBox.appendChild(acts);
        confirmBox.hidden = false;
      };

      const doRestore = async (v, okBtn) => {
        okBtn.disabled = true;
        const bodyAtConfirm = o.liveText();   // I-12: the text the restore replaces
        let resp;
        try {
          resp = await o.restore(v.id);
        } catch (err) {
          okBtn.disabled = false;
          _toast((err && err.message) || '回復失敗', 'fail', err && err.code);
          return;
        }
        confirmBox.hidden = true;
        const done = o.applyRestore(resp || {}, bodyAtConfirm);
        if (resp && resp.changed) {
          _toast('已回復至 v' + v.version, 'ok',
            done.name + '：另存為 v' + done.version + '，下次執行生效');
        } else {
          _toast('內容與目前相同', 'ok', 'v' + v.version + ' 與目前內文一致，未產生新版本');
        }
        diffBox.replaceChildren();
        load();
      };

      const load = async () => {
        listBox.replaceChildren(el('div', 'pv-testing', '⏳ 載入版本記錄…'));
        let resp;
        try {
          resp = await o.list();
        } catch (err) {
          listBox.replaceChildren(el('div', 'pv-note', '版本記錄載入失敗：' + ((err && err.message) || '')));
          _toast((err && err.message) || '版本記錄載入失敗', 'fail', err && err.code);
          return;
        }
        listBox.replaceChildren();
        const versions = (resp && resp.versions) || [];
        if (!versions.length) {
          listBox.appendChild(el('div', 'pv-note', '尚無版本記錄'));
          return;
        }
        versions.forEach((v) => {
          const row = el('div', 'ver-row' + (v.is_current ? ' is-current' : ''));
          row.dataset.version = String(v.version);
          const no = el('span', 'ver-no', 'v' + v.version);
          row.appendChild(no);
          if (v.is_current) row.appendChild(el('span', 'badge ver-current', '目前'));
          row.appendChild(el('span', 'ver-time', f.datetime(v.saved_at)));
          row.appendChild(el('span', 'ver-src',
            v.source_label + (v.restored_from != null ? '（回復自 v' + v.restored_from + '）' : '')));
          row.appendChild(el('span', 'ver-size', f.num(v.lines, 0) + ' 行'));
          const acts = el('span', 'ver-acts');
          const mk = (label, fn, cls) => {
            const b = el('button', 'btn btn-sm' + (cls ? ' ' + cls : ''), label);
            b.type = 'button';
            b.addEventListener('click', fn);
            acts.appendChild(b);
            return b;
          };
          mk('檢視', () => showBody(v));
          if (v.version > 1) mk('對照前一版', () => showDiff(v, 'previous'));
          if (!v.is_current) {
            mk('對照目前', () => showDiff(v, 'current'));
            mk('回復此版', () => restore(v), 'btn-primary ver-restore');
          }
          row.appendChild(acts);
          listBox.appendChild(row);
        });
      };
      load();
    }, true);
  }

  /* The strategy adapter (DEF-033). `t._live` = { card, ta, meta } of the card instance
     currently in the list: a restore re-renders the card, so the modal must never hold on
     to the textarea it opened from. */
  function versionHistory(t) {
    openVersionHistory({
      title: t.name,
      writeDoors: '同步官方',
      applies: '引用此策略的洞察任務下次執行即使用回復後的內文。',
      dirtyWhere: '此卡片',
      list: () => api.get('/api/strategy-prompt-versions', { strategy_id: t.id }),
      one: (id) => api.get('/api/strategy-prompt-versions/' + id),
      diff: (id, against) => api.get('/api/strategy-prompt-versions/' + id + '/diff',
        { against: against }),
      restore: (id) => api.post('/api/strategy-prompt-versions/' + id + '/restore'),
      liveText: () => t._live.ta.value,
      storedText: () => t.body,
      applyRestore: (resp, bodyAtConfirm) => {
        const sp = resp.strategy || {};
        t.body = sp.body != null ? sp.body : t.body;
        t.updated_at = sp.updated_at || t.updated_at;
        t.current_version = resp.current_version != null
          ? resp.current_version : t.current_version;
        /* a reply that lands after the owner kept typing never overwrites the typing: the
           card is re-rendered only when the restored text actually went in. */
        if (window.pdField.writeIfUntouched(t._live.ta, bodyAtConfirm, t.body)) {
          const old = t._live.card;
          const wasOpen = old.classList.contains('open');
          const fresh = addStrategyCard(t);   // re-checks the scope badge + 同步官方
          old.replaceWith(fresh);
          fresh.classList.toggle('open', wasOpen);
        } else {
          t._live.meta.textContent = tplMetaText(t);
        }
        return { name: t.name, version: t.current_version };
      }
    });
  }

  function addStrategyCard(t) {
    const card = el('div', 'tpl-card');
    const head = el('div', 'tpl-head');
    head.appendChild(el('span', 'tpl-name', t.name));
    const perSym = hasPerSymbolVars(t.body);
    const scopeBadge = el('span', 'tpl-scope ' + (perSym ? 'scope-sym' : 'scope-pf'), perSym ? '單一標的' : '全組合');
    scopeBadge.title = perSym
      ? '含「單一標的」變數 — 只能被範圍為「單一標的」的洞察類型引用'
      : '僅使用全組合變數 — 任何範圍的洞察類型皆可引用';
    head.appendChild(scopeBadge);
    const metaEl = el('span', 'tpl-meta', tplMetaText(t));
    head.appendChild(metaEl);
    const right = el('span', 'right');
    if (!t.enabled) right.appendChild(el('span', 'pill pill-off', '停用'));
    const tg = el('button', 'toggle' + (t.enabled ? ' on' : ''));
    tg.type = 'button';
    tg.setAttribute('role', 'switch');
    tg.title = '啟用/停用此策略：停用後，引用它的洞察類型下次執行時跳過此策略段（其餘策略照常），新組合也不可選用；不影響歷史洞察';
    tg.addEventListener('click', async () => {
      const on = !tg.classList.contains('on');
      try {
        await api.put('/api/strategy-prompts/' + t.id,
          { name: t.name, body: t.body, enabled: on });
      } catch (err) {
        _toast((err && err.message) || '切換失敗', 'fail', err && err.code);
        return;
      }
      t.enabled = on;
      tg.classList.toggle('on', on);
      window.toast(on ? '策略已啟用' : '策略已停用', 'ok',
        on ? t.name + '：引用此策略的洞察任務恢復執行此段'
           : t.name + '：引用此策略的洞察任務將跳過此段，新組合不可選用');
    });
    right.appendChild(tg);
    head.appendChild(right);
    card.appendChild(head);

    const body = el('div', 'tpl-body');
    const ta = el('textarea', 'input');
    ta.rows = 4;
    ta.value = t.body;
    body.appendChild(ta);
    t._live = { card: card, ta: ta, meta: metaEl };   // DEF-033: what 版本記錄 writes back to

    /* 插入變數（讀取數據變數總表） */
    body.appendChild(varInsertRow(ta));

    const actions = el('div', 'tpl-actions');
    const mkBtn = (label, cls, fn, title) => {
      const b = el('button', 'btn' + (cls ? ' ' + cls : ''), label);
      b.type = 'button';
      if (title) b.title = title;
      b.addEventListener('click', fn);
      return b;
    };
    actions.appendChild(mkBtn('儲存', 'btn-primary', async () => {
      if (!ta.value.trim()) {
        window.toast('內文不可為空', 'fail', '請填寫提示詞內文');
        return;
      }
      try {
        const sp = await api.put('/api/strategy-prompts/' + t.id,
          { name: t.name, body: ta.value, enabled: t.enabled !== false });
        t.body = (sp && sp.body) || ta.value;
        t.updated_at = (sp && sp.updated_at) || t.updated_at;
        if (sp && sp.current_version != null) t.current_version = sp.current_version;
        metaEl.textContent = tplMetaText(t);   // DEF-033: the save's version number
      } catch (err) {
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
        return;
      }
      /* 重新檢查範圍徽章（變數可能增減） */
      const ps = hasPerSymbolVars(ta.value);
      scopeBadge.textContent = ps ? '單一標的' : '全組合';
      scopeBadge.className = 'tpl-scope ' + (ps ? 'scope-sym' : 'scope-pf');
      scopeBadge.title = ps
        ? '含「單一標的」變數 — 只能被範圍為「單一標的」的洞察類型引用'
        : '僅使用全組合變數 — 任何範圍的洞察類型皆可引用';
      window.toast('已儲存', 'ok', t.name + '：內文已寫入資料庫' +
        (t.current_version != null ? '（v' + t.current_version + '）' : '') + '，下次執行生效');
    }, '寫入資料庫並依最新內文重新檢查範圍徽章'));
    actions.appendChild(mkBtn('預覽提示詞', null, () => previewPrompt(t, ta),
      '變數代入目前快照，檢視實際送出的完整提示詞'));
    actions.appendChild(mkBtn('測試送出', null, () => testSend(t, ta),
      '經 LiteLLM 實際送出一次並回傳洞察結果（費用照記）'));
    /* DEF-033: every saved version, the diff between any two, and 回復此版. */
    actions.appendChild(mkBtn('版本記錄', 'tpl-versions', () => versionHistory(t),
      '檢視每次儲存的版本、比對差異，並可回復任一版'));
    /* W7 (AI-D37): 同步官方 vX — only when the row's name matches an official template
       AND its body has drifted. The overwrite goes through from-template's replace mode;
       every task bound to this strategy id runs the new body on its next pass. */
    const tpl = (D.library || []).find((x) => x.name === t.name);
    if (tpl && tpl.body !== t.body) {
      actions.appendChild(mkBtn('同步官方 ' + tpl.version, null, () => {
        window.confirmDialog({
          title: '同步官方 ' + tpl.version,
          body: '將以官方「' + tpl.name + ' ' + tpl.version + '」覆寫此策略內文；' +
            '目前的內文會保留在「版本記錄」中，可隨時回復。引用此策略的洞察任務下次執行即升版。',
          confirmLabel: '覆寫為官方版', danger: true,
          onConfirm: async () => {
            const bodyAtConfirm = ta.value;   // I-12: the text the overwrite discards
            try {
              const sp = await api.post('/api/strategy-prompts/from-template',
                { name: t.name, mode: 'replace', strategy_id: t.id });
              t.body = (sp && sp.body) || t.body;
              t.updated_at = (sp && sp.updated_at) || t.updated_at;
              if (sp && sp.current_version != null) t.current_version = sp.current_version;
              /* DEF-007 class: a reply that lands after the owner kept typing must not
                 overwrite what they typed — only the text that was there at confirm. */
              window.pdField.writeIfUntouched(ta, bodyAtConfirm, t.body);
              window.toast('已同步官方 ' + tpl.version, 'ok',
                t.name + '：綁定的任務下次執行即使用新版');
            } catch (err) {
              _toast((err && err.message) || '同步失敗', 'fail', err && err.code);
              return;
            }
            /* re-render the card — the bodies now match, so the button disappears */
            card.replaceWith(addStrategyCard(t));
          },
        });
      }, '以官方模板覆寫內文（目前內文保留在版本記錄，可回復；綁定的任務原地升版）'));
    }
    actions.appendChild(mkBtn('封存', 'btn-danger', () => deleteStrategy(t, card),
      '被洞察類型引用時將阻擋'));
    body.appendChild(actions);
    card.appendChild(body);

    head.addEventListener('click', (e) => {
      if (e.target.closest('.toggle')) return;
      card.classList.toggle('open');
    });
    list.appendChild(card);
    syncTplEmpty();
    return card;
  }

  /* M9-05: with no strategy rows the panel rendered a 0px-tall void between its title and
     its two buttons — 「空」 and 「壞掉」 look identical, and this list is empty on every
     fresh install. Same shape and tone as the 授權用戶 empty state on this page. Kept in
     sync from the three places the card count can change (initial render / add / 封存)
     rather than re-rendering the list, so an open card is never collapsed by a sibling. */
  function syncTplEmpty() {
    if (!list) return;
    const existing = list.querySelector('.tpl-empty');
    if (list.querySelector('.tpl-card')) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    list.appendChild(el('div', 'tpl-empty',
      '尚無策略提示詞 — 洞察批次目前只會套用上方的系統提示詞，不會產生任何策略卡片。'
      + '按右上角「新增策略」自訂一則，或「從官方模板庫新增」複製一份可編輯的官方版本。'));
  }

  D.strategies.filter((t) => !t.archived).forEach(addStrategyCard);
  syncTplEmpty();

  /* ＋ 新增策略：表單 → POST /api/strategy-prompts → 推入清單 */
  const tplAdd = document.getElementById('tpl-add');
  if (tplAdd) tplAdd.addEventListener('click', () => {
    openModal('新增策略提示詞', (body, close) => {
      const nameFld = el('div', 'pv-field');
      nameFld.appendChild(el('label', null, '策略名稱'));
      const nameInp = el('input', 'input');
      nameInp.placeholder = '例：買進時機體檢';
      nameFld.appendChild(nameInp);
      body.appendChild(nameFld);
      const bodyFld = el('div', 'pv-field');
      bodyFld.appendChild(el('label', null, '提示詞內文（可用 {{變數}}，見下方變數總表）'));
      const ta2 = el('textarea', 'input');
      ta2.rows = 5;
      ta2.style.width = '100%';
      ta2.placeholder = '根據 {{holdings_json}} 與 {{price_vs_cost_json}}，…';
      bodyFld.appendChild(ta2);
      body.appendChild(bodyFld);
      body.appendChild(varInsertRow(ta2));
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '建立策略');
      ok.type = 'button';
      ok.addEventListener('click', async () => {
        const nm = nameInp.value.trim();
        if (!nm || !ta2.value.trim()) {
          window.toast('請填寫完整', 'fail', '名稱與內文皆為必填');
          return;
        }
        let sp;
        try {
          sp = await api.post('/api/strategy-prompts',
            { name: nm, body: ta2.value.trim(), enabled: true });
        } catch (err) {
          _toast((err && err.message) || '建立失敗', 'fail', err && err.code);
          return;
        }
        D.strategies.push(sp);
        const card = addStrategyCard(sp);
        card.classList.add('open');
        close();
        window.toast('已建立', 'ok', sp.name + '：可在洞察任務中掛載使用');
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    }, true);
  });

  /* 儲存設定：將系統提示詞寫入後端 (PUT /api/system-prompt)；策略卡仍為設計稿（無端點）。 */
  if (sysSave) sysSave.addEventListener('click', async () => {
    const body = $('#sys-prompt').value;
    sysSave.disabled = true;
    try {
      const sp = await api.put('/api/system-prompt', { body: body });
      D.system_prompt = (sp && sp.body) || body;
      D.system_updated_at = (sp && sp.updated_at) || D.system_updated_at;
      const before = D.system_version;
      D.system_version = sp && sp.current_version != null ? sp.current_version : D.system_version;
      sysMetaSet();
      _toast('已儲存', 'ok', D.system_version != null && D.system_version !== before
        ? '系統提示詞已存為 v' + D.system_version + '，下次 AI 呼叫生效'
        : '系統提示詞已更新，下次 AI 呼叫生效');
    } catch (err) {
      _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
    } finally {
      sysSave.disabled = false;
    }
  });

  /* 重置回官方版（官方模板庫 2026-07-05）：POST /api/system-prompt/reset */
  const sysReset = document.getElementById('sys-reset');
  if (sysReset) sysReset.addEventListener('click', () => {
    window.confirmDialog({
      title: '重置系統提示詞',
      body: '將以官方模板庫的最新版本覆蓋目前內容（策略提示詞不受影響）。目前內容會保留在「版本記錄」中，可隨時回復。',
      confirmLabel: '重置回官方版', danger: true,
      onConfirm: async () => {
        try {
          const sysAtConfirm = $('#sys-prompt').value;   // I-12: the text the reset discards
          const sp = await api.post('/api/system-prompt/reset');
          D.system_prompt = (sp && sp.body) || '';
          D.system_updated_at = (sp && sp.updated_at) || '';
          D.system_version = sp && sp.current_version != null ? sp.current_version : D.system_version;
          window.pdField.writeIfUntouched($('#sys-prompt'), sysAtConfirm, D.system_prompt);
          sysMetaSet();
          _toast('已重置', 'ok', '系統提示詞已回到官方版');
        } catch (err) {
          _toast((err && err.message) || '重置失敗', 'fail', err && err.code);
        }
      }
    });
  });

  /* 版本記錄 (DEF-057, owner ruling 2026-09-24): the system prompt's history, in the same
     modal as a strategy's. A restore writes the body back into the textarea only when the
     owner has not typed since confirming (I-12). */
  const sysVersions = document.getElementById('sys-versions');
  if (sysVersions) sysVersions.addEventListener('click', () => openVersionHistory({
    title: '系統提示詞',
    writeDoors: '重置回官方版',
    applies: '所有 AI 呼叫下次即使用回復後的內文。',
    dirtyWhere: '系統提示詞欄位',
    list: () => api.get('/api/system-prompt/versions'),
    one: (id) => api.get('/api/system-prompt/versions/' + id),
    diff: (id, against) => api.get('/api/system-prompt/versions/' + id + '/diff',
      { against: against }),
    restore: (id) => api.post('/api/system-prompt/versions/' + id + '/restore'),
    liveText: () => $('#sys-prompt').value,
    storedText: () => D.system_prompt,
    applyRestore: (resp, bodyAtConfirm) => {
      const sp = resp.prompt || {};
      D.system_prompt = sp.body != null ? sp.body : D.system_prompt;
      D.system_updated_at = sp.updated_at || D.system_updated_at;
      D.system_version = resp.current_version != null ? resp.current_version : D.system_version;
      window.pdField.writeIfUntouched($('#sys-prompt'), bodyAtConfirm, D.system_prompt);
      sysMetaSet();
      return { name: '系統提示詞', version: D.system_version };
    }
  }));

  /* ================= 新聞整理提示詞 (GET/PUT /api/news-prompt · POST reset) =================
     spec docs/spec/2026-09-10-news-prompt-settings.html (owner D1(a)/D2(b)/D3(a)/D4(a)):
     the news pipeline's organizer system prompt, same grammar as the system prompt above.
     The badge answers "is this still the official version" from the BACKEND's is_official —
     the frontend never holds the official body. Blank is refused server-side (422
     news_prompt_empty) and the textarea keeps the user's text. The field reminder is a
     non-blocking hint: the organizer parses {title, news_date, body_summary,
     related_stocks} back out of the model's JSON, so a prompt that stops naming a key will
     fail every article — say so, do not block. */
  const NEWS_FIELDS = ['title', 'news_date', 'body_summary', 'related_stocks'];
  const newsTa = document.getElementById('news-prompt');
  if (newsTa) {
    const newsBadge = $('#news-prompt-badge'), newsMeta = $('#news-prompt-meta');
    const newsSchema = $('#news-prompt-schema');
    const newsSave = $('#news-save'), newsReset = $('#news-reset');
    const N = { body: '', updated_at: '', official_version: '', is_official: true,
      current_version: null };
    const newsBadgeSet = (text, official) => {
      newsBadge.textContent = text;
      newsBadge.className = 'badge prompt-badge ' + (official ? 'is-official' : 'is-custom');
    };
    const newsMetaSet = () => {
      newsMeta.textContent = (N.current_version != null ? 'v' + N.current_version + '・' : '') +
        '更新 ' + (N.updated_at ? f.date(N.updated_at) : '—') +
        '・官方 ' + (N.official_version || '—') + '・每日 06:00 新聞管線與手動抓取共用';
    };
    const newsSchemaCheck = (text) => {
      const missing = NEWS_FIELDS.filter((k) => text.indexOf(k) < 0);
      if (missing.length) {
        newsSchema.textContent = '提示詞未提及欄位 ' + missing.join('、') +
          ' — 整理員回傳的 JSON 需要這四個鍵，缺了文章會整理失敗（不擋儲存，只提醒）';
        newsSchema.hidden = false;
      } else {
        newsSchema.textContent = '';
        newsSchema.hidden = true;
      }
    };
    const newsApply = (wire, sent) => {
      N.body = (wire && wire.body) || '';
      N.updated_at = (wire && wire.updated_at) || '';
      N.official_version = (wire && wire.official_version) || '';
      N.is_official = !!(wire && wire.is_official);
      N.current_version = wire && wire.current_version != null ? wire.current_version : null;
      window.pdField.writeIfUntouched(newsTa, sent === undefined ? '' : sent, N.body);   // I-12
      newsBadgeSet(N.is_official ? '與官方版相同' : '已自訂', N.is_official);
      newsMetaSet();
      newsSchemaCheck(N.body);
    };
    try {
      newsApply(await api.get('/api/news-prompt'));
    } catch (err) {
      _toast('新聞整理提示詞載入失敗', 'fail', (err && err.message) || undefined);
      newsBadgeSet('載入失敗', false);
    }
    newsTa.addEventListener('input', () => {
      const same = newsTa.value === N.body;
      newsBadgeSet(same ? (N.is_official ? '與官方版相同' : '已自訂') : '未儲存的修改',
        same && N.is_official);
      newsSchemaCheck(newsTa.value);
    });
    newsSave.addEventListener('click', async () => {
      const restore = window.pdBusy ? window.pdBusy(newsSave, '儲存中…') : () => {};
      try {
        const sent = newsTa.value;
        newsApply(await api.put('/api/news-prompt', { body: sent }), sent);
        _toast('已儲存', 'ok', '新聞整理提示詞已更新，下次新聞管線執行生效');
      } catch (err) {
        /* 422 news_prompt_empty: the message is the server's zh sentence; the textarea
           keeps whatever the user typed (never cleared on a refusal). */
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      } finally {
        restore();
      }
    });
    /* 版本記錄 (DEF-057): the news-organizer prompt's history, same modal. */
    const newsVersions = $('#news-versions');
    if (newsVersions) newsVersions.addEventListener('click', () => openVersionHistory({
      title: '新聞整理提示詞',
      writeDoors: '重置回官方版',
      applies: '下次新聞管線執行即使用回復後的內文。',
      dirtyWhere: '新聞整理提示詞欄位',
      list: () => api.get('/api/news-prompt/versions'),
      one: (id) => api.get('/api/news-prompt/versions/' + id),
      diff: (id, against) => api.get('/api/news-prompt/versions/' + id + '/diff',
        { against: against }),
      restore: (id) => api.post('/api/news-prompt/versions/' + id + '/restore'),
      liveText: () => newsTa.value,
      storedText: () => N.body,
      applyRestore: (resp, bodyAtConfirm) => {
        if (resp.prompt) newsApply(resp.prompt, bodyAtConfirm);
        return { name: '新聞整理提示詞', version: N.current_version };
      }
    }));
    newsReset.addEventListener('click', () => {
      window.confirmDialog({
        title: '重置新聞整理提示詞',
        body: '將以官方模板庫的最新版本覆蓋目前內容（系統提示詞與策略提示詞不受影響）。目前內容會保留在「版本記錄」中，可隨時回復。',
        confirmLabel: '重置回官方版', danger: true,
        onConfirm: async () => {
          try {
            const sent = newsTa.value;
            newsApply(await api.post('/api/news-prompt/reset'), sent);
            _toast('已重置', 'ok', '新聞整理提示詞已回到官方 ' + (N.official_version || ''));
          } catch (err) {
            _toast((err && err.message) || '重置失敗', 'fail', err && err.code);
          }
        }
      });
    });
  }

  /* 從官方模板庫新增策略副本：GET /api/prompt-templates → POST from-template */
  const tplFromLib = document.getElementById('tpl-from-lib');
  if (tplFromLib) tplFromLib.addEventListener('click', async () => {
    let lib;
    try {
      lib = await api.get('/api/prompt-templates');
    } catch (err) {
      _toast('模板庫載入失敗', 'fail', (err && err.message) || undefined);
      return;
    }
    openModal('官方模板庫 ' + (lib.library_version || ''), (body, close) => {
      (lib.strategies || []).forEach((tpl) => {
        const row = el('div', 'pv-field');
        const head = el('div', null);
        head.appendChild(el('strong', null, tpl.name + '　' + tpl.version +
          '（' + (tpl.scope === 'per_symbol' ? '單一標的' : '全組合') + '）'));
        const btn = el('button', 'btn btn-primary', '新增副本');
        btn.type = 'button';
        btn.style.marginLeft = '8px';
        btn.addEventListener('click', async () => {
          try {
            const sp = await api.post('/api/strategy-prompts/from-template',
              { name: tpl.name });
            D.strategies.push(sp);
            const card = addStrategyCard(sp);
            card.classList.add('open');
            close();
            window.toast('已新增', 'ok', sp.name + '：官方 ' + tpl.version + ' 副本，可自由修改');
          } catch (err) {
            _toast((err && err.message) || '新增失敗', 'fail', err && err.code);
          }
        });
        head.appendChild(btn);
        row.appendChild(head);
        const pre = el('pre', 'pv-pre', tpl.body);
        pre.style.maxHeight = '180px';
        pre.style.overflow = 'auto';
        row.appendChild(pre);
        body.appendChild(row);
      });
    }, true);
  });

  /* mount point — FM8 (2026-07-07): the save bar now lives INSIDE the system-prompt
     panel, so dynamic panels simply append at the page end (vars table, then the
     self-contained 自我進化設定 with its own 儲存進化設定 button). */
  const promptsView = document.getElementById('view-prompts') || document.querySelector('.page');
  if (!promptsView) return;
  const mount = (node) => { promptsView.appendChild(node); };

  /* ================= 數據變數總表 ================= */
  (function () {
    const panel = document.createElement('details');
    panel.className = 'panel freshness';
    panel.id = 'vars-panel';
    panel.dataset.anchor = 'vars';        /* DEF-038: settings.html#prompts/vars lands here */
    const sum = el('summary');
    sum.appendChild(el('span', 'caret', '▶'));
    sum.appendChild(el('span', null, '數據變數總表'));
    const chips = el('span', 'chips sum-chips');
    const nVars = V.all().length;
    const nIngest = V.all().filter((v) => v.source === 'ingest').length;
    const c1 = el('span', 'ccy-chip');
    c1.appendChild(el('b', null, nVars + ' 個變數'));
    chips.appendChild(c1);
    const c2 = el('span', 'ccy-chip');
    c2.appendChild(el('span', null, '其中 '));
    /* DEF-038 residual copy: these variables have been live since spec 20.2 / W3
       (variables.py registers every one `available=True`) — 「待後端新增」 claimed otherwise. */
    c2.appendChild(el('b', null, nIngest + ' 個取自外部資料快照'));
    chips.appendChild(c2);
    sum.appendChild(chips);
    panel.appendChild(sum);

    const wrap = el('div', 'vars-wrap');
    V.CATEGORIES.forEach((cat) => {
      const sec = el('div', 'vars-cat');
      const head = el('div', 'vars-cat-head');
      head.appendChild(el('span', 'vars-cat-name', cat.name));
      head.appendChild(el('span', 'vars-src ' + (cat.source === 'ready' ? 'src-ready' : 'src-ingest'),
        cat.source === 'ready' ? '後端已具備' : '外部資料快照'));
      sec.appendChild(head);
      /* DEF-002: the table scrolls inside its OWN `.table-wrap` (styles.css), like every other
         table on this page — appended bare into the `.vars-cat` flex column it set the
         document width (3,673px at 390px, 3,874px at 1,440px) the moment the panel opened. */
      const tableWrap = el('div', 'table-wrap');
      const table = el('table', 'data vars-table');
      table.innerHTML = '<thead><tr><th class="col-text">變數</th><th class="col-text">名稱</th><th class="col-text">說明</th><th class="col-text">範圍</th><th class="col-text"></th></tr></thead>';
      const tb = el('tbody');
      cat.vars.forEach((v) => {
        /* tier-greyout (spec 20.15): vars whose required tier is unavailable are dimmed. */
        const tierOk = v.tier_ok !== false;
        const tr = el('tr');
        if (!tierOk) { tr.classList.add('tier-locked'); tr.style.opacity = '0.5'; }
        const tdTok = el('td', 'col-text');
        tdTok.appendChild(el('code', 'pv-tok', '{{' + v.token + '}}'));
        tr.appendChild(tdTok);
        tr.appendChild(el('td', 'col-text vars-name', v.name));
        const tdDesc = el('td', 'col-text vars-desc', v.desc);
        tdDesc.title = '預覽範例：' + v.sample;
        tr.appendChild(tdDesc);
        const tdScope = el('td', 'col-text vars-scope', v.scope);
        if (!tierOk) {
          tdScope.appendChild(document.createTextNode(' '));
          const lock = el('span', 'pill pill-off', v.tier_label || '方案受限');
          lock.style.fontSize = '10px';
          tdScope.appendChild(lock);
        }
        tr.appendChild(tdScope);
        const tdCopy = el('td', 'col-text');
        const cp = el('button', 'btn-link', '複製');
        cp.type = 'button';
        cp.addEventListener('click', () => {
          /* F-08: `writeText` is ASYNC — it returns a promise and rejects on a denied
             permission or a non-secure context, so the try/catch caught nothing and the
             success toast fired unconditionally. Measured 2026-08-27: a green 「✓ 已複製」
             beside an uncaught `Write permission denied` (the only uncaught exception in
             504 clicks), and on a plain-HTTP self-hosted instance `navigator.clipboard` is
             not there at all. Same shape as settings-notify.js::copyTopic, which had it
             right — two idioms for one job on the same settings page. */
          const label = '{{' + v.token + '}}';
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(label).then(
              () => window.toast('已複製', 'ok', label),
              () => window.toast('複製失敗，請手動選取', 'fail', label)
            );
          } else {
            window.toast('瀏覽器不支援自動複製，請手動選取', 'fail', label);
          }
        });
        tdCopy.appendChild(cp);
        tr.appendChild(tdCopy);
        tb.appendChild(tr);
      });
      table.appendChild(tb);
      tableWrap.appendChild(table);
      sec.appendChild(tableWrap);
      wrap.appendChild(sec);
    });
    wrap.appendChild(el('div', 'cmp-note',
      '所有變數由計算核心即時組裝注入（LLM 不自行計算）；「單一標的」範圍變數僅能用於範圍為「單一標的」的洞察任務。外部資料（FinMind 籌碼基本面、市場情緒）抓取後以快照存入資料庫，供回測重現當時輸入。'));
    panel.appendChild(wrap);
    mount(panel);
  })();

  /* 任務組合／校正版本管理已整合至洞察管線中心（pipeline-hub）——本頁只維護提示詞資產（2026-07-05 收斂，原 mock 區塊移除）。 */

  /* ================= 自我進化設定 (GET/PUT /api/evolution-config) ================= */
  await (async function () {
    const panel = el('section', 'panel');
    /* DEF-038: settings.html#prompts/evolution (洞察管線 › 進化設定) lands here. */
    panel.dataset.anchor = 'evolution';
    const head = el('div', 'panel-head');
    head.appendChild(el('h2', 'panel-title', '自我進化設定'));
    head.appendChild(el('span', 'panel-sub', '安全邊界與成本上限 — 儲存後套用於下次校正產生批次'));
    panel.appendChild(head);

    /* Read the backend config so the 5 visible fields reflect the stored knobs AND the
       non-panel knobs (horizon_basis / defer_limit_days / shadow_on_alert) are kept for a
       lossless round-trip on save. gap_alert_pp is a Decimal STRING on the wire; coerce to
       a number only for the numeric input's display value (never recompute money here). */
    const cfg = { auto_promote: false, shadow_batches: 5, min_samples: 8, max_shadows: 2, gap_alert_pp: '15' };
    let serverCfg = {};
    try {
      const got = await api.get('/api/evolution-config');
      if (got && typeof got === 'object') {
        serverCfg = got;
        Object.assign(cfg, got);
      }
    } catch (err) {
      _toast('進化設定載入失敗', 'fail', (err && err.message) || undefined);
    }

    const FIELDS = [
      { id: 'auto_promote', name: '影子評估勝出後自動切換生效版', kind: 'toggle',
        desc: '關閉時需人工按「設為生效」；建議觀察兩輪後再開啟。' },
      { id: 'shadow_batches', name: '影子評估批次數', kind: 'num', min: 3, max: 20, step: 1, unit: '次',
        desc: '最新版需並行評估 N 次且成績不劣於生效版，才視為勝出。' },
      { id: 'min_samples', name: '校正產生最低樣本數', kind: 'num', min: 3, max: 50, step: 1, unit: '筆',
        desc: '組合的到期評估未達此數，AI 大師模型不產生新版本（避免小樣本過擬合）。' },
      { id: 'max_shadows', name: '同時影子評估上限', kind: 'num', min: 1, max: 5, step: 1, unit: '個',
        desc: '影子期 LLM 呼叫 ×2 — 控制 AI 大師模型額外成本，超過時排隊。' },
      { id: 'gap_alert_pp', name: '校準誤差預警門檻', kind: 'num', min: 5, max: 50, step: 1, unit: 'pp',
        desc: '與「設定›預警規則」的 AI 校準誤差規則同步（F4）。' }
    ];

    const list = el('div', 'evo-cfg-list');
    const inputs = {};
    FIELDS.forEach((fd) => {
      const row = el('div', 'evo-cfg-row');
      const main = el('div', 'evo-cfg-main');
      main.appendChild(el('div', 'evo-cfg-name', fd.name));
      main.appendChild(el('div', 'evo-cfg-desc', fd.desc));
      row.appendChild(main);
      const ctrl = el('div', 'evo-cfg-ctrl');
      if (fd.kind === 'toggle') {
        const tg = el('button', 'toggle' + (cfg[fd.id] ? ' on' : ''));
        tg.type = 'button';
        tg.setAttribute('role', 'switch');
        tg.setAttribute('data-evo-field', fd.id);  // stable e2e hook
        tg.addEventListener('click', () => tg.classList.toggle('on'));
        inputs[fd.id] = () => tg.classList.contains('on');
        ctrl.appendChild(tg);
      } else {
        const inp = el('input', 'input evo-cfg-input');
        inp.type = 'number'; inp.min = fd.min; inp.max = fd.max; inp.step = fd.step;
        inp.setAttribute('data-evo-field', fd.id);  // stable e2e hook
        inp.value = cfg[fd.id];
        inputs[fd.id] = () => Number(inp.value);
        ctrl.appendChild(inp);
        ctrl.appendChild(el('span', 'evo-cfg-unit', fd.unit));
      }
      row.appendChild(ctrl);
      list.appendChild(row);
    });
    panel.appendChild(list);

    const acts = el('div', 'cal-actions');
    acts.style.padding = '4px var(--pad) 14px';
    const save = el('button', 'btn btn-primary', '儲存進化設定');
    save.type = 'button';
    save.setAttribute('data-evo-save', '1');  // stable e2e hook
    save.addEventListener('click', async () => {
      /* Build the PUT body from the FULL fetched config, then OVERRIDE only the 5 visible
         fields — this preserves the non-panel knobs (horizon_basis / defer_limit_days /
         shadow_on_alert) across the round-trip. gap_alert_pp goes back as a Decimal STRING. */
      const body = Object.assign({}, serverCfg, {
        auto_promote: inputs.auto_promote(),
        shadow_batches: inputs.shadow_batches(),
        min_samples: inputs.min_samples(),
        max_shadows: inputs.max_shadows(),
        gap_alert_pp: String(inputs.gap_alert_pp()),
      });
      save.disabled = true;
      try {
        const got = await api.put('/api/evolution-config', body);
        if (got && typeof got === 'object') serverCfg = got;  // keep the canonical view
        _toast('進化設定已儲存', 'ok', '下次校正產生批次生效');
      } catch (err) {
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      } finally {
        save.disabled = false;
      }
    });
    acts.appendChild(save);
    panel.appendChild(acts);
    mount(panel);
  })();

  /* Signal that all dynamic panels (組合器 / 校正庫 / 進化設定) are now in the DOM, so the
     page's trailing legacy-view collector runs AFTER they exist (panels mount async now). */
  document.dispatchEvent(new CustomEvent('pd-prompts-mounted'));
  } /* end boot() */

  boot();
})();
