/* portfolio-dash — 輸入中心 mock data */
window.INPUT_DATA = {
  "accounts": [
    { "id": "tw_broker", "name": "台灣券商", "ccy": "TWD", "div_model": "tw" },
    { "id": "schwab", "name": "嘉信 Schwab", "ccy": "USD", "div_model": "drip" },
    { "id": "moomoo_my_us", "name": "Moomoo 美股", "ccy": "USD", "div_model": "drip" },
    { "id": "moomoo_my_my", "name": "Moomoo 馬股", "ccy": "MYR", "div_model": "net" }
  ],
  "fee_rules": {
    "tw_broker":    { "rate": 0.001425, "discount": 1.0, "min_fee": 20, "round_int": true,  "tax_sell": 0.003, "tax_sell_etf": 0.001, "label": "0.1425%・最低 NT$20・賣出證交稅 0.3%（ETF 0.1%）" },
    "schwab":       { "rate": 0, "discount": 1.0, "min_fee": 0, "round_int": false, "tax_sell": 0, "label": "$0 佣金" },
    "moomoo_my_us": { "rate": 0, "discount": 1.0, "min_fee": 0.99, "round_int": false, "tax_sell": 0, "label": "平台費 USD 0.99/筆" },
    "moomoo_my_my": { "rate": 0.0008, "discount": 1.0, "min_fee": 3.00, "round_int": false, "tax_sell": 0.001, "label": "0.08%・最低 RM3・印花稅 0.1%" }
  },
  "instruments": [
    { "symbol": "2330", "name": "台積電", "market": "TW", "ccy": "TWD", "etf": false },
    { "symbol": "0056", "name": "元大高股息", "market": "TW", "ccy": "TWD", "etf": true },
    { "symbol": "00919", "name": "群益台灣精選高息", "market": "TW", "ccy": "TWD", "etf": true },
    { "symbol": "6488", "name": "環球晶", "market": "TW", "ccy": "TWD", "etf": false },
    { "symbol": "AAPL", "name": "Apple", "market": "US", "ccy": "USD", "etf": false },
    { "symbol": "MSFT", "name": "Microsoft", "market": "US", "ccy": "USD", "etf": false },
    { "symbol": "NVDA", "name": "NVIDIA", "market": "US", "ccy": "USD", "etf": false },
    { "symbol": "1155.KL", "name": "Maybank", "market": "MY", "ccy": "MYR", "etf": false }
  ],
  "holdings": {
    "tw_broker": { "2330": 1000, "0056": 10000, "00919": 5000 },
    "schwab": { "AAPL": 30, "MSFT": 12 },
    "moomoo_my_us": { "NVDA": 25 },
    "moomoo_my_my": { "1155.KL": 1000 }
  },
  "csv_preview": {
    "kind": "交易",
    "filename": "tw_broker_2026H1.csv",
    "rows": [
      { "n": 1, "date": "2026-06-02", "account": "台灣券商", "side": "buy", "symbol": "0056", "shares": 2000, "price": 38.60, "status": "ok", "reason": null },
      { "n": 2, "date": "2026-06-05", "account": "台灣券商", "side": "sell", "symbol": "2330", "shares": 1500, "price": 605.00, "status": "warn", "reason": "賣出股數 1,500 超過持有 1,000 — 可寫入，請確認是否放空" },
      { "n": 3, "date": "2026-06-06", "account": "台灣券商", "side": "buy", "symbol": "23300", "shares": 1000, "price": 51.00, "status": "error", "reason": "未知代號 23300 — 不在標的清單，已排除" }
    ]
  },
  "ai_drafts": {
    "source_label": "schwab_screenshot_0611.png",
    "model": "claude-sonnet（Vision）",
    "rows": [
      { "account_id": "schwab", "date": "2026-06-11", "side": "buy", "symbol": "AAPL", "name": "Apple", "shares": 10, "price": 211.40, "fee": 0, "tax": 0, "note": null },
      { "account_id": "schwab", "date": "2026-06-11", "side": "buy", "symbol": "MSFT", "name": "Microsoft", "shares": 2, "price": 498.20, "fee": 0, "tax": 0, "note": "日期無法辨識，已預設今日 — 請確認" }
    ]
  },
  "dividend_defaults": {
    "tw":   { "symbol": "2330", "gross": 5000, "net": 5000 },
    "drip": { "symbol": "AAPL", "gross": 7.50, "withhold_rate": 0.30, "reinvest_shares": 0.0248, "reinvest_price": 211.40 },
    "net":  { "symbol": "1155.KL", "net": 170.00 }
  }
};
