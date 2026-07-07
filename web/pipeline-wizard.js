/* portfolio-dash — 洞察管線中心：建立精靈（4 步）＋右側即時管線預覽（spec 07 / 19）.
   核心理念：使用者每做一個選擇，右側的「管線」就長出一節 — 規劃即所見。
   相容性（R1）在勾選當下就以後端 draft 預檢確認（建立前先乾跑）。

   資料來源（全部經 window.pdApi）：
   - 分析模板  ← GET /api/strategy-prompts  ([{id,name,body,enabled,...}])
   - 預警規則  ← GET /api/alert-rules        ({rules:[{id,...}]})
   - 持倉標的  ← GET /api/dashboard          (holdings[].symbol)
   - 額度      ← GET /api/insight-tasks/status (health.quota_remaining, Decimal STRING)
   建立：POST /api/insight-tasks（InsightTypeIn）→（排程則）POST .../{id}/schedule → 預檢。

   錢規則：右側「估算成本」是本地 count × 單價的設計估算（~$X/次），可保留為估算；但同行
   顯示的「額度餘」必須來自後端 quota_remaining，一律經 window.fmt 呈現。 */
(function () {
  'use strict';
  var pdApi = window.pdApi;
  var f = window.fmt;
  var el = window.ppEl;

  var STEPS = ['觸發', '範圍', '組裝', '確認'];

  /* Stable alert-rule id -> zh label (the /api/alert-rules payload carries ids only). */
  var RULE_LABELS = {
    single_weight: '單一標的集中度', sector_weight: '產業集中度',
    stale_price: '價格過期/缺價', fx_drift: '匯率漂移',
    exdiv_upcoming: '即將除息', quota_low: 'AI 額度偏低', calib_gap: 'AI 校準誤差'
  };
  function ruleLabel(id) { return RULE_LABELS[id] || id; }

  window.ppWizard = function () {
    /* fetched reference data (filled before the wizard renders). */
    var REF = { templates: [], rules: [], held: [], quota: null };
    var tplOf = function (id) {
      return REF.templates.find(function (x) { return x.id === id; }) || null;
    };

    /* 草稿狀態 */
    var d = {
      name: '', trigger: 'schedule', cron: '0 8 * * *',
      rules: 'all', scope: 'portfolio', universe: { mode: 'all' },
      templates: [], self_correct: true
    };
    var step = 0;

    /* Load the reference data, THEN open the modal (so selectors are populated). */
    var loads = pdApi ? Promise.all([
      pdApi.get('/api/strategy-prompts').catch(function () { return []; }),
      pdApi.get('/api/alert-rules').catch(function () { return { rules: [] }; }),
      pdApi.get('/api/dashboard').catch(function () { return { holdings: [] }; }),
      pdApi.get('/api/insight-tasks/status').catch(function () { return { health: {} }; })
    ]) : Promise.resolve([[], { rules: [] }, { holdings: [] }, { health: {} }]);

    loads.then(function (res) {
      REF.templates = (Array.isArray(res[0]) ? res[0] : []).filter(function (x) { return !x.archived; });
      REF.rules = (res[1] && res[1].rules) || [];
      REF.held = ((res[2] && res[2].holdings) || []).map(function (h) { return h.symbol; });
      REF.quota = res[3] && res[3].health ? res[3].health.quota_remaining : null;
      openWizard();
    });

    function openWizard() {
    window.ppModal('新增洞察任務', function (body) {
      body.classList.add('wz-body');
      var shell = el('div', 'wz-shell');
      var main = el('div', 'wz-main');
      var rail = el('div', 'wz-rail');
      shell.appendChild(main);
      shell.appendChild(rail);
      body.appendChild(shell);

      /* ---- 右側即時管線預覽 ---- */
      function symCount() {
        if (d.scope !== 'per_symbol') return 1;
        return d.universe.mode === 'all' ? REF.held.length : (d.universe.symbols || []).length;
      }
      function renderRail() {
        rail.replaceChildren();
        rail.appendChild(el('div', 'wz-rail-title', '管線預覽 — 即時組裝'));
        var stack = el('div', 'pp-stack');
        var layer = function (tag, name, cls) {
          var l = el('div', 'pp-layer' + (cls ? ' ' + cls : ''));
          l.appendChild(el('span', 'tag', tag));
          l.appendChild(el('span', 'name', name));
          return l;
        };
        stack.appendChild(layer('觸發', d.trigger === 'schedule' ? cronHuman(d.cron)
          : d.trigger === 'on_alert' ? '預警觸發（' + (d.rules === 'all' ? '全部規則' : d.rules.length + ' 條') + '）'
          : '手動（暫不排程）'));
        stack.appendChild(layer('輸入', d.scope === 'portfolio' ? '全組合・1 卡'
          : d.scope === 'on_alert' ? '事件標的'
          : (d.universe.mode === 'all' ? '全部持倉 ' + REF.held.length : '自選 ' + (d.universe.symbols || []).length) + ' 檔・每檔 1 卡'));
        stack.appendChild(layer('守則', '全域守則（共用）'));
        if (!d.templates.length) stack.appendChild(layer('模板', '－ 尚未選擇 －', 'l-off'));
        d.templates.forEach(function (tid, i) {
          var t = tplOf(tid);
          stack.appendChild(layer('模板' + (i + 1), t ? t.name : String(tid)));
        });
        if (d.self_correct) stack.appendChild(layer('校正', '自我校正（累積樣本後生成）', 'l-calib'));
        stack.appendChild(layer('產出', d.scope === 'per_symbol'
          ? symCount() + ' 張洞察卡/次'
          : d.scope === 'per_market' ? '每持有市場 1 張/次' : '1 張洞察卡/次'));
        rail.appendChild(stack);
        var est = el('div', 'wz-note');
        est.style.marginTop = '8px';
        var n = symCount();
        /* LOCAL count × rate ESTIMATE ("~$X / 次") — a design estimate, NOT money of record.
           The額度餘 alongside MUST come from the backend quota STRING via f.num. */
        est.textContent = '估算成本：~$' + (n * 0.01).toFixed(2)
          + ' / 次（額度餘 $' + f.num(REF.quota, 2) + '）';
        rail.appendChild(est);
      }

      /* ---- 步驟頭 ---- */
      var stepsBar = el('div', 'wz-steps');
      function renderSteps() {
        stepsBar.replaceChildren();
        STEPS.forEach(function (s, i) {
          var st = el('div', 'wz-step' + (i === step ? ' cur' : i < step ? ' done' : ''));
          st.appendChild(el('span', 'n', i < step ? '✓' : String(i + 1)));
          st.appendChild(el('span', null, s));
          stepsBar.appendChild(st);
        });
      }

      var stage = el('div');
      var foot = el('div', 'wz-foot');
      main.appendChild(stepsBar);
      main.appendChild(stage);
      main.appendChild(foot);

      var opt = function (title, sub, sel, fn) {
        var o = el('button', 'wz-opt' + (sel ? ' sel' : ''));
        o.type = 'button';
        o.appendChild(el('b', null, title));
        o.appendChild(el('span', null, sub));
        o.addEventListener('click', fn);
        return o;
      };

      /* ---- 各步驟 ---- */
      function renderStage() {
        renderSteps();
        renderRail();
        stage.replaceChildren();
        foot.replaceChildren();

        if (step === 0) {
          stage.appendChild(el('div', 'pv-note', '這個任務什麼時候跑？（之後隨時可改）'));
          var grid = el('div', 'wz-opts');
          grid.appendChild(opt('定期排程', '寫入運行中心，依週期自動執行', d.trigger === 'schedule', function () { d.trigger = 'schedule'; if (d.scope === 'on_alert') d.scope = 'portfolio'; renderStage(); }));
          grid.appendChild(opt('預警觸發', '預警規則命中時自動解讀（不可排程）', d.trigger === 'on_alert', function () { d.trigger = 'on_alert'; d.scope = 'on_alert'; renderStage(); }));
          grid.appendChild(opt('先不排程', '手動執行；之後可隨時掛排程', d.trigger === 'manual', function () { d.trigger = 'manual'; if (d.scope === 'on_alert') d.scope = 'portfolio'; renderStage(); }));
          stage.appendChild(grid);
          if (d.trigger === 'schedule') {
            var row = el('div', 'wz-fields');
            var period = el('select', 'select');
            [['每日 08:00', '0 8 * * *'], ['每週一 09:00', '0 9 * * 1'],
             ['每週五 17:00', '0 17 * * 5'], ['每月 1 日 08:00', '0 8 1 * *']].forEach(function (pair) {
              var o = el('option', null, pair[0]); o.value = pair[1];
              if (pair[1] === d.cron) o.selected = true;
              period.appendChild(o);
            });
            period.addEventListener('change', function () { d.cron = period.value; renderRail(); });
            row.appendChild(period);
            stage.appendChild(row);
          }
          if (d.trigger === 'on_alert') {
            var note = el('div', 'wz-note');
            note.style.marginTop = '10px';
            note.textContent = '監聽規則（預設全部；可自選）：';
            stage.appendChild(note);
            var grid2 = el('div', 'pv-symgrid');
            grid2.style.marginTop = '6px';
            REF.rules.forEach(function (r) {
              var lb = el('label', 'pv-check');
              var cb = el('input');
              cb.type = 'checkbox';
              cb.checked = d.rules === 'all' || (Array.isArray(d.rules) && d.rules.indexOf(r.id) >= 0);
              cb.value = r.id;
              cb.addEventListener('change', function () {
                var picked = Array.prototype.slice.call(grid2.querySelectorAll('input:checked')).map(function (x) { return x.value; });
                d.rules = picked.length === REF.rules.length ? 'all' : picked;
                renderRail();
              });
              lb.appendChild(cb);
              lb.appendChild(el('span', null, ruleLabel(r.id)));
              grid2.appendChild(lb);
            });
            stage.appendChild(grid2);
          }
        }

        if (step === 1) {
          if (d.trigger === 'on_alert') {
            stage.appendChild(el('div', 'pv-note', '預警觸發任務的範圍固定為「事件標的」— 由命中的規則決定要解讀哪個標的或全組合。'));
          } else {
            stage.appendChild(el('div', 'pv-note', '一次執行要看整個組合，還是逐檔各看一次？'));
            var grid3 = el('div', 'wz-opts');
            grid3.style.gridTemplateColumns = 'repeat(3, 1fr)';
            grid3.appendChild(opt('全組合', '單一快照、產 1 張卡', d.scope === 'portfolio', function () { d.scope = 'portfolio'; d.templates = d.templates.filter(function (tid) { var t = tplOf(tid); return t && t.scope !== 'per_symbol'; }); renderStage(); }));
            grid3.appendChild(opt('單一市場', '台/美/馬各一張；資料自動市場切片', d.scope === 'per_market', function () { d.scope = 'per_market'; d.templates = d.templates.filter(function (tid) { var t = tplOf(tid); return t && t.scope !== 'per_symbol'; }); renderStage(); }));
            grid3.appendChild(opt('單一標的', '每檔一張卡；可用個股級變數', d.scope === 'per_symbol', function () { d.scope = 'per_symbol'; renderStage(); }));
            stage.appendChild(grid3);
            if (d.scope === 'per_symbol') {
              var n2 = el('div', 'wz-note');
              n2.style.margin = '10px 0 6px';
              n2.textContent = '標的宇宙（出清/移出觀察清單會自動移除；清單空會自動停用＋預警）：';
              stage.appendChild(n2);
              var grid4 = el('div', 'wz-opts');
              grid4.style.gridTemplateColumns = 'repeat(2, 1fr)';
              grid4.appendChild(opt('全部持倉', REF.held.length + ' 檔・自動跟隨持倉變動', d.universe.mode === 'all', function () { d.universe = { mode: 'all' }; renderStage(); }));
              grid4.appendChild(opt('自選標的', '持倉勾選', d.universe.mode === 'custom', function () { d.universe = { mode: 'custom', symbols: d.universe.symbols || [] }; renderStage(); }));
              stage.appendChild(grid4);
              if (d.universe.mode === 'custom') {
                var grid5 = el('div', 'pv-symgrid');
                grid5.style.marginTop = '8px';
                if (!REF.held.length) grid5.appendChild(el('div', 'wz-note', '目前無持倉標的可選。'));
                REF.held.forEach(function (sym) {
                  var lb = el('label', 'pv-check');
                  var cb = el('input');
                  cb.type = 'checkbox';
                  cb.value = sym;
                  cb.checked = (d.universe.symbols || []).indexOf(sym) >= 0;
                  cb.addEventListener('change', function () {
                    d.universe.symbols = Array.prototype.slice.call(grid5.querySelectorAll('input:checked')).map(function (x) { return x.value; });
                    renderRail();
                  });
                  lb.appendChild(cb);
                  lb.appendChild(el('span', null, sym));
                  lb.appendChild(el('span', 'pv-symtag', '持倉'));
                  grid5.appendChild(lb);
                });
                stage.appendChild(grid5);
              }
            }
          }
        }

        if (step === 2) {
          stage.appendChild(el('div', 'pv-note',
            '選擇分析模板（可多選、依序串接於同一次呼叫）。與範圍不相容的模板會直接鎖定 — 建立前的乾跑預檢會再確認一次（R1）。'));
          if (!REF.templates.length) stage.appendChild(el('div', 'wz-note', '尚無分析模板 — 請先到模板庫建立。'));
          REF.templates.forEach(function (t) {
            var blocked = t.scope === 'per_symbol' && d.scope !== 'per_symbol';
            var row = el('div', 'wz-tpl-row' + (blocked ? ' blocked' : ''));
            var ix = d.templates.indexOf(t.id);
            row.appendChild(el('span', 'ord', ix >= 0 ? String(ix + 1) : ''));
            var cb = el('input');
            cb.type = 'checkbox';
            cb.disabled = blocked;
            cb.checked = ix >= 0;
            cb.addEventListener('change', function () {
              if (cb.checked) d.templates.push(t.id);
              else d.templates = d.templates.filter(function (x) { return x !== t.id; });
              renderStage();
            });
            row.appendChild(cb);
            row.appendChild(el('span', null, t.name));
            row.appendChild(el('span', 'pv-symtag' + (t.scope === 'per_symbol' ? ' watch' : ''), t.scope === 'per_symbol' ? '單一標的' : '全組合'));
            if (!t.enabled) row.appendChild(el('span', 'why', '模板目前停用 — 可選，但執行時該段會跳過（R3）'));
            if (blocked) row.appendChild(el('span', 'why', '含單一標的變數，需任務範圍為「單一標的」'));
            row.appendChild(el('span', 'spacer'));
            stage.appendChild(row);
          });
          var lib = el('a', 'btn btn-sm', '＋ 需要新模板？前往模板庫');
          lib.href = 'settings.html#prompts';
          stage.appendChild(lib);
        }

        if (step === 3) {
          stage.appendChild(el('div', 'pv-note', '命名並確認。建立前先做一次乾跑預檢，確保第一次觸發就成功。'));
          var fld = el('div', 'pv-field');
          fld.appendChild(el('label', null, '任務名稱'));
          var inp = el('input', 'input');
          inp.placeholder = '例：高息部位體檢';
          inp.value = d.name;
          inp.addEventListener('input', function () { d.name = inp.value; });
          fld.appendChild(inp);
          stage.appendChild(fld);
          var scLb = el('label', 'pv-check');
          scLb.style.marginTop = '10px';
          var scCb = el('input');
          scCb.type = 'checkbox';
          scCb.checked = d.self_correct;
          scCb.addEventListener('change', function () { d.self_correct = scCb.checked; renderRail(); });
          scLb.appendChild(scCb);
          scLb.appendChild(el('span', null, '啟動自我校正（AI 大師模型回測評分＋產生 1:1 校正版本）'));
          stage.appendChild(scLb);
        }

        /* ---- footer ---- */
        if (step > 0) {
          var back = el('button', 'btn', '← 上一步');
          back.type = 'button';
          back.addEventListener('click', function () { step--; renderStage(); });
          foot.appendChild(back);
        }
        var canNext = step !== 2 || d.templates.length > 0;
        if (step < 3) {
          var next = el('button', 'btn btn-primary', '下一步 →');
          next.type = 'button';
          next.disabled = !canNext;
          if (!canNext) foot.appendChild(el('span', 'wz-note', '至少勾選一個模板'));
          next.addEventListener('click', function () {
            if (step === 1 && d.scope === 'per_symbol' && d.universe.mode === 'custom' && !(d.universe.symbols || []).length) {
              window.toast('至少選一檔', 'fail', '自選模式需勾選至少一個標的');
              return;
            }
            step++;
            renderStage();
          });
          foot.appendChild(next);
        } else {
          var create = el('button', 'btn btn-primary', '乾跑預檢並建立');
          create.type = 'button';
          create.addEventListener('click', submitCreate);
          foot.appendChild(create);
          foot.appendChild(el('span', 'wz-note', '建立後立即顯示預檢結果 — 第一次觸發前就知道會不會跑'));
        }
      }

      function submitCreate() {
        if (!d.name.trim()) { window.toast('請命名', 'fail', '任務名稱必填'); return; }
        if (!pdApi) { window.toast('無法建立', 'fail', 'API 不可用'); return; }
        var scope = d.trigger === 'on_alert' ? 'on_alert' : d.scope;
        var payload = {
          name: d.name.trim(),
          scope: scope,
          strategy_ids: d.templates.slice(),
          use_system_prompt: true,
          self_correct: d.self_correct,
          universe: scope === 'per_symbol' ? d.universe : null,
          alert_rules: scope === 'on_alert' ? (d.rules === 'all' ? 'all' : d.rules) : null,
          /* on_alert defaults to disabled server-side (R7); others default enabled. */
          enabled: scope === 'on_alert' ? false : true
        };
        pdApi.post('/api/insight-tasks', payload).then(function (it) {
          /* schedule binding for scheduled tasks (not on_alert / manual). */
          if (d.trigger === 'schedule' && it && it.id != null) {
            return pdApi.post('/api/insight-tasks/' + it.id + '/schedule', { cron: d.cron })
              .then(function () { return it; });
          }
          return it;
        }).then(function (it) {
          document.querySelectorAll('.pv-backdrop').forEach(function (n) { n.remove(); });
          window.toast('已建立洞察任務', 'ok',
            payload.name + (payload.enabled ? '' : '：on_alert 任務預設停用，確認監聽規則後再啟用'));
          if (window.ppRefresh) window.ppRefresh();
          /* show the preflight for the freshly-created task. */
          if (it && it.id != null && window.ppPreflight) {
            window.ppPreflight({ id: it.id, name: payload.name, scope: payload.scope });
          }
        }).catch(function (err) {
          window.toast((err && err.message) || '建立失敗', 'fail', err && err.code);
        });
      }

      renderStage();
    }, true);

    /* 加寬精靈 modal */
    var box = document.querySelector('.pv-box.wide');
    if (box) box.style.width = '880px';
    }
  };

  function cronHuman(cron) {
    var map = { '0 8 * * *': '每日 08:00', '0 9 * * 1': '每週一 09:00',
      '0 17 * * 5': '每週五 17:00', '0 8 1 * *': '每月 1 日 08:00' };
    return map[cron] || cron;
  }
})();
