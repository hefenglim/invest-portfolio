/* portfolio-dash mock dataset — field names are the real backend contract (DashboardData) */
window.DASHBOARD_DATA = {
  "as_of": "2026-06-11T14:30:00+08:00",
  "reporting_currency": "TWD",
  "kpis": {
    "reporting_currency": "TWD",
    "total_market_value": 1618682.54,
    "total_return": 308529.66,
    "total_return_rate": 0.2147,
    "realized_total": 34931.12,
    "unrealized_total": 273598.54,
    "xirr": 0.1832,
    "fx_realized": 1250.00,
    "fx_unrealized": 14154.12
  },
  "holdings": [
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "2330",
     "name": "台積電", "market": "TW", "sector": "Semiconductors", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 1000, "original_avg": 500.00, "adjusted_avg": 495.00,
     "original_cost_total": 500000, "adjusted_cost_total": 495000,
     "dividend_portion": 5000, "payback_ratio": 0.0100,
     "market_price": 612.5, "market_value": 612500, "unrealized_pnl": 117500,
     "capital_gain": 112500, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.3784},
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "0056",
     "name": "元大高股息", "market": "TW", "sector": "ETF", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 10000, "original_avg": 36.20, "adjusted_avg": 34.85,
     "original_cost_total": 362000, "adjusted_cost_total": 348500,
     "dividend_portion": 13500, "payback_ratio": 0.0373,
     "market_price": 38.95, "market_value": 389500, "unrealized_pnl": 41000,
     "capital_gain": 27500, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.2406},
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "00919",
     "name": "群益台灣精選高息", "market": "TW", "sector": "ETF", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 5000, "original_avg": 23.50, "adjusted_avg": 22.90,
     "original_cost_total": 117500, "adjusted_cost_total": 114500,
     "dividend_portion": 3000, "payback_ratio": 0.0255,
     "market_price": null, "market_value": null, "unrealized_pnl": null,
     "capital_gain": null, "price_stale": true, "price_as_of": null, "weight": null},
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": "AAPL",
     "name": "Apple", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 30, "original_avg": 182.50, "adjusted_avg": 182.50,
     "original_cost_total": 5475.00, "adjusted_cost_total": 5475.00,
     "dividend_portion": 28.80, "payback_ratio": 0.0053,
     "market_price": 211.40, "market_value": 6342.00, "unrealized_pnl": 867.00,
     "capital_gain": 867.00, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.1289},
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": "MSFT",
     "name": "Microsoft", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 12, "original_avg": 405.00, "adjusted_avg": 405.00,
     "original_cost_total": 4860.00, "adjusted_cost_total": 4860.00,
     "dividend_portion": 21.60, "payback_ratio": 0.0044,
     "market_price": 498.20, "market_value": 5978.40, "unrealized_pnl": 1118.40,
     "capital_gain": 1118.40, "price_stale": true, "price_as_of": "2026-06-06",
     "weight": 0.1215},
    {"account_id": "moomoo_my_us", "account_name": "Moomoo MY (US)", "symbol": "NVDA",
     "name": "NVIDIA", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 25, "original_avg": 118.00, "adjusted_avg": 118.00,
     "original_cost_total": 2950.00, "adjusted_cost_total": 2950.00,
     "dividend_portion": 2.50, "payback_ratio": 0.0008,
     "market_price": 172.35, "market_value": 4308.75, "unrealized_pnl": 1358.75,
     "capital_gain": 1358.75, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.0876},
    {"account_id": "moomoo_my_my", "account_name": "Moomoo MY (MY)", "symbol": "1155.KL",
     "name": "Maybank", "market": "MY", "sector": "Financials", "board": ".KL",
     "quote_ccy": "MYR", "shares": 1000, "original_avg": 9.150, "adjusted_avg": 8.980,
     "original_cost_total": 9150.00, "adjusted_cost_total": 8980.00,
     "dividend_portion": 170.00, "payback_ratio": 0.0186,
     "market_price": 9.870, "market_value": 9870.00, "unrealized_pnl": 890.00,
     "capital_gain": 720.00, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.0430}
  ],
  "realized": {
    "rows": [
      {"account_id": "tw_broker", "symbol": "2330", "quote_ccy": "TWD",
       "shares_sold": 200, "proceeds_net": 119350, "original_cost_removed": 100000,
       "adjusted_cost_removed": 98000, "realized": 21350},
      {"account_id": "schwab", "symbol": "AAPL", "quote_ccy": "USD",
       "shares_sold": 5, "proceeds_net": 1002.30, "original_cost_removed": 589.50,
       "adjusted_cost_removed": 589.50, "realized": 412.80}
    ],
    "by_currency": {"TWD": 21350, "USD": 412.80}
  },
  "returns": {
    "by_currency": {
      "TWD": {"realized": 21350, "unrealized": 158500, "total_return": 179850,
               "gross_invested": 979500, "rate": 0.1836},
      "USD": {"realized": 412.80, "unrealized": 3344.15, "total_return": 3756.95,
               "gross_invested": 13285.00, "rate": 0.2828},
      "MYR": {"realized": 0, "unrealized": 890.00, "total_return": 890.00,
               "gross_invested": 9150.00, "rate": 0.0973}
    },
    "reporting_currency": "TWD", "reporting_total_return": 308529.66, "xirr": null
  },
  "allocation": {
    "by_sector": {"Semiconductors": 612500, "ETF": 389500, "Tech": 547099.04,
                   "Financials": 69583.50},
    "weights": {"Semiconductors": 0.3784, "ETF": 0.2406, "Tech": 0.3380,
                 "Financials": 0.0430},
    "reporting_currency": "TWD"
  },
  "currency_view": {
    "by_currency_value": {"TWD": 1002000, "USD": 16629.15, "MYR": 9870.00},
    "reporting_total_value": 1618682.54, "reporting_currency": "TWD"
  },
  "fx": {
    "by_account": {
      "schwab": {"account_id": "schwab", "home_ccy": "TWD", "foreign_ccy": "USD",
        "avg_rate": 31.80, "current_spot": 32.90, "foreign_cash": 1420.55,
        "foreign_stock_value": 12320.40, "realized_fx": 1250.00,
        "unrealized_fx_stocks": 13552.44, "unrealized_fx_cash": 1562.61},
      "moomoo_my_us": {"account_id": "moomoo_my_us", "home_ccy": "MYR",
        "foreign_ccy": "USD", "avg_rate": 4.450, "current_spot": 4.420,
        "foreign_cash": 230.10, "foreign_stock_value": 4308.75, "realized_fx": 0,
        "unrealized_fx_stocks": -129.26, "unrealized_fx_cash": -6.90}
    },
    "reporting_currency": "TWD",
    "reporting_realized_fx": 1250.00, "reporting_unrealized_fx": 14154.12
  },
  "dividends": {
    "by_year": [
      {"year": 2024, "by_currency": {"TWD": 8200}},
      {"year": 2025, "by_currency": {"TWD": 14650, "USD": 86.40, "MYR": 412.00}},
      {"year": 2026, "by_currency": {"TWD": 18500, "USD": 52.10, "MYR": 280.00}}
    ],
    "total_by_currency": {"TWD": 41350, "USD": 138.50, "MYR": 692.00}
  },
  "ex_dividend_calendar": [
    {"symbol": "2330", "name": "台積電", "ex_date": "2026-06-20",
     "pay_date": "2026-07-16", "cash_amount": 5.00, "stock_amount": null,
     "currency": "TWD", "source": "twse"},
    {"symbol": "1155.KL", "name": "Maybank", "ex_date": "2026-06-25",
     "pay_date": "2026-07-10", "cash_amount": 0.32, "stock_amount": null,
     "currency": "MYR", "source": "yfinance"},
    {"symbol": "0056", "name": "元大高股息", "ex_date": "2026-07-15",
     "pay_date": "2026-08-06", "cash_amount": 0.85, "stock_amount": null,
     "currency": "TWD", "source": "twse"}
  ],
  "trend": {
    "available": true, "reporting_currency": "TWD",
    "points": [
      {"date": "2026-01-05", "total_value": 980000, "net_invested": 1012000, "incomplete": false},
      {"date": "2026-01-20", "total_value": 1015000, "net_invested": 1095000, "incomplete": false},
      {"date": "2026-02-03", "total_value": 1124000, "net_invested": 1180000, "incomplete": false},
      {"date": "2026-02-17", "total_value": 1098000, "net_invested": 1180000, "incomplete": true},
      {"date": "2026-03-02", "total_value": 1186000, "net_invested": 1228000, "incomplete": false},
      {"date": "2026-03-16", "total_value": 1242000, "net_invested": 1262000, "incomplete": false},
      {"date": "2026-03-30", "total_value": 1198000, "net_invested": 1262000, "incomplete": false},
      {"date": "2026-04-13", "total_value": 1286000, "net_invested": 1291000, "incomplete": false},
      {"date": "2026-04-27", "total_value": 1342000, "net_invested": 1291000, "incomplete": false},
      {"date": "2026-05-11", "total_value": 1420000, "net_invested": 1310153, "incomplete": false},
      {"date": "2026-05-25", "total_value": 1518000, "net_invested": 1310153, "incomplete": false},
      {"date": "2026-06-11", "total_value": 1618682.54, "net_invested": 1310153, "incomplete": false}
    ]
  },
  "freshness": {
    "prices": [
      {"symbol": "2330", "as_of": "2026-06-11", "stale": false},
      {"symbol": "0056", "as_of": "2026-06-11", "stale": false},
      {"symbol": "00919", "as_of": null, "stale": true},
      {"symbol": "AAPL", "as_of": "2026-06-11", "stale": false},
      {"symbol": "MSFT", "as_of": "2026-06-06", "stale": true},
      {"symbol": "NVDA", "as_of": "2026-06-11", "stale": false},
      {"symbol": "1155.KL", "as_of": "2026-06-11", "stale": false}
    ],
    "fx": [
      {"base": "USD", "quote": "TWD", "as_of": "2026-06-11", "stale": false},
      {"base": "MYR", "quote": "TWD", "as_of": "2026-06-11", "stale": false},
      {"base": "USD", "quote": "MYR", "as_of": "2026-06-11", "stale": false}
    ],
    "any_stale": true,
    "missing_prices": ["00919"],
    "missing_fx": [],
    "xirr_unavailable_reason": null,
    "trend_unavailable_reason": null
  },
  "insights": [
    {"id": "ic-001", "title": "半導體部位集中度偏高",
     "body": "台積電與科技股合計約占組合 71%。若半導體景氣反轉，組合波動將顯著放大；可留意 ETF 與金融股的再平衡空間。",
     "generated_at": "2026-06-11T08:00:00+08:00",
     "token_cost_usd": 0.043},
    {"id": "ic-002", "title": "USD 匯率順風貢獻明顯",
     "body": "Schwab 美元部位平均取得匯率 31.80，現時 32.90，未實現匯兌貢獻約 +15,115 TWD。若 USD 回落，此部分收益將回吐。",
     "generated_at": "2026-06-11T08:00:00+08:00",
     "token_cost_usd": 0.038}
  ]
};
