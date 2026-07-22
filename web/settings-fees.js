/* portfolio-dash — 費率明細 editor (FU-D2, data-driven).

   Rendered from GET /api/fee-rules (per rule set: per-field default + effective + overridden).
   Editing is a DB overlay over the fee-engine v2 defaults (config_seed.FEE_RULES); saving PUTs
   the changed fields; per-set + global 重設 delete the overlay. NO money is computed here — the
   values are rate/amount STRINGS delivered by the API and sent back verbatim; the frontend only
   detects "differs from default" to route a field to override-vs-revert. History is untouched
   (each transaction row keeps its own fee_rule_snapshot) — edits affect FUTURE trades only. */
(function () {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* Display labels for FEE RULE SETS (not accounts): after the Batch B merge, ONE account
     (Moomoo MY) references TWO sets — moomoo_us + moomoo_my — so each label reads as a
     rule set, never an account name. The set id is still shown verbatim beside the label. */
  const RS_NAMES = { tw: '台灣券商規則', schwab: '嘉信 Schwab 規則', moomoo_us: 'Moomoo 美股規則', moomoo_my: 'Moomoo 馬股規則' };
  const LABELS = {
    brokerage: '券商費率', discount: '折扣', min_fee: '最低手續費',
    tax_normal: '證交稅（現股）', tax_etf: '證交稅（ETF）', tax_daytrade: '證交稅（當沖）',
    rebate_rate: '折讓率', rounding: '捨入方式',
    commission_rate: '佣金率', commission_min: '最低佣金', platform_fee: '平台費',
    settlement_per_share: '交割費（每股）', settlement_cap_rate: '交割費上限比率',
    cat_per_share: 'CAT（每股）', sec_rate: 'SEC 費率', sec_min: 'SEC 最低',
    taf_per_share: 'TAF（每股）', taf_min: 'TAF 最低', taf_cap: 'TAF 上限',
    broker_assisted_surcharge: '專人下單附加費',
    clearing_rate: '清算費率', clearing_cap: '清算費上限', sst_rate: 'SST 稅率',
    stamp_unit: '印花稅級距', stamp_per_unit: '印花稅每級距',
    stamp_cap_stock: '印花稅上限（股票）', stamp_cap_etf: '印花稅上限（ETF）'
  };
  /* Fields relevant per market — keeps each card scannable (a US card never shows MY-only
     fields). Within the list, zero-default numeric fields that are NOT overridden are hidden
     (isMeaningful), so Schwab shows only SEC/TAF while Moomoo US shows the full US set. */
  const FIELDS_BY_MARKET = {
    TW: ['brokerage', 'discount', 'min_fee', 'tax_normal', 'tax_etf', 'tax_daytrade', 'rebate_rate', 'rounding'],
    US: ['commission_rate', 'commission_min', 'platform_fee', 'settlement_per_share', 'settlement_cap_rate',
      'cat_per_share', 'sec_rate', 'sec_min', 'taf_per_share', 'taf_min', 'taf_cap',
      'stamp_unit', 'stamp_per_unit', 'stamp_cap_stock', 'stamp_cap_etf', 'broker_assisted_surcharge', 'rounding'],
    MY: ['commission_rate', 'commission_min', 'platform_fee', 'clearing_rate', 'clearing_cap', 'sst_rate',
      'stamp_unit', 'stamp_per_unit', 'stamp_cap_stock', 'stamp_cap_etf', 'rounding']
  };
  const CAP_KEYS = new Set(['taf_cap', 'clearing_cap', 'stamp_cap_stock', 'stamp_cap_etf']);

  function isMeaningful(f) {
    if (f.key === 'rounding') return true;
    if (f.overridden) return true;
    if (CAP_KEYS.has(f.key)) return f.default !== null && f.default !== undefined; // 0-cap is meaningful (exempt)
    return f.default !== null && f.default !== undefined && Number(f.default) !== 0;
  }

  /* Change detection: does the input value differ from the field default? For numeric fields
     compare as numbers (a UI routing decision, never money of record — the value SENT is the
     input's own string). Blank input = revert. */
  function differsFromDefault(f, raw) {
    if (f.key === 'rounding') return raw !== f.default;
    if (raw === '' || raw === null) return f.default !== null;   // blank -> revert (unless default already null)
    return Number(raw) !== Number(f.default);
  }

  function fmtUpdated(iso) {
    if (!iso) return '';
    return '更新於 ' + String(iso).slice(0, 19).replace('T', ' ');
  }

  function renderField(f) {
    const row = el('div', 'fee-field');
    row.dataset.key = f.key;
    const label = el('div', 'ff-label');
    label.appendChild(el('span', null, LABELS[f.key] || f.key));
    label.appendChild(el('span', 'ff-key', f.key));
    const mod = el('span', 'fee-mod', '已修改');
    mod.hidden = !f.overridden;
    label.appendChild(mod);
    row.appendChild(label);

    const ctrl = el('div', 'fee-ctrl');
    let input;
    if (f.key === 'rounding') {
      input = el('select', 'input fee-input');
      [['floor', '無條件捨去 (floor)'], ['half_up', '四捨五入 (half_up)']].forEach((opt) => {
        const o = el('option', null, opt[1]); o.value = opt[0]; input.appendChild(o);
      });
      input.value = f.effective || f.default || 'half_up';
    } else {
      input = el('input', 'input fee-input');
      input.type = 'number'; input.step = 'any'; input.min = '0';
      if (!CAP_KEYS.has(f.key) && Number(f.default) <= 1) input.max = '1';
      input.value = f.effective === null || f.effective === undefined ? '' : f.effective;
      if (CAP_KEYS.has(f.key)) input.placeholder = f.default === null ? '無上限' : '';
    }
    input.dataset.key = f.key;
    ctrl.appendChild(input);

    const revert = el('button', 'fee-revert', '還原');
    revert.type = 'button';
    revert.hidden = !f.overridden;
    revert.title = '還原此欄位為系統預設';
    revert.dataset.key = f.key;
    ctrl.appendChild(revert);
    row.appendChild(ctrl);
    return row;
  }

  function renderCard(rs) {
    const det = el('details', 'fee-detail');
    if (rs.name === 'tw') det.open = true;
    const overriddenCount = rs.fields.filter((f) => f.overridden).length;
    const sum = el('summary');
    sum.appendChild(el('span', 'caret', '▶'));
    sum.appendChild(el('span', null, RS_NAMES[rs.name] || rs.name));
    sum.appendChild(el('span', 'panel-sub num', rs.name));
    if (overriddenCount) sum.appendChild(el('span', 'fee-count', overriddenCount + ' 項已修改'));
    if (rs.updated_at) sum.appendChild(el('span', 'fee-updated', fmtUpdated(rs.updated_at)));
    det.appendChild(sum);

    const rows = el('div', 'fee-rows');
    const shown = FIELDS_BY_MARKET[rs.market] || rs.fields.map((f) => f.key);
    const byKey = {}; rs.fields.forEach((f) => { byKey[f.key] = f; });
    shown.forEach((key) => {
      const f = byKey[key];
      if (f && isMeaningful(f)) rows.appendChild(renderField(f));
    });
    det.appendChild(rows);

    const foot = el('div', 'fee-foot');
    const reset = el('button', 'btn btn-sm', '重設為系統預設');
    reset.type = 'button'; reset.dataset.reset = rs.name;
    foot.appendChild(reset);
    foot.appendChild(el('span', 'spacer'));
    const save = el('button', 'btn btn-sm btn-primary', '儲存');
    save.type = 'button'; save.dataset.save = rs.name;
    foot.appendChild(save);
    det.appendChild(foot);

    // wiring
    det.querySelectorAll('.fee-revert').forEach((btn) => {
      btn.addEventListener('click', () => revertField(rs.name, btn.dataset.key));
    });
    save.addEventListener('click', () => saveSet(rs.name, byKey));
    reset.addEventListener('click', () => resetSet(rs.name));
    return det;
  }

  let SETS = [];

  function renderAll(body) {
    const wrap = $('#fee-rules-wrap');
    if (!wrap) return;
    SETS = (body && body.rule_sets) || [];
    wrap.replaceChildren();
    SETS.forEach((rs) => wrap.appendChild(renderCard(rs)));
  }

  async function load() {
    if (!window.pdApi) return;
    try {
      renderAll(await window.pdApi.get('/api/fee-rules'));
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  function collect(name, byKey) {
    /* Build the PUT overrides map from the card's inputs: a value that differs from the
       default is sent as an override string; one equal to the default is sent as null
       (revert / clear any stale override). Only rendered fields are touched. */
    const overrides = {};
    const card = document.querySelector('details.fee-detail [data-save="' + name + '"]');
    const det = card ? card.closest('details') : null;
    if (!det) return overrides;
    det.querySelectorAll('.fee-input').forEach((inp) => {
      const f = byKey[inp.dataset.key];
      if (!f) return;
      const raw = inp.value;
      overrides[inp.dataset.key] = differsFromDefault(f, raw) ? raw : null;
    });
    return overrides;
  }

  async function saveSet(name, byKey) {
    if (!window.pdApi) return;
    const btn = document.querySelector('[data-save="' + name + '"]');
    if (btn) btn.disabled = true;
    try {
      const res = await window.pdApi.put('/api/fee-rules/' + name, { overrides: collect(name, byKey) });
      replaceCard(res);
      if (window.toast) window.toast('費率已儲存', 'ok', '僅影響未來交易；歷史以交易列快照為準');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function revertField(name, key) {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.put('/api/fee-rules/' + name, { overrides: { [key]: null } });
      replaceCard(res);
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  async function resetSet(name) {
    if (!window.pdApi) return;
    if (!window.confirm('重設「' + (RS_NAMES[name] || name) + '」所有費率為系統預設？')) return;
    try {
      replaceCard(await window.pdApi.post('/api/fee-rules/' + name + '/reset'));
      if (window.toast) window.toast('已重設為系統預設', 'ok');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  async function resetAll() {
    if (!window.pdApi) return;
    if (!window.confirm('重設全部費率規則為系統預設？此動作會清除所有費率調整。')) return;
    try {
      renderAll(await window.pdApi.post('/api/fee-rules/reset-all'));
      if (window.toast) window.toast('全部費率已重設為系統預設', 'ok');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  /* Replace a single card in place (keeps other cards' open/scroll state). */
  function replaceCard(rs) {
    if (!rs || !rs.name) return;
    const idx = SETS.findIndex((s) => s.name === rs.name);
    if (idx >= 0) SETS[idx] = rs;
    const btn = document.querySelector('[data-save="' + rs.name + '"]');
    const old = btn ? btn.closest('details') : null;
    const fresh = renderCard(rs);
    if (old && old.parentNode) { fresh.open = old.open; old.replaceWith(fresh); }
    else load();
  }

  const resetAllBtn = $('#fee-reset-all');
  if (resetAllBtn) resetAllBtn.addEventListener('click', resetAll);
  window.PD_RENDER_FEE_RULES = load;
  load();
})();
