/* portfolio-dash — 風險預警 (規則引擎，非 LLM) + 常駐 AI 額度 chip.
   Rules run client-side over the computed DashboardData snapshot — the backend
   version belongs in strategy/ (spec 03). Thresholds are user-configurable in
   設定 › 預警規則 (localStorage 'pd_alert_rules'); a storage listener keeps
   other open pages' bells in sync without reload. */
(function () {
  'use strict';
  const f = window.fmt;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* mock LLM budget + AI score state (後端接線後由 dashboard payload 提供) */
  window.PD_QUOTA = window.PD_QUOTA || { remaining: 0.84, threshold: 1.00 };
  window.PD_AI_SCORE = window.PD_AI_SCORE || { calibration_gap: 0.085 };

  const DEFAULTS = { single_weight: 0.30, sector_weight: 0.60, fx_drift: 0.03, exdiv_days: 14, calib_gap: 0.15 };

  /* 讀取使用者自訂門檻（設定›預警規則 E1）— 每次計算時重讀，storage 事件可即時生效 */
  function ruleConfig() {
    const RULES = Object.assign({}, DEFAULTS);
    const ON = { single_weight: true, sector_weight: true, fx_drift: true, exdiv_days: true,
      stale_price: true, quota_low: true, calib_gap: true };
    try {
      const saved = JSON.parse(localStorage.getItem('pd_alert_rules') || '{}');
      const pct = (k) => {
        if (!saved[k]) return;
        if (saved[k].value) RULES[k] = saved[k].value / 100;
        if (saved[k].enabled === false) ON[k] = false;
      };
      pct('single_weight'); pct('sector_weight'); pct('fx_drift'); pct('calib_gap');
      if (saved.exdiv_days) {
        if (saved.exdiv_days.value) RULES.exdiv_days = saved.exdiv_days.value;
        if (saved.exdiv_days.enabled === false) ON.exdiv_days = false;
      }
      if (saved.stale_price && saved.stale_price.enabled === false) ON.stale_price = false;
      if (saved.quota_low) {
        if (saved.quota_low.value) window.PD_QUOTA.threshold = saved.quota_low.value;
        if (saved.quota_low.enabled === false) ON.quota_low = false;
      }
    } catch (e) { /* defaults */ }
    return { RULES, ON };
  }

  function computeAlerts(D) {
    const cfg = ruleConfig();
    const RULES = cfg.RULES;
    const ON = cfg.ON;
    const alerts = [];
    const today = (D.as_of || '').slice(0, 10);

    /* 1. 單一標的集中度 */
    if (ON.single_weight) (D.holdings || []).forEach((h) => {
      if (h.weight !== null && h.weight !== undefined && h.weight > RULES.single_weight) {
        alerts.push({ sev: 'risk', title: '單一標的集中度：' + h.symbol + ' ' + f.pct(h.weight),
          detail: h.name + ' 權重超過 ' + (RULES.single_weight * 100).toFixed(0) + '% 門檻，個股事件風險放大。',
          href: 'index.html#sym=' + encodeURIComponent(h.symbol) });
      }
    });

    /* 2. 產業集中度 */
    if (ON.sector_weight && D.allocation && D.allocation.weights) {
      const w = D.allocation.weights;
      const techish = (w['Semiconductors'] || 0) + (w['Tech'] || 0);
      if (techish > RULES.sector_weight) {
        alerts.push({ sev: 'risk', title: '產業集中度：半導體＋科技 ' + f.pct(techish),
          detail: '合計權重超過 ' + (RULES.sector_weight * 100).toFixed(0) + '%，產業反轉時組合波動將顯著放大。',
          href: 'index.html' });
      }
    }

    /* 3. 缺價 / 過期 */
    if (ON.stale_price && D.freshness) {
      (D.freshness.missing_prices || []).forEach((s) => {
        alerts.push({ sev: 'warn', title: '缺價：' + s,
          detail: '無任何儲存價格，市值與權重不含此標的。', href: 'index.html#freshness' });
      });
      (D.freshness.prices || []).forEach((p) => {
        if (p.stale && p.as_of) {
          alerts.push({ sev: 'warn', title: '價格過期：' + p.symbol,
            detail: '最後報價 ' + p.as_of + '，未實現損益以舊價計算。', href: 'index.html#freshness' });
        }
      });
    }

    /* 4. 匯率曝險 */
    if (ON.fx_drift && D.fx && D.fx.by_account) {
      Object.values(D.fx.by_account).forEach((a) => {
        if (a.avg_rate === null || a.current_spot === null) return;
        const drift = (a.current_spot - a.avg_rate) / a.avg_rate;
        if (Math.abs(drift) > RULES.fx_drift) {
          const dir = drift > 0 ? '順風' : '逆風';
          alerts.push({ sev: 'info', title: a.foreign_ccy + ' 匯率' + dir + ' ' + f.signedPct(drift),
            detail: '取得均價 ' + f.rate(a.avg_rate) + ' vs 現匯 ' + f.rate(a.current_spot) +
              '，未實現匯損益依賴匯率持續性。', href: 'index.html' });
        }
      });
    }

    /* 5. 即將除息 */
    if (ON.exdiv_days) (D.ex_dividend_calendar || []).forEach((e) => {
      if (!today || !e.ex_date) return;
      const days = Math.round((new Date(e.ex_date) - new Date(today)) / 86400000);
      if (days >= 0 && days <= RULES.exdiv_days) {
        alerts.push({ sev: 'info', title: '即將除息：' + e.symbol + '（' + days + ' 天後）',
          detail: e.name + ' ' + e.ex_date + ' 除息，每股 ' + f.price(e.cash_amount, e.currency) + ' ' + e.currency + '。',
          href: 'index.html#sym=' + encodeURIComponent(e.symbol) });
      }
    });

    /* 6. AI 額度 */
    const q = window.PD_QUOTA;
    if (q.remaining !== null && q.remaining <= 0) {
      alerts.push({ sev: 'risk', title: 'AI 額度用盡', detail: 'AI Agents 與洞察產生已暫停，前往設定重置額度。', href: 'settings.html#llm' });
    } else if (ON.quota_low && q.remaining !== null && q.remaining < q.threshold) {
      alerts.push({ sev: 'warn', title: 'AI 額度偏低 $' + q.remaining.toFixed(2),
        detail: '低於警示閾值 $' + q.threshold.toFixed(2) + '，AI 輸入與洞察可能中斷。', href: 'settings.html#llm' });
    }

    /* 7. AI 校準誤差 (F4 — AI 品質本身進入預警閉環，與洞察卡上的校準 chip 同源) */
    if (ON.calib_gap && window.PD_AI_SCORE && typeof window.PD_AI_SCORE.calibration_gap === 'number') {
      const gap = window.PD_AI_SCORE.calibration_gap;
      if (Math.abs(gap) > RULES.calib_gap) {
        alerts.push({ sev: 'warn',
          title: 'AI 校準誤差 ' + (gap > 0 ? '+' : '−') + Math.abs(gap * 100).toFixed(1) + 'pp',
          detail: 'AI 預測信心與實際命中率偏差超過 ' + (RULES.calib_gap * 100).toFixed(0) +
            'pp 門檻（信心' + (gap > 0 ? '高' : '低') + '於實際）— 檢查自我校正管線是否生效。',
          href: 'insights.html' });
      }
    }

    const order = { risk: 0, warn: 1, info: 2 };
    alerts.sort((a, b) => order[a.sev] - order[b.sev]);
    return alerts;
  }

  /* Is this the dashboard? Only there do we consume the backend D.alerts + D.llm_quota
     (from the shared /api/dashboard promise). Other pages still run the legacy
     client-compute / cache path (their mock-data + history-mock are retired in Task 2.7). */
  const isDashboard = document.body.dataset.page === 'dashboard';

  /* Map a backend Alert.href onto the static-frontend routing scheme. Backend hrefs are
     server-route shaped (/symbol/{sym}, /settings, …) with no matching StaticFiles page,
     so we translate them HERE in the renderer (route knowledge stays out of api.js).
     Returns { href, sym? }: sym set when the alert points at a symbol (→ drawer). */
  function mapAlertHref(href) {
    if (!href) return { href: '#' };
    const sym = href.match(/^\/symbol\/(.+)$/);
    if (sym) return { href: 'index.html#sym=' + encodeURIComponent(sym[1]), sym: sym[1] };
    if (href.match(/#sym=(.+)$/)) {
      return { href: href, sym: decodeURIComponent(href.match(/#sym=(.+)$/)[1]) };
    }
    if (href === '/settings') return { href: 'settings.html' };
    if (href === '/settings#llm') return { href: 'settings.html#llm' };
    if (href === '/insights') return { href: 'insights.html' };
    if (href === '/pipeline') return { href: 'AI Pipeline Hub.html' };
    return { href: href };  // already a static page (e.g. settings.html#llm) — pass through
  }

  /* Non-dashboard cache path: read the last cached alerts written by the dashboard. */
  function cachedAlerts() {
    try { return JSON.parse(localStorage.getItem('pd_alerts_cache') || '[]'); } catch (e) { return []; }
  }

  let alerts = isDashboard ? [] : cachedAlerts();
  window.PD_ALERTS = alerts;

  /* ---- topbar UI: quota chip + bell ---- */
  const tb = document.getElementById('topbar');
  if (!tb) return;
  const spacer = tb.querySelector('.header-spacer');

  /* AI 額度 chip（常駐）。On the dashboard the value is a Decimal STRING from
     D.llm_quota.remaining_usd (formatted via fmt, never .toFixed); on other pages it
     is the legacy mock PD_QUOTA number. `remaining` may be null = 無上限. */
  const chip = el('a', 'badge quota-chip');
  chip.href = 'settings.html#llm';
  chip.title = '剩餘 AI 額度（點擊前往額度設定）';
  /* 2-dp amount for the chip. The dashboard always loads format.js, so the Decimal
     STRING from D.llm_quota.remaining_usd goes through fmt there (per spec). On the
     non-dashboard pages that load alerts.js WITHOUT format.js, fall back to a Number
     formatter for the legacy mock value (never .toFixed on a wire string). */
  function money2(v) {
    if (window.fmt) return window.fmt.num(v, 2);
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function renderQuotaChip(remaining, threshold) {
    chip.className = 'badge quota-chip';
    chip.replaceChildren();
    if (remaining === null || remaining === undefined) {
      chip.classList.add('badge-fresh-ok');
      chip.textContent = 'AI 額度 無上限';
      return;
    }
    const rNum = Number(remaining);            // Decimal string OR number → number for compare
    const tNum = threshold == null ? null : Number(threshold);
    const amt = money2(remaining);             // fmt (dashboard) or Number fallback (other pages)
    if (rNum <= 0) {
      chip.classList.add('badge-missing');
      chip.textContent = 'AI 額度 $' + money2(0);
    } else if (tNum !== null && rNum < tNum) {
      chip.classList.add('badge-fresh-stale');
      chip.appendChild(el('span', 'dot'));
      chip.appendChild(document.createTextNode('AI 額度 $' + amt));
    } else {
      chip.textContent = 'AI 額度 $' + amt;
    }
  }
  /* synchronous initial paint: dashboard threshold is not known until data arrives, so
     render from the legacy mock first (other pages keep this as their final state). */
  const mq = window.PD_QUOTA || {};
  renderQuotaChip(mq.remaining, mq.threshold);

  /* bell */
  const bellWrap = el('div', 'bell-wrap');
  const bell = el('button', 'bell-btn');
  bell.type = 'button';
  bell.title = '風險預警（規則引擎，每次資料更新時重算）';
  bell.appendChild(el('span', 'bell-ico', '⚠'));
  bellWrap.appendChild(bell);
  const panel = el('div', 'bell-panel');
  panel.hidden = true;
  bellWrap.appendChild(panel);

  function renderCount() {
    const old = bell.querySelector('.bell-count');
    if (old) old.remove();
    const riskCount = alerts.filter((a) => a.sev === 'risk').length;
    if (alerts.length) {
      bell.appendChild(el('span', 'bell-count' + (riskCount ? ' risk' : ''), String(alerts.length)));
    }
  }

  function fillPanel() {
    panel.replaceChildren();
    const head = el('div', 'bell-head');
    head.appendChild(el('span', null, '風險預警'));
    head.appendChild(el('span', 'bell-sub', '規則引擎・非 AI 生成'));
    panel.appendChild(head);
    if (!alerts.length) {
      panel.appendChild(el('div', 'bell-empty', '目前無預警事項'));
      return;
    }
    alerts.forEach((a) => {
      const mapped = mapAlertHref(a.href);
      const item = el('a', 'bell-item sev-' + a.sev);
      item.href = mapped.href || '#';
      /* 含個股代號的預警 → 就地彈出抽屜，不跳轉（保留 href 供中鍵/可及性） */
      if (mapped.sym && window.pdOpenSymbol) {
        item.addEventListener('click', (ev) => {
          ev.preventDefault();
          panel.hidden = true;
          window.pdOpenSymbol(mapped.sym);
        });
      }
      item.appendChild(el('span', 'sev-dot'));
      const txt = el('div', 'bell-txt');
      txt.appendChild(el('div', 'bell-title', a.title));
      txt.appendChild(el('div', 'bell-detail', a.detail));
      item.appendChild(txt);
      panel.appendChild(item);
    });
  }

  renderCount();
  fillPanel();

  bell.addEventListener('click', (e) => {
    e.stopPropagation();
    panel.hidden = !panel.hidden;
  });
  document.addEventListener('click', (e) => {
    if (!panel.hidden && !bellWrap.contains(e.target)) panel.hidden = true;
  });

  /* 體驗債修復：其他已開啟頁面在規則變更後即時同步（無需重新整理）。
     storage 事件只在「別的分頁」觸發 — 正是要修的場景。dashboard 以後端 D.alerts
     為準，不隨 storage 重算（避免和後端來源分歧）。 */
  window.addEventListener('storage', (e) => {
    if (isDashboard) return;
    if (e.key !== 'pd_alert_rules' && e.key !== 'pd_alerts_cache') return;
    alerts = cachedAlerts();
    window.PD_ALERTS = alerts;
    renderCount();
    fillPanel();
  });

  if (spacer && spacer.nextSibling) {
    tb.insertBefore(chip, spacer.nextSibling);
    tb.insertBefore(bellWrap, chip);
  } else {
    tb.appendChild(bellWrap);
    tb.appendChild(chip);
  }

  /* ---- dashboard boot: consume the SAME shared /api/dashboard payload ---- */
  /* On the dashboard, replace the synchronous mock paint with backend data: D.alerts
     (the embedded rule-engine output — no client recompute) and D.llm_quota.remaining_usd
     (Decimal string). Cache the alerts so other open pages' bells stay in sync. On
     failure (non-401; api.js handles 401) leave the empty state — never throw (the e2e
     smoke asserts ZERO console errors / pageerrors). */
  if (isDashboard) {
    (async function bootDashboardAlerts() {
      let D;
      try {
        D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
      } catch (e) {
        return;  // app.js surfaces the load-failure UI; bell/chip stay in their default state.
      }
      alerts = Array.isArray(D.alerts) ? D.alerts : [];
      window.PD_ALERTS = alerts;
      try { localStorage.setItem('pd_alerts_cache', JSON.stringify(alerts)); } catch (e) { /* noop */ }
      const quota = D.llm_quota || {};
      renderQuotaChip(quota.remaining_usd, mq.threshold);
      renderCount();
      fillPanel();
    })();
  }
})();
