/* portfolio-dash — 洞察管線中心：建立精靈（4 步）＋右側即時管線預覽.
   核心理念：使用者每做一個選擇，右側的「管線」就長出一節 — 規劃即所見。
   相容性（R1）在勾選當下就擋，而不是執行時才爆。 */
(function () {
  'use strict';
  const P = window.PIPE;
  const el = window.ppEl;

  const STEPS = ['觸發', '範圍', '組裝', '確認'];

  window.ppWizard = function () {
    /* 草稿狀態 */
    const d = {
      name: '', trigger: 'schedule', period: '每日', time: '08:00',
      rules: 'all', scope: 'portfolio', universe: { mode: 'all' },
      templates: [], self_correct: true
    };
    let step = 0;

    window.ppModal('新增洞察任務', (body) => {
      body.classList.add('wz-body');
      const shell = el('div', 'wz-shell');
      const main = el('div', 'wz-main');
      const rail = el('div', 'wz-rail');
      shell.appendChild(main);
      shell.appendChild(rail);
      body.appendChild(shell);

      /* ---- 右側即時管線預覽 ---- */
      function renderRail() {
        rail.replaceChildren();
        rail.appendChild(el('div', 'wz-rail-title', '管線預覽 — 即時組裝'));
        const stack = el('div', 'pp-stack');
        const layer = (tag, name, cls) => {
          const l = el('div', 'pp-layer' + (cls ? ' ' + cls : ''));
          l.appendChild(el('span', 'tag', tag));
          l.appendChild(el('span', 'name', name));
          return l;
        };
        stack.appendChild(layer('觸發', d.trigger === 'schedule' ? d.period + ' ' + d.time
          : d.trigger === 'on_alert' ? '預警觸發（' + (d.rules === 'all' ? '全部規則' : d.rules.length + ' 條') + '）'
          : '手動（暫不排程）'));
        stack.appendChild(layer('輸入', d.scope === 'portfolio' ? '全組合・1 卡'
          : d.scope === 'on_alert' ? '事件標的'
          : (d.universe.mode === 'all' ? '全部持倉 ' + P.HELD.length : '自選 ' + d.universe.symbols.length) + ' 檔・每檔 1 卡'));
        stack.appendChild(layer('守則', '全域守則（共用）'));
        if (!d.templates.length) stack.appendChild(layer('模板', '－ 尚未選擇 －', 'l-off'));
        d.templates.forEach((tid, i) => {
          const t = P.tplOf(tid);
          stack.appendChild(layer('模板' + (i + 1), t ? t.name : tid));
        });
        if (d.self_correct) stack.appendChild(layer('校正', '自我校正（累積樣本後生成）', 'l-calib'));
        stack.appendChild(layer('產出', d.scope === 'per_symbol'
          ? (d.universe.mode === 'all' ? P.HELD.length : d.universe.symbols.length) + ' 張洞察卡/次'
          : '1 張洞察卡/次'));
        rail.appendChild(stack);
        const est = el('div', 'wz-note');
        est.style.marginTop = '8px';
        const n = d.scope === 'per_symbol' ? (d.universe.mode === 'all' ? P.HELD.length : d.universe.symbols.length) : 1;
        est.textContent = '估算成本：~$' + (n * 0.01).toFixed(2) + ' / 次（額度餘 $' + P.health.quota.remaining + '）';
        rail.appendChild(est);
      }

      /* ---- 步驟頭 ---- */
      const stepsBar = el('div', 'wz-steps');
      function renderSteps() {
        stepsBar.replaceChildren();
        STEPS.forEach((s, i) => {
          const st = el('div', 'wz-step' + (i === step ? ' cur' : i < step ? ' done' : ''));
          st.appendChild(el('span', 'n', i < step ? '✓' : String(i + 1)));
          st.appendChild(el('span', null, s));
          stepsBar.appendChild(st);
        });
      }

      const stage = el('div');
      const foot = el('div', 'wz-foot');
      main.appendChild(stepsBar);
      main.appendChild(stage);
      main.appendChild(foot);

      const opt = (title, sub, sel, fn) => {
        const o = el('button', 'wz-opt' + (sel ? ' sel' : ''));
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
          const grid = el('div', 'wz-opts');
          grid.appendChild(opt('定期排程', '寫入運行中心，依週期自動執行', d.trigger === 'schedule', () => { d.trigger = 'schedule'; if (d.scope === 'on_alert') d.scope = 'portfolio'; renderStage(); }));
          grid.appendChild(opt('預警觸發', '預警規則命中時自動解讀（不可排程）', d.trigger === 'on_alert', () => { d.trigger = 'on_alert'; d.scope = 'on_alert'; renderStage(); }));
          grid.appendChild(opt('先不排程', '手動執行；之後可隨時掛排程', d.trigger === 'manual', () => { d.trigger = 'manual'; if (d.scope === 'on_alert') d.scope = 'portfolio'; renderStage(); }));
          stage.appendChild(grid);
          if (d.trigger === 'schedule') {
            const row = el('div', 'wz-fields');
            const period = el('select', 'select');
            ['每日', '每週一', '每週五', '每月 1 日'].forEach((p) => {
              const o = el('option', null, p); o.value = p; if (p === d.period) o.selected = true; period.appendChild(o);
            });
            period.addEventListener('change', () => { d.period = period.value; renderRail(); });
            const time = el('input', 'input');
            time.type = 'time'; time.value = d.time;
            time.addEventListener('change', () => { d.time = time.value; renderRail(); });
            row.appendChild(period);
            row.appendChild(time);
            stage.appendChild(row);
          }
          if (d.trigger === 'on_alert') {
            const note = el('div', 'wz-note');
            note.style.marginTop = '10px';
            note.textContent = '監聽規則（預設全部；可自選）：';
            stage.appendChild(note);
            const grid2 = el('div', 'pv-symgrid');
            grid2.style.marginTop = '6px';
            P.ALERT_RULES.forEach(([id, nm]) => {
              const lb = el('label', 'pv-check');
              const cb = el('input');
              cb.type = 'checkbox';
              cb.checked = d.rules === 'all' || (Array.isArray(d.rules) && d.rules.includes(id));
              cb.addEventListener('change', () => {
                const picked = Array.from(grid2.querySelectorAll('input:checked')).map((x) => x.value);
                d.rules = picked.length === P.ALERT_RULES.length ? 'all' : picked;
                renderRail();
              });
              cb.value = id;
              lb.appendChild(cb);
              lb.appendChild(el('span', null, nm));
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
            const grid = el('div', 'wz-opts');
            grid.style.gridTemplateColumns = 'repeat(2, 1fr)';
            grid.appendChild(opt('全組合', '單一快照、產 1 張卡', d.scope === 'portfolio', () => { d.scope = 'portfolio'; d.templates = d.templates.filter((tid) => { const t = P.tplOf(tid); return t && t.scope !== 'per_symbol'; }); renderStage(); }));
            grid.appendChild(opt('單一標的', '每檔一張卡；可用個股級變數', d.scope === 'per_symbol', () => { d.scope = 'per_symbol'; renderStage(); }));
            stage.appendChild(grid);
            if (d.scope === 'per_symbol') {
              const note = el('div', 'wz-note');
              note.style.margin = '10px 0 6px';
              note.textContent = '標的宇宙（出清/移出觀察清單會自動移除；清單空會自動停用＋預警）：';
              stage.appendChild(note);
              const grid2 = el('div', 'wz-opts');
              grid2.style.gridTemplateColumns = 'repeat(2, 1fr)';
              grid2.appendChild(opt('全部持倉', P.HELD.length + ' 檔・自動跟隨持倉變動', d.universe.mode === 'all', () => { d.universe = { mode: 'all' }; renderStage(); }));
              grid2.appendChild(opt('自選標的', '持倉＋觀察清單勾選', d.universe.mode === 'custom', () => { d.universe = { mode: 'custom', symbols: d.universe.symbols || [] }; renderStage(); }));
              stage.appendChild(grid2);
              if (d.universe.mode === 'custom') {
                const grid3 = el('div', 'pv-symgrid');
                grid3.style.marginTop = '8px';
                const allSyms = P.HELD.map((s) => [s, '持倉']).concat(P.WATCH.map(([s, nm]) => [s + ' ' + nm, '觀察', s]));
                allSyms.forEach(([label, tag, val]) => {
                  const lb = el('label', 'pv-check');
                  const cb = el('input');
                  cb.type = 'checkbox';
                  cb.value = val || label;
                  cb.checked = (d.universe.symbols || []).includes(cb.value);
                  cb.addEventListener('change', () => {
                    d.universe.symbols = Array.from(grid3.querySelectorAll('input:checked')).map((x) => x.value);
                    renderRail();
                  });
                  lb.appendChild(cb);
                  lb.appendChild(el('span', null, label));
                  lb.appendChild(el('span', 'pv-symtag' + (tag === '觀察' ? ' watch' : ''), tag));
                  grid3.appendChild(lb);
                });
                stage.appendChild(grid3);
              }
            }
          }
        }

        if (step === 2) {
          stage.appendChild(el('div', 'pv-note',
            '選擇分析模板（可多選、依序串接於同一次呼叫）。與範圍不相容的模板會直接鎖定 — 不會等到執行才爆（R1）。'));
          P.templates.forEach((t) => {
            const blocked = t.scope === 'per_symbol' && d.scope !== 'per_symbol';
            const row = el('div', 'wz-tpl-row' + (blocked ? ' blocked' : ''));
            const ix = d.templates.indexOf(t.id);
            row.appendChild(el('span', 'ord', ix >= 0 ? String(ix + 1) : ''));
            const cb = el('input');
            cb.type = 'checkbox';
            cb.disabled = blocked;
            cb.checked = ix >= 0;
            cb.addEventListener('change', () => {
              if (cb.checked) d.templates.push(t.id);
              else d.templates = d.templates.filter((x) => x !== t.id);
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
          const lib = el('a', 'btn btn-sm', '＋ 需要新模板？前往模板庫');
          lib.href = 'settings-prompts.html';
          stage.appendChild(lib);
        }

        if (step === 3) {
          stage.appendChild(el('div', 'pv-note', '命名並確認。建立前先做一次乾跑預檢，確保第一次觸發就成功。'));
          const fld = el('div', 'pv-field');
          fld.appendChild(el('label', null, '任務名稱'));
          const inp = el('input', 'input');
          inp.placeholder = '例：高息部位體檢';
          inp.value = d.name;
          inp.addEventListener('input', () => { d.name = inp.value; });
          fld.appendChild(inp);
          stage.appendChild(fld);
          const scLb = el('label', 'pv-check');
          scLb.style.marginTop = '10px';
          const scCb = el('input');
          scCb.type = 'checkbox';
          scCb.checked = d.self_correct;
          scCb.addEventListener('change', () => { d.self_correct = scCb.checked; renderRail(); });
          scLb.appendChild(scCb);
          scLb.appendChild(el('span', null, '啟動自我校正（AI 大師模型回測評分＋產生 1:1 校正版本）'));
          stage.appendChild(scLb);
        }

        /* ---- footer ---- */
        if (step > 0) {
          const back = el('button', 'btn', '← 上一步');
          back.type = 'button';
          back.addEventListener('click', () => { step--; renderStage(); });
          foot.appendChild(back);
        }
        const canNext = step !== 2 || d.templates.length > 0;
        if (step < 3) {
          const next = el('button', 'btn btn-primary', '下一步 →');
          next.type = 'button';
          next.disabled = !canNext;
          if (!canNext) foot.appendChild(el('span', 'wz-note', '至少勾選一個模板'));
          next.addEventListener('click', () => {
            if (step === 1 && d.scope === 'per_symbol' && d.universe.mode === 'custom' && !(d.universe.symbols || []).length) {
              window.toast('至少選一檔', 'fail', '自選模式需勾選至少一個標的');
              return;
            }
            step++;
            renderStage();
          });
          foot.appendChild(next);
        } else {
          const create = el('button', 'btn btn-primary', '乾跑預檢並建立');
          create.type = 'button';
          create.addEventListener('click', () => {
            if (!d.name.trim()) {
              window.toast('請命名', 'fail', '任務名稱必填');
              return;
            }
            const t = {
              id: 'it-new-' + Date.now(), name: d.name.trim(),
              scope: d.trigger === 'on_alert' ? 'on_alert' : d.scope,
              trigger: d.trigger === 'schedule'
                ? { kind: 'schedule', human: d.period + ' ' + d.time, cron: '（後端轉換）', next: '依週期計算' }
                : d.trigger === 'on_alert'
                ? { kind: 'on_alert', human: '預警觸發', rules: d.rules === 'all' ? null : d.rules }
                : { kind: 'manual', human: '未排程' },
              universe: d.scope === 'per_symbol' ? d.universe : undefined,
              templates: d.templates.slice(),
              self_correct: d.self_correct,
              enabled: d.trigger !== 'on_alert', /* on_alert 預設停用，啟用後才參與觸發（R7） */
              lastRun: { at: '—', status: 'idle', summary: '尚未執行', notes: [] }
            };
            P.tasks.unshift(t);
            document.querySelectorAll('.pv-backdrop').forEach((n) => n.remove());
            window.ppRenderCards();
            window.toast('已建立洞察任務', 'ok', t.name + (t.enabled ? '' : '：on_alert 任務預設停用，確認監聽規則後再啟用') + '（設計稿）');
            window.ppPreflight(t);
          });
          foot.appendChild(create);
          foot.appendChild(el('span', 'wz-note', '建立後立即顯示預檢結果 — 第一次觸發前就知道會不會跑'));
        }
      }
      renderStage();
    }, true);

    /* 加寬精靈 modal */
    const box = document.querySelector('.pv-box.wide');
    if (box) box.style.width = '880px';
  };
})();
