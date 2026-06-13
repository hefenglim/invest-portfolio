/* portfolio-dash — per-symbol history mock (模擬資料).
   Stands in for pricing.get_price_history / get_dividend_events / ledger reads.
   Deterministic (seeded) so the design review is stable across reloads.
   The real backend replaces this file with server-rendered JSON. */
window.PD_HISTORY = (function () {
  'use strict';

  const AS_OF = '2026-06-11';

  /* per-symbol anchors: [startPrice, endPrice(null=缺價), volatility, lastDate] */
  const ANCHOR = {
    '2330':    { start: 472.0,  end: 612.5,  vol: 0.013 },
    '0056':    { start: 35.10,  end: 38.95,  vol: 0.006 },
    '00919':   { start: 22.60,  end: 23.05,  vol: 0.007, lastDate: '2026-05-30', note: '00919 自 2026-05-30 起無報價（來源無資料）' },
    'AAPL':    { start: 178.2,  end: 211.40, vol: 0.012 },
    'MSFT':    { start: 396.0,  end: 498.20, vol: 0.011, lastDate: '2026-06-06', note: '價格過期：最後報價 2026-06-06' },
    'NVDA':    { start: 112.4,  end: 172.35, vol: 0.022 },
    '1155.KL': { start: 9.050,  end: 9.870,  vol: 0.008 }
  };

  /* dividend events per symbol (sums match holdings[].dividend_portion) */
  const DIVIDENDS = {
    '2330':    [{ date: '2026-06-03', type: '現金', gross: 5000, net: 5000, ccy: 'TWD' }],
    '0056':    [{ date: '2025-10-21', type: '現金', gross: 5000, net: 5000, ccy: 'TWD' },
                { date: '2026-04-15', type: '現金', gross: 8500, net: 8500, ccy: 'TWD' }],
    '00919':   [{ date: '2026-02-27', type: '現金', gross: 3000, net: 3000, ccy: 'TWD' }],
    'AAPL':    [{ date: '2025-11-13', type: 'DRIP', gross: 7.20, net: 5.04, reinvest_shares: 0.0241, reinvest_price: 209.10, ccy: 'USD' },
                { date: '2026-02-12', type: 'DRIP', gross: 7.05, net: 4.94, reinvest_shares: 0.0239, reinvest_price: 206.60, ccy: 'USD' },
                { date: '2026-05-20', type: 'DRIP', gross: 7.50, net: 5.25, reinvest_shares: 0.0248, reinvest_price: 211.40, ccy: 'USD' }],
    'MSFT':    [{ date: '2025-12-11', type: 'DRIP', gross: 9.96, net: 6.97, reinvest_shares: 0.0152, reinvest_price: 458.40, ccy: 'USD' },
                { date: '2026-03-12', type: 'DRIP', gross: 9.96, net: 6.97, reinvest_shares: 0.0143, reinvest_price: 487.10, ccy: 'USD' }],
    'NVDA':    [{ date: '2026-03-26', type: 'DRIP', gross: 2.50, net: 1.75, reinvest_shares: 0.0112, reinvest_price: 156.30, ccy: 'USD' }],
    '1155.KL': [{ date: '2026-04-28', type: '淨額', gross: null, net: 170.00, ccy: 'MYR' }]
  };

  /* trade events per symbol (consistent with the ledger mock) */
  const TRADES = {
    '2330':    [{ date: '2026-01-02', side: 'open', shares: 500, price: 480.00 },
                { date: '2026-02-10', side: 'buy', shares: 700, price: 505.00 },
                { date: '2026-05-15', side: 'sell', shares: 200, price: 598.00 }],
    '0056':    [{ date: '2026-01-15', side: 'buy', shares: 8000, price: 35.60 },
                { date: '2026-06-09', side: 'buy', shares: 2000, price: 38.60 }],
    '00919':   [{ date: '2026-05-02', side: 'buy', shares: 5000, price: 23.50 }],
    'AAPL':    [{ date: '2026-01-22', side: 'buy', shares: 35, price: 182.50 },
                { date: '2026-06-05', side: 'sell', shares: 5, price: 200.50 }],
    'MSFT':    [{ date: '2026-01-02', side: 'open', shares: 12, price: 405.00 }],
    'NVDA':    [{ date: '2026-02-18', side: 'buy', shares: 15, price: 118.00 },
                { date: '2026-05-28', side: 'buy', shares: 10, price: 165.20 }],
    '1155.KL': [{ date: '2026-02-04', side: 'buy', shares: 700, price: 8.950 },
                { date: '2026-05-28', side: 'buy', shares: 300, price: 9.620 }]
  };

  /* deterministic PRNG (mulberry32) seeded by symbol */
  function seedOf(sym) {
    let h = 2166136261;
    for (let i = 0; i < sym.length; i++) { h ^= sym.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function rng(seed) {
    let a = seed;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** Weekday daily close series from ~180d back to lastDate (default as_of). */
  function series(symbol) {
    const a = ANCHOR[symbol];
    if (!a) return { available: false, points: [], note: '無歷史價格資料' };
    const last = a.lastDate || AS_OF;
    const dates = [];
    const d0 = new Date('2025-12-12T00:00:00Z');
    const dEnd = new Date(last + 'T00:00:00Z');
    for (let d = new Date(d0); d <= dEnd; d.setUTCDate(d.getUTCDate() + 1)) {
      const dow = d.getUTCDay();
      if (dow !== 0 && dow !== 6) dates.push(d.toISOString().slice(0, 10));
    }
    const n = dates.length;
    const rand = rng(seedOf(symbol));
    /* random walk, then linearly pinned so it starts at a.start and ends at a.end */
    const walk = [0];
    for (let i = 1; i < n; i++) walk.push(walk[i - 1] + (rand() * 2 - 1));
    const w0 = walk[0], w1 = walk[n - 1];
    const points = dates.map((date, i) => {
      const t = n === 1 ? 1 : i / (n - 1);
      const detrended = walk[i] - (w0 + (w1 - w0) * t);
      const base = a.start + (a.end - a.start) * t;
      const close = base * (1 + detrended * a.vol);
      const dp = symbol.endsWith('.KL') ? 3 : 2;
      return { date, close: Number(close.toFixed(dp)) };
    });
    /* pin exact endpoint to the official close */
    if (points.length) points[points.length - 1].close = a.end;
    return { available: true, points, note: a.note || null, last_date: last };
  }

  function events(symbol) {
    return { dividends: DIVIDENDS[symbol] || [], trades: TRADES[symbol] || [] };
  }

  return { series, events, AS_OF };
})();
