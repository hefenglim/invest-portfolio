/* portfolio-dash — 數據變數系統 (variable registry).
   單一事實來源：策略/系統提示詞的可用變數、變數總表、預覽替換全部讀這裡。
   後端接線後由 GET /api/prompt-vars 提供（spec 06）；mock 值僅供預覽渲染。 */
window.PD_VARS = (function () {
  'use strict';

  /* source 標記：ready = 後端已具備｜ingest = 需新增外部資料快照（spec 06） */
  const CATEGORIES = [
    {
      id: 'position', name: '部位與績效', source: 'ready',
      vars: [
        { token: 'holdings_json', name: '持倉明細', scope: '全組合',
          desc: '全部持倉：股數、原始/調整均價、現價、市值、未實現損益、權重、回本率',
          sample: '[{"symbol":"2330","shares":1000,"original_avg":"500.00","adjusted_avg":"495.00","market_price":"612.50","unrealized_pnl":"117500","weight":"0.378","payback_ratio":"0.010"}, …共 7 檔]' },
        { token: 'allocation_json', name: '產業配置', scope: '全組合',
          desc: '各產業權重（報告幣別市值佔比）',
          sample: '{"Semiconductors":"0.378","ETF":"0.305","Tech":"0.196","Financials":"0.088","Banks":"0.033"}' },
        { token: 'kpis_json', name: '組合 KPI', scope: '全組合',
          desc: '總市值、總報酬、XIRR、已實現/未實現、匯損益（報告幣別）',
          sample: '{"total_market_value":"1618683","total_return":"308530","xirr":"0.1832","realized_total":"34931","unrealized_total":"273599"}' },
        { token: 'returns_by_ccy_json', name: '各幣別報酬', scope: '全組合',
          desc: '各幣別已實現/未實現/投入/報酬率（原幣，不可跨幣加總）',
          sample: '{"TWD":{"total_return":"162850","rate":"0.1836"},"USD":{"total_return":"4498.50","rate":"0.2828"},"MYR":{"total_return":"688.30","rate":"0.0973"}}' },
        { token: 'realized_json', name: '已實現損益明細', scope: '全組合',
          desc: '每筆賣出的淨收款、調整成本移除、已實現損益',
          sample: '[{"symbol":"2330","shares_sold":200,"proceeds_net":"119350","realized":"21350","quote_ccy":"TWD"}, …]' },
        { token: 'symbol_detail_json', name: '單一標的全檔', scope: '單一標的',
          desc: 'per_symbol 範圍專用：該標的部位、成本、配息史、交易事件、已實現記錄',
          sample: '{"symbol":"2330","shares":1000,"adjusted_avg":"495.00","dividend_events":[…],"trade_events":[…]}' }
      ]
    },
    {
      id: 'price', name: '價格與技術', source: 'ready',
      vars: [
        { token: 'price_history_json', name: '歷史日線', scope: '單一標的',
          desc: '近 180 個交易日收盤序列（含 staleness 標記）',
          sample: '{"symbol":"2330","points":[{"date":"2026-06-11","close":"612.50"}, …180 點],"stale":false}' },
        { token: 'ma_signals_json', name: '均線位置', scope: '單一標的',
          desc: '現價相對 20/60/120 日均線的位置與乖離率（由日線計算）',
          sample: '{"ma20":"598.40","ma60":"571.20","price_vs_ma20":"+0.0236","price_vs_ma60":"+0.0723"}' },
        { token: 'volatility_json', name: '波動度', scope: '單一標的',
          desc: '30 日年化波動率與最大回撤',
          sample: '{"vol_30d_annualized":"0.284","max_drawdown_90d":"-0.062"}' },
        { token: 'price_vs_cost_json', name: '價格 vs 成本', scope: '單一標的',
          desc: '現價相對原始/調整均價的距離（決策核心比值）',
          sample: '{"price_vs_original":"+0.2250","price_vs_adjusted":"+0.2374"}' }
      ]
    },
    {
      id: 'dividend', name: '股利', source: 'ready',
      vars: [
        { token: 'dividends_json', name: '配息史', scope: '全組合',
          desc: '帳本全部股利記錄（type/gross/net/再投資，含幣別）',
          sample: '[{"symbol":"0056","date":"2026-04-15","type":"cash","net":"8500","ccy":"TWD"}, …]' },
        { token: 'ex_dividend_calendar_json', name: '除息日曆', scope: '全組合',
          desc: '未來已宣告除息事件（除息日/發放日/每股金額）',
          sample: '[{"symbol":"2330","ex_date":"2026-06-20","cash_amount":"5.00","currency":"TWD"}, …]' },
        { token: 'dividend_projection_json', name: '年度股利預估', scope: '全組合',
          desc: '年內已宣告股利現金流預估（各幣別分列，稅後淨額）',
          sample: '{"TWD":{"declared_net":"13500","events":2},"MYR":{"declared_net":"320.00","events":1}}' }
      ]
    },
    {
      id: 'fx', name: '匯率', source: 'ready',
      vars: [
        { token: 'fx_json', name: '換匯損益', scope: '全組合',
          desc: '各帳戶外幣池均價、現匯、已實現/未實現匯損益（股+現金拆分）',
          sample: '{"schwab":{"avg_rate":"31.80","current_spot":"32.90","unrealized_fx_stocks":"13552"}, …}' },
        { token: 'fx_rates_json', name: '即期匯率', scope: '全組合',
          desc: '報告幣別對各持倉幣別的最新匯率與取得時間',
          sample: '{"USD_TWD":"32.90","MYR_TWD":"7.05","as_of":"2026-06-11T14:30:00+08:00"}' }
      ]
    },
    {
      id: 'chips', name: '籌碼與基本面（FinMind）', source: 'ingest',
      vars: [
        { token: 'institutional_json', name: '法人買賣超', scope: '單一標的',
          desc: '外資/投信/自營近 20 日買賣超與連買連賣天數（台股）',
          sample: '{"symbol":"2330","foreign_net_20d":"+48200","consecutive_buy_days":6}' },
        { token: 'margin_json', name: '融資融券', scope: '單一標的',
          desc: '融資餘額/融券餘額近 20 日變化（台股）',
          sample: '{"margin_balance_chg_20d":"-0.031","short_balance_chg_20d":"+0.012"}' },
        { token: 'monthly_revenue_json', name: '月營收', scope: '單一標的',
          desc: '近 12 個月營收與 YoY/MoM（台股）',
          sample: '{"latest":{"month":"2026-05","yoy":"+0.31","mom":"+0.04"},"trailing_12m":[…]}' },
        { token: 'valuation_json', name: '估值（PER/PBR）', scope: '單一標的',
          desc: '本益比/股價淨值比與 5 年歷史百分位',
          sample: '{"per":"24.1","per_5y_percentile":"0.78","pbr":"6.2"}' },
        { token: 'financials_json', name: '季度財報摘要', scope: '單一標的',
          desc: '近 4 季營收/毛利率/EPS（台股；美股 v2）',
          sample: '{"quarters":[{"q":"2026Q1","revenue_yoy":"+0.28","gross_margin":"0.532","eps":"14.2"}, …]}' }
      ]
    },
    {
      id: 'sentiment', name: '市場情緒', source: 'ingest',
      vars: [
        { token: 'market_sentiment_json', name: '情緒指標', scope: '全組合',
          desc: 'VIX、Fear & Greed 指數與所處區間',
          sample: '{"vix":"14.2","vix_zone":"low","fear_greed":62,"fear_greed_zone":"greed"}' },
        { token: 'index_quotes_json', name: '大盤指數', scope: '全組合',
          desc: '加權指數/S&P 500/KLCI 近 20 日漲跌',
          sample: '{"TAIEX":{"chg_20d":"+0.042"},"SPX":{"chg_20d":"+0.031"},"KLCI":{"chg_20d":"+0.008"}}' }
      ]
    },
    {
      id: 'ai', name: 'AI 自身（校正用）', source: 'ready',
      vars: [
        { token: 'backtest_json', name: '回測命中分佈', scope: '全組合',
          desc: '該洞察組合的歷史命中率信心分桶 — 校正提示詞錨定信心值用',
          sample: '{"bins":[{"conf":"0.7-0.8","actual_rate":"0.66","n":6}],"overall_hit_rate":"0.625"}' },
        { token: 'calibration_gap_json', name: '校準誤差', scope: '全組合',
          desc: '該組合信心 vs 實際命中的 rolling 偏差',
          sample: '{"gap":"+0.085","window_n":16}' }
      ]
    },
    {
      id: 'system', name: '系統狀態', source: 'ready',
      vars: [
        { token: 'freshness_json', name: '資料新鮮度', scope: '全組合',
          desc: '缺價/過期標的清單 — 讓 AI 知道哪些數字不可信',
          sample: '{"missing_prices":["00919"],"stale":[{"symbol":"MSFT","as_of":"2026-06-06"}]}' },
        { token: 'as_of', name: '資料時間', scope: '全組合',
          desc: '本次快照的資料時間戳',
          sample: '"2026-06-11T14:30:00+08:00"' }
      ]
    }
  ];

  function all() {
    const out = [];
    CATEGORIES.forEach((c) => c.vars.forEach((v) => out.push(Object.assign({ category: c.name, source: c.source }, v))));
    return out;
  }

  function find(token) {
    return all().find((v) => v.token === token) || null;
  }

  /* 預覽替換：把 {{token}} 換成 mock sample；未知 token 標紅保留。
     symbol：per_symbol 範圍的代入標的（範例值以 2330 為模板，代入時換成選定代號示意） */
  function render(text, symbol) {
    return text.replace(/\{\{([a-z0-9_]+)\}\}/gi, (m, token) => {
      const v = find(token);
      if (!v) return '⚠未知變數 ' + m;
      return symbol && v.scope === '單一標的' ? v.sample.replace(/2330/g, symbol) : v.sample;
    });
  }

  /* 萃取文中引用的變數 token */
  function tokensIn(text) {
    const out = [];
    (text.match(/\{\{([a-z0-9_]+)\}\}/gi) || []).forEach((m) => {
      const t = m.slice(2, -2);
      if (!out.includes(t)) out.push(t);
    });
    return out;
  }

  return { CATEGORIES, all, find, render, tokensIn };
})();
