# 13 — 帳戶與費率（唯讀 v1）（P1）

> 前端現況：`settings-accounts.html` 帳戶卡＋費率明細為靜態 HTML（「v1 唯讀 —
> 帳戶由設定檔定義」）。後端已有 `config_seed.AccountConfig / FeeRuleSet /
> get_fee_rule_set / seed_accounts` 與 `store.list_accounts`，但無路由。

## 13.1 GET /api/accounts

### Endpoint & Method
`GET /api/accounts`

### Description
四帳戶基本資料＋股利模式＋完整費率規則。供「設定 › 帳戶與費率」頁渲染帳戶卡與
費率明細折疊區，並標示規則版本（settings_meta seeded 時間）。

### Request Structure
- Header：`Cookie: pd_session=…`（保護模式時）
- Query／Body：無。

### Response Structure
**200**：
```jsonc
{
  "version": { "category": "accounts", "seeded_at": "2026-01-02T00:00:00+00:00" },
  "accounts": [
    { "account_id": "tw_broker", "name": "台灣券商", "broker": "元大",
      "settlement_ccy": "TWD", "funding_ccy": "TWD",
      "div_model": "tw",                       // "tw"|"drip"|"net"
      "fee_rules": {
        "rate": "0.001425", "discount": "1.0", "min_fee": "20", "round_int": true,
        "tax_sell": "0.003", "tax_sell_etf": "0.001",
        "label": "0.1425%・最低 NT$20・賣出證交稅 0.3%（ETF 0.1%）" } },
    { "account_id": "moomoo_my_us", "name": "Moomoo 美股", "broker": "Moomoo MY",
      "settlement_ccy": "USD", "funding_ccy": "MYR",
      "div_model": "drip",
      "fee_rules": { "rate": "0", "discount": "1.0", "min_fee": "0.99",
                     "round_int": false, "tax_sell": "0",
                     "label": "平台費 USD 0.99/筆" } }
  ]
}
```
**500**：共同錯誤格式。

### Python Backend Implementation Notes
- 組合 `store.list_accounts(conn)`（Account model）＋ `config_seed.get_fee_rule_set(name)`
  （FeeRuleSet pydantic → dict，Decimal 轉字串）；`seeded_at` 讀 `settings_meta`。
- `div_model` 目前在 `AccountConfig` — 確認 seed 表有此欄位；若僅存在 Python 常數，
  以 account_id 映射輸出即可（v1 唯讀，無需落表）。
- **本 spec 無寫入 endpoints**（費率修改 = 改 `config_seed` 後
  `config_store.restore_defaults` — 屬部署操作，非 UI 功能）。前端「重設為預設」按鈕
  若日後需要，另開 `POST /api/accounts/restore-defaults`（目前不做）。
- 此 payload 與 spec 12.1 `fee_rules` 來源相同 — 抽共用序列化函式，禁止兩處各寫一份。
