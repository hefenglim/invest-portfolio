/* portfolio-dash — shared instrument quick-add dialog (FU-D23).

   window.pdInstQuickAdd(opts) opens a modal that:
     1. runs GET /api/instruments/lookup — fast identify (name + suggested sector +
        board/is_etf); found:false is the typo guard and blocks the confirm,
     2. lets the user confirm the name + pick/enter a sector (datalist of the EXISTING
        sectors + free text; the lookup suggestion is preselected),
     3. registers via POST /api/instruments — the heavy quote/history fetch runs in the
        BACKGROUND (BackgroundTasks), so the confirm returns fast,
   then calls opts.onConfirm(result) (確認) or opts.onBuy(result) (記一筆買入).

   opts:
     symbol      prefill symbol (both call sites prefill it)
     market      'TW' | 'US' | 'MY' (default 'TW')
     lockSymbol  symbol field readonly when true (prefilled)
     onConfirm(result)  after 確認 registers
     onBuy(result)      after 記一筆買入 registers (the caller navigates to the manual pane)
   result = the POST /api/instruments response element (+ restored / last_price_date when a
   soft-deleted symbol was RESTORED rather than freshly added).

   No money is computed here (the dialog only submits metadata; prices come from the API).
   Styling reuses the shared .modal-* / .field / .btn classes (no styles.css edits). */
(function () {
  'use strict';
  const api = window.pdApi;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const MARKETS = [['TW', '台股'], ['US', '美股'], ['MY', '馬股']];

  /* Distinct non-empty sectors already in the registry — the datalist suggestions. */
  async function fetchSectors() {
    try {
      const resp = await api.get('/api/instruments');
      const set = new Set();
      ((resp && resp.list) || []).forEach((i) => { if (i.sector) set.add(i.sector); });
      return Array.from(set).sort();
    } catch (e) {
      return [];
    }
  }

  window.pdInstQuickAdd = function (opts) {
    opts = opts || {};
    if (!api) return;

    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');

    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '加入觀察清單'));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    close.title = '關閉（Esc）';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    const fld = (label, node) => {
      const w = el('div', 'field');
      w.appendChild(el('label', null, label));
      w.appendChild(node);
      return w;
    };

    const symIn = el('input', 'input');
    symIn.value = (opts.symbol || '').trim().toUpperCase();
    symIn.spellcheck = false;
    symIn.placeholder = '例：2330、AAPL';
    if (opts.lockSymbol) symIn.readOnly = true;
    body.appendChild(fld('代號', symIn));

    const mktSel = el('select', 'select');
    MARKETS.forEach(([v, label]) => {
      const o = el('option', null, label); o.value = v;
      if ((opts.market || 'TW') === v) o.selected = true;
      mktSel.appendChild(o);
    });
    body.appendChild(fld('市場', mktSel));

    const nameIn = el('input', 'input');
    nameIn.placeholder = '名稱（自動查詢，可修改）';
    nameIn.spellcheck = false;
    body.appendChild(fld('名稱', nameIn));

    const dlId = 'iqa-sectors-' + Date.now();
    const secIn = el('input', 'input');
    secIn.setAttribute('list', dlId);
    secIn.placeholder = '產業（可從清單選擇或自行輸入）';
    secIn.spellcheck = false;
    const dl = el('datalist'); dl.id = dlId;
    const secField = fld('產業', secIn);
    secField.appendChild(dl);
    body.appendChild(secField);

    const etfWrap = el('label', 'hint');
    const etfCb = el('input'); etfCb.type = 'checkbox';
    etfWrap.appendChild(etfCb);
    etfWrap.appendChild(el('span', null, ' 此標的為 ETF（影響台股賣出稅率 0.1%）'));
    body.appendChild(fld('類別', etfWrap));

    const status = el('div', 'hint');
    status.style.cssText = 'min-height:16px;';
    body.appendChild(status);
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    const buyBtn = el('button', 'btn', '記一筆買入'); buyBtn.type = 'button';
    buyBtn.title = '加入後直接前往手動交易，並帶入此代號';
    const okBtn = el('button', 'btn btn-primary', '確認'); okBtn.type = 'button';
    foot.appendChild(buyBtn); foot.appendChild(okBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    /* Latest lookup result — board rides through to the POST so TW is not re-probed. */
    let lookupState = { found: false, registered: false, archived: false, board: null };
    let busy = false;

    function setEnabled(ok) { okBtn.disabled = !ok; buyBtn.disabled = !ok; }
    setEnabled(false);

    const dismiss = () => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape' && !busy) dismiss(); };
    document.addEventListener('keydown', onKey);
    close.addEventListener('click', () => { if (!busy) dismiss(); });
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop && !busy) dismiss(); });

    async function runLookup() {
      const sym = symIn.value.trim().toUpperCase();
      setEnabled(false);
      lookupState = { found: false, registered: false, archived: false, board: null };
      if (!sym) { status.textContent = '請輸入代號'; return; }
      status.textContent = '查詢中…';
      let r;
      try {
        r = await api.get('/api/instruments/lookup', { symbol: sym, market: mktSel.value });
      } catch (err) {
        status.textContent = '查詢失敗，請稍後再試';
        return;
      }
      lookupState = r || { found: false };
      if (r && r.registered) {
        status.textContent = '已註冊 — 此標的已在觀察清單中';
        return;
      }
      if (!r || !r.found) {
        status.textContent = '查無報價 — 請確認代號與市場是否正確';
        return;
      }
      /* found & addable (a brand-new symbol, or an archived one to restore) */
      if (r.name && !nameIn.value.trim()) nameIn.value = r.name;
      if (r.sector && !secIn.value.trim()) secIn.value = r.sector;
      etfCb.checked = !!r.is_etf;
      status.textContent = r.archived
        ? '已封存 — 確認後將還原並於背景補抓缺口'
        : '已找到，確認後加入觀察清單（報價與歷史於背景抓取）';
      setEnabled(true);
    }

    async function doRegister(after) {
      const sym = symIn.value.trim().toUpperCase();
      if (!sym) return;
      const reqBody = {
        symbol: sym, market: mktSel.value,
        name: nameIn.value.trim(), sector: secIn.value.trim(),
        is_etf: etfCb.checked,
      };
      /* Only a TW board rides through (US/MY resolve their board server-side). */
      if (mktSel.value === 'TW' && lookupState.board) reqBody.board = lookupState.board;
      const btn = after === 'buy' ? buyBtn : okBtn;
      busy = true;
      const restore = window.pdBusy ? window.pdBusy(btn, '加入中…') : () => {};
      let resp;
      try {
        resp = await api.post('/api/instruments', reqBody);
      } catch (err) {
        busy = false;
        restore();
        if (window.toast) {
          window.toast(err && err.message ? err.message : '加入失敗', 'fail', err && err.code);
        }
        return;
      }
      busy = false;
      restore();
      if (window.toast) {
        if (resp && resp.restored) {
          window.toast('已還原 ' + sym, 'ok',
            '已還原既有資料' +
            (resp.last_price_date ? '（保留至 ' + resp.last_price_date + '）' : '') +
            '，背景補抓中');
        } else {
          window.toast('已加入 ' + sym, 'ok', '已加入觀察清單，背景抓取報價中');
        }
      }
      dismiss();
      if (after === 'buy') { if (opts.onBuy) opts.onBuy(resp); }
      else if (opts.onConfirm) { opts.onConfirm(resp); }
    }

    okBtn.addEventListener('click', () => doRegister('confirm'));
    buyBtn.addEventListener('click', () => doRegister('buy'));
    mktSel.addEventListener('change', runLookup);
    if (!opts.lockSymbol) {
      let t = null;
      symIn.addEventListener('input', () => {
        if (t) clearTimeout(t);
        t = setTimeout(runLookup, 300);
      });
    }

    document.body.appendChild(backdrop);
    fetchSectors().then((secs) => {
      secs.forEach((s) => { const o = el('option'); o.value = s; dl.appendChild(o); });
    });
    runLookup();
    setTimeout(() => { (opts.lockSymbol ? nameIn : symIn).focus(); }, 50);
  };
})();
