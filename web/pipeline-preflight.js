/* portfolio-dash — 洞察管線中心：乾跑預檢（preflight）＋「為什麼沒跑」診斷.
   兩者共用後端同一套守門閘門（R1–R8 + G0/G1/G7），零 LLM 成本、零寫入。
   後端（spec 07，皆經 window.pdApi）：
   - POST /api/insight-tasks/{id}/preflight
       -> { gates[{id,name,lv,msg,fix:{kind,id}|null}], verdict,
            assembled_preview{ layers[{kind,name,rendered}], est_tokens, est_cost_usd } }
   - GET  /api/insight-tasks/{id}/diagnose
       -> { gates[...], verdict, first_blocker, recent_skips[{at,reason}] }

   I2：每個 gate 的 fix.kind 透過 window.ppFixKinds（pipeline.js）映射成中文標籤＋一鍵動作；
   recent_skips 的 reason 代碼透過 window.ppSkipReason 映射成中文。
   錢規則：assembled_preview.est_cost_usd 為後端 Decimal STRING，經 window.fmt 呈現。 */
(function () {
  'use strict';
  var pdApi = window.pdApi;
  var f = window.fmt;
  var el = window.ppEl;

  /* gate.lv from the backend is one of ok|info|warn|fail. Render an icon per level. */
  function gateIcon(lv) {
    return lv === 'ok' ? '✓' : lv === 'info' ? 'ⓘ' : lv === 'warn' ? '⚠' : '✕';
  }
  function gateRowCls(lv) { return lv === 'info' ? 'ok' : lv; }

  /* Render the shared gate list; each gate's fix.kind -> a one-click button (I2). */
  function gateList(gates, t, closeModal) {
    var wrap = el('div', 'pf-list');
    (gates || []).forEach(function (g) {
      var row = el('div', 'pf-row ' + gateRowCls(g.lv));
      row.appendChild(el('span', 'pf-ico', gateIcon(g.lv)));
      row.appendChild(el('span', 'pf-gate', g.name));
      var msg = el('span', 'pf-msg', g.msg || '');
      if (g.fix && g.fix.kind) {
        var def = (window.ppFixKinds || {})[g.fix.kind];
        var label = def ? def.label : g.fix.kind;
        var fx = el('span', 'fixes');
        var b = el('button', 'btn btn-sm btn-primary', label);
        b.type = 'button';
        b.addEventListener('click', function () {
          if (closeModal) closeModal();
          document.querySelectorAll('.pv-backdrop').forEach(function (n) { n.remove(); });
          if (def && def.run) def.run(t, g.fix);
          else window.toast(label, 'ok', '請於對應頁面完成設定');
        });
        fx.appendChild(b);
        msg.appendChild(fx);
      }
      row.appendChild(msg);
      row.appendChild(el('span', 'pf-ref', g.id));
      wrap.appendChild(row);
    });
    return wrap;
  }

  function verdictSummary(verdict, gates) {
    var fails = (gates || []).filter(function (g) { return g.lv === 'fail'; }).length;
    var warns = (gates || []).filter(function (g) { return g.lv === 'warn'; }).length;
    var sum = el('div', 'pf-sum');
    if (verdict === 'blocked') {
      sum.innerHTML = '<b>結論：本任務現在不會成功執行</b> — ' + fails + ' 道閘門擋下。修復上方紅色項目後重跑預檢。';
    } else if (verdict === 'degraded') {
      sum.innerHTML = '<b>結論：可以執行</b> — ' + warns + ' 項警告會反映在產出（跳過段落/資料異常卡/部分執行），不會中斷。';
    } else {
      sum.innerHTML = '<b>結論：全部通過</b> — 下次觸發將完整執行。';
    }
    return sum;
  }

  /* ================= 乾跑預檢 ================= */
  window.ppPreflight = function (t) {
    var back = window.ppModal('乾跑預檢 — ' + t.name, function (body) {
      body.appendChild(el('div', 'pv-note',
        '不呼叫 LLM、零成本：逐道執行與正式執行相同的守門檢查（R1–R8），並組裝實際送出的提示詞供確認。'));
      var slot = el('div');
      slot.appendChild(el('div', 'wz-note', '執行預檢中…'));
      body.appendChild(slot);

      if (!pdApi) { slot.replaceChildren(el('div', 'wz-note', '預檢不可用。')); return; }
      pdApi.post('/api/insight-tasks/' + t.id + '/preflight').then(function (resp) {
        slot.replaceChildren();
        var gates = (resp && resp.gates) || [];
        slot.appendChild(gateList(gates, t, function () { if (back) back.remove(); }));
        slot.appendChild(verdictSummary(resp && resp.verdict, gates));

        /* 組裝預覽 — layers + est tokens + est cost (Decimal STRING via f.num). */
        var preview = resp && resp.assembled_preview;
        if (preview) {
          var det = document.createElement('details');
          det.style.marginTop = '10px';
          var s = el('summary', null, '查看組裝後提示詞（變數已代入快照）');
          s.style.cssText = 'font-size:12px;color:var(--text-2);cursor:pointer;';
          det.appendChild(s);
          var parts = (preview.layers || []).map(function (lyr) {
            return '【' + (lyr.name || lyr.kind) + '】\n' + (lyr.rendered || '');
          });
          var note = '\n\n（估算 ' + f.num(preview.est_tokens) + ' tokens・約 $'
            + f.num(preview.est_cost_usd, 4) + '；預檢零成本，不實際扣除）';
          det.appendChild(el('pre', 'pv-pre', parts.join('\n\n') + note));
          body.appendChild(det);
        }
      }).catch(function (err) {
        slot.replaceChildren(el('div', 'wz-note', '預檢失敗：' + ((err && err.message) || '未知錯誤')));
      });
    }, true);
  };

  /* ================= 為什麼沒跑？ ================= */
  window.ppDiagnose = function (t) {
    var back = window.ppModal('為什麼沒跑？ — ' + t.name, function (body) {
      body.appendChild(el('div', 'pv-note',
        '由上而下找出第一道擋住執行的閘門。每個未產出的執行也會記入運行記錄（status=skipped＋原因），可回溯。'));
      var slot = el('div');
      slot.appendChild(el('div', 'wz-note', '診斷中…'));
      body.appendChild(slot);

      if (!pdApi) { slot.replaceChildren(el('div', 'wz-note', '診斷不可用。')); return; }
      pdApi.get('/api/insight-tasks/' + t.id + '/diagnose').then(function (resp) {
        slot.replaceChildren();
        var gates = (resp && resp.gates) || [];
        slot.appendChild(gateList(gates, t, function () { if (back) back.remove(); }));

        var first = resp && resp.first_blocker;
        var sum = el('div', 'pf-sum');
        if (first) {
          var fg = gates.find(function (g) { return g.id === first; });
          sum.innerHTML = '<b>診斷：卡在「' + (fg ? fg.name : first) + '」</b> — ' + (fg ? fg.msg : '')
            + (fg && fg.fix && fg.fix.kind ? ' 點上方按鈕一鍵修復。' : '');
        } else {
          sum.innerHTML = '<b>診斷：目前沒有阻擋</b> — 任務會在下次觸發時執行；若仍無產出，請查下方近期跳過原因。';
        }
        slot.appendChild(sum);

        /* recent_skips — reason codes -> human zh labels (I2). */
        var skips = (resp && resp.recent_skips) || [];
        if (skips.length) {
          var h = el('div', 'pv-note', '近期跳過記錄：');
          h.style.marginTop = '10px';
          slot.appendChild(h);
          var ul = el('div', 'pf-list');
          skips.forEach(function (sk) {
            var row = el('div', 'pf-row warn');
            row.appendChild(el('span', 'pf-ico', '⚠'));
            row.appendChild(el('span', 'pf-gate', f.datetime(sk.at)));
            row.appendChild(el('span', 'pf-msg', window.ppSkipReason(sk.reason)));
            ul.appendChild(row);
          });
          slot.appendChild(ul);
        }
      }).catch(function (err) {
        slot.replaceChildren(el('div', 'wz-note', '診斷失敗：' + ((err && err.message) || '未知錯誤')));
      });
    }, true);
  };
})();
