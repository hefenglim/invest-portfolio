/* portfolio-dash — 洞察管線中心：乾跑預檢（preflight）＋「為什麼沒跑」診斷.
   兩者共用同一套守門閘門（R1–R8 + 任務啟用/排程），零 LLM 成本。
   後端對應：POST /api/insight-tasks/{id}/preflight、GET /api/insight-tasks/{id}/diagnose（spec 07）。 */
(function () {
  'use strict';
  const P = window.PIPE;
  const el = window.ppEl;

  /* ---- 共用：逐道閘門評估（前端 mock；後端為單一事實來源） ---- */
  function evalGates(t) {
    const gates = [];
    const push = (id, name, lv, msg, fixes) => gates.push({ id, name, lv, msg, fixes: fixes || [] });

    /* G0 任務啟用 */
    if (!t.enabled) push('G0', '任務啟用', 'fail', '任務目前為「停用」— 任何觸發都不會執行。',
      [{ label: '啟用任務', fn: () => { t.enabled = true; } }]);
    else push('G0', '任務啟用', 'ok', '任務已啟用。');

    /* G1 觸發來源 */
    if (t.scope === 'on_alert') {
      const n = t.trigger.rules ? t.trigger.rules.length : '全部';
      push('G1', '觸發來源', 'ok', '監聽 ' + (t.trigger.rules ? t.trigger.rules.map(P.ruleName).join('、') : '全部規則') + '（' + n + ' 條）；防抖 24h（R7）。');
    } else if (t.trigger.kind === 'manual') {
      push('G1', '觸發來源', 'fail', '未設定排程 — 不會自動執行，只能手動。',
        [{ label: '啟動排程', fn: () => window.ppScheduleModal(t, () => window.ppRenderCards()) }]);
    } else {
      push('G1', '觸發來源', 'ok', t.trigger.human + '・下次 ' + (t.trigger.next || '依週期計算') + '。');
    }

    /* R1 範圍×變數相容 */
    const tpls = t.templates.map(P.tplOf).filter(Boolean);
    const mismatch = tpls.filter((x) => x.scope === 'per_symbol' && t.scope !== 'per_symbol');
    if (mismatch.length) push('R1', '範圍相容', 'fail',
      '模板「' + mismatch.map((x) => x.name).join('、') + '」含單一標的變數，但任務範圍非單一標的 — 執行將被擋下。',
      [{ label: '更換模板', fn: () => window.toast('更換模板', 'ok', '開啟模板勾選表（設計稿）') }]);
    else push('R1', '範圍相容', 'ok', '模板變數範圍與任務範圍相容。');

    /* R2 標的宇宙 */
    if (t.scope === 'per_symbol') {
      const all = !t.universe || t.universe.mode === 'all';
      const n = all ? P.HELD.length : t.universe.symbols.length;
      if (!n) push('R2', '標的宇宙', 'fail', '自選清單已空（標的全數出清/移出）— 任務已自動停用。',
        [{ label: '編輯標的', fn: () => window.toast('編輯標的', 'ok', '開啟標的選擇器（設計稿）') }]);
      else push('R2', '標的宇宙', t.universeEvent ? 'info' : 'ok',
        (all ? '全部持倉 ' : '自選 ') + n + ' 檔有效' + (t.universeEvent ? '；' + t.universeEvent : '') + '。');
    } else {
      push('R2', '標的宇宙', 'ok', '不適用（非單一標的範圍）。');
    }

    /* R3 模板啟用 */
    const live = tpls.filter((x) => x.enabled);
    if (!live.length) push('R3', '模板啟用', 'fail',
      '引用的模板全部停用（' + tpls.map((x) => x.name).join('、') + '）— 該次執行整體跳過＋warn 預警。',
      [{ label: '啟用「' + (tpls[0] ? tpls[0].name : '') + '」', fn: () => { tpls.forEach((x) => { x.enabled = true; }); } }]);
    else if (live.length < tpls.length) push('R3', '模板啟用', 'warn',
      live.length + '/' + tpls.length + ' 模板啟用 — 停用段跳過，其餘照常執行。');
    else push('R3', '模板啟用', 'ok', tpls.length + ' 個模板全數啟用，依序串接於同一次 LLM 呼叫（R8）。');

    /* R4 缺價 */
    if (t.scope === 'per_symbol' && t.inputNote && t.inputNote.level === 'warn') {
      push('R4', '價格資料', 'warn', t.inputNote.text + ' — 該檔不呼叫 LLM，產確定性「資料異常」卡（零成本）。');
    } else {
      push('R4', '價格資料', 'ok', '快照內所有相關標的均有有效價格。');
    }

    /* R5 變數可用性 */
    const ingestVars = [];
    tpls.forEach((x) => (x.vars || []).forEach((v) => {
      if (['institutional_json', 'margin_json', 'monthly_revenue_json', 'valuation_json', 'financials_json', 'market_sentiment_json', 'index_quotes_json'].includes(v) && !ingestVars.includes(v)) ingestVars.push(v);
    }));
    if (ingestVars.length) push('R5', '變數可用性', 'warn',
      ingestVars.join('、') + ' 為外部快照變數：美股/馬股或斷線時代入 {"unavailable":true}，照常執行。');
    else push('R5', '變數可用性', 'ok', '全部變數由計算核心即時組裝，無外部依賴。');

    /* R6 額度 */
    const est = t.scope === 'per_symbol' ? (P.HELD.length * 0.01).toFixed(3) : '0.012';
    if (Number(P.health.quota.remaining) <= 0) push('R6', 'LLM 額度', 'fail', '額度已歸零 — 執行將中止（402）。');
    else push('R6', 'LLM 額度', P.health.quota.warn ? 'warn' : 'ok',
      '餘 $' + P.health.quota.remaining + '・本次估 $' + est + '；迭代中途耗盡會標 partial、已產卡保留（R6）。');

    /* 校正 */
    const cl = P.calibLabel(t);
    if (t.self_correct) {
      const ch = P.chainOf(t.id);
      const masterOk = P.health.master.ok;
      if (!masterOk) push('G7', '校正管線', 'warn', '未設定 AI 大師模型 — 洞察照常產生，回測評分與校正生成暫停。');
      else if (ch && ch.activeVer === null && ch.versions.length) push('G7', '校正管線', 'info', '已有 v' + ch.versions.length + ' 但未套用 — 本次執行不附加校正層。',
        [{ label: '前往版本鏈', fn: () => window.ppOpenDrawer(t, 'calib') }]);
      else push('G7', '校正管線', 'ok', cl.text + '；附加於模板之後（只能附加、不得改寫上層）。');
    } else {
      push('G7', '校正管線', 'ok', '自我校正未啟動 — 不附加校正層。');
    }
    return gates;
  }

  function gateList(gates) {
    const wrap = el('div', 'pf-list');
    gates.forEach((g) => {
      const row = el('div', 'pf-row ' + (g.lv === 'info' ? 'ok' : g.lv));
      row.appendChild(el('span', 'pf-ico', g.lv === 'ok' ? '✓' : g.lv === 'info' ? 'ⓘ' : g.lv === 'warn' ? '⚠' : '✕'));
      row.appendChild(el('span', 'pf-gate', g.name));
      const msg = el('span', 'pf-msg', g.msg);
      if (g.fixes.length) {
        const fx = el('span', 'fixes');
        g.fixes.forEach((f) => {
          const b = el('button', 'btn btn-sm btn-primary', f.label);
          b.type = 'button';
          b.addEventListener('click', () => {
            f.fn();
            window.ppRenderCards();
            window.toast('已套用修復', 'ok', f.label + '（設計稿）');
            document.querySelectorAll('.pv-backdrop').forEach((n) => n.remove());
          });
          fx.appendChild(b);
        });
        msg.appendChild(fx);
      }
      row.appendChild(msg);
      row.appendChild(el('span', 'pf-ref', g.id));
      wrap.appendChild(row);
    });
    return wrap;
  }

  /* ================= 乾跑預檢 ================= */
  window.ppPreflight = function (t) {
    window.ppModal('乾跑預檢 — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '不呼叫 LLM、零成本：逐道執行與正式執行相同的守門檢查（R1–R8），並組裝實際送出的提示詞供確認。'));
      const gates = evalGates(t);
      body.appendChild(gateList(gates));

      const fails = gates.filter((g) => g.lv === 'fail').length;
      const warns = gates.filter((g) => g.lv === 'warn').length;
      const sum = el('div', 'pf-sum');
      if (fails) sum.innerHTML = '<b>結論：本任務現在不會成功執行</b> — ' + fails + ' 道閘門擋下。修復上方紅色項目後重跑預檢。';
      else if (warns) sum.innerHTML = '<b>結論：可以執行</b> — ' + warns + ' 項警告會反映在產出（跳過段落/資料異常卡/部分執行），不會中斷。';
      else sum.innerHTML = '<b>結論：全部通過</b> — 下次觸發將完整執行。';
      body.appendChild(sum);

      /* 組裝預覽 */
      const det = document.createElement('details');
      det.style.marginTop = '10px';
      const s = el('summary', null, '查看組裝後提示詞（變數代入快照）');
      s.style.cssText = 'font-size:12px;color:var(--text-2);cursor:pointer;';
      det.appendChild(s);
      const tpls = t.templates.map(P.tplOf).filter(Boolean).filter((x) => x.enabled);
      const cl = P.calibLabel(t);
      const txt = ['【全域守則】', P.SYSTEM_RULES, '']
        .concat(tpls.map((x, i) => '【模板 ' + (i + 1) + '・' + x.name + '】\n' + x.body))
        .concat(t.self_correct && cl.on ? ['', '【校正層】' + cl.text] : [])
        .join('\n');
      const pre = el('pre', 'pv-pre', txt + '\n\n（變數 {{…}} 於正式執行時由計算核心代入；est 1,842 tokens）');
      det.appendChild(pre);
      body.appendChild(det);
    }, true);
  };

  /* ================= 為什麼沒跑？ ================= */
  window.ppDiagnose = function (t) {
    window.ppModal('為什麼沒跑？ — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '由上而下找出第一道擋住執行的閘門。每個未產出的執行也會記入運行記錄（status=skipped＋原因），可回溯。'));
      const gates = evalGates(t);
      const firstFail = gates.find((g) => g.lv === 'fail');
      body.appendChild(gateList(gates));
      const sum = el('div', 'pf-sum');
      if (firstFail) sum.innerHTML = '<b>診斷：卡在「' + firstFail.name + '」</b> — ' + firstFail.msg +
        (firstFail.fixes.length ? ' 點上方按鈕一鍵修復。' : '');
      else sum.innerHTML = '<b>診斷：目前沒有阻擋</b> — 任務會在下次觸發時執行；若仍無產出，請查運行記錄的 skipped 原因。';
      body.appendChild(sum);
    }, true);
  };
})();
