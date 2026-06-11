# Claude Design Brief — portfolio-dash Full App (shell + pages 2–9)

> **How to use:** paste BOTH documents into the same Claude Design session, in order:
> 1. `claude-design-brief-dashboard.md` (Page 1 儀表板 — detailed spec + mock)
> 2. this document (app shell + every other page)
>
> Iterate page by page in this order: P1 儀表板 → P5/P6/P7 設定 → P2 輸入中心 →
> P3 標的管理 → P4 帳本 → P9 登入. P8 (AI 洞察) is a placeholder page only.
> All §1–§2 constraints of the dashboard brief (vanilla HTML/CSS/JS + ECharts CDN,
> 紅漲綠跌, zh-TW labels, dark theme, thousands separators, null → "—" + badge,
> never invent numbers, one `:root` token set) apply to EVERY page here.

---

## 1. App shell & navigation (design once, applies everywhere)

- **Left sidebar** (collapsible to icons): 儀表板 · 輸入中心 · 標的管理 · 帳本 ·
  設定 (expands: LLM 與額度 / 排程 / 帳戶與費率 / 一般) · AI 洞察 (with a muted
  「即將推出」badge). Active-page highlight.
- **Top bar**: current page title · 資料時間 + freshness chip (same as dashboard) ·
  報告幣別 badge (TWD) · placeholder avatar/menu slot (auth comes later).
- **Toast pattern**: bottom-right transient toasts for 寫入成功 (green) / 寫入失敗
  (red, persists until dismissed, shows the backend message verbatim).
- **Confirm dialog pattern**: destructive or irreversible actions (刪除模型, 重設額度,
  restore defaults) always get a modal with the consequence spelled out.
- Every page is a separate static HTML file sharing `styles.css` + tokens
  (`index.html`, `input.html`, `instruments.html`, `ledger.html`, `settings-llm.html`,
  `settings-scheduler.html`, `settings-accounts.html`, `insights.html`, `login.html`).
  Plain `<a>` links between them — no SPA router.

## 2. Shared interaction patterns (design once, reuse on every page)

1. **Draft → Confirm card** (the app's signature interaction; backend is two-phase
   everywhere): left = input form, right (or below) = live preview card showing the
   parsed/derived result + an **issues list**. Issue severities:
   - **hard error** (red): blocks the 確認 button, field highlighted.
   - **soft warning / needs-confirm** (amber): shows a checkbox 「我了解,仍要寫入」
     that must be ticked before 確認 enables. Example copy: 「賣出股數 1,500 超過
     持有 1,000 — 輸入錯誤還是放空?」
2. **Dense data table** — identical to the dashboard holdings table pattern
   (sticky header, click-to-sort, filter chips, zebra, ~32px rows, tabular numerals).
3. **Empty / degraded state** — "—" + context badge + one-line reason, same family
   as the dashboard (缺價 / 過期 / 匯率資料不足 / AI 未啟用 / 額度用盡 / 服務不可用).
4. **Masked secret field** — shows `sk-•••••••3f2`, click 重設 to replace; never
   reveals the stored value.
5. **Toggle + status dot** — enabled switches; status dots: 綠=ok, 紅=error, 灰=停用.

---

## 3. Page 2 — 輸入中心 (`input.html`)

Purpose: every way data enters the ledgers. **Five tabs.** All writes go through
the Draft → Confirm pattern (§2.1).

### Tab 1 手動交易
- Form: 帳戶 select (台灣券商/嘉信 Schwab/Moomoo 美股/Moomoo 馬股) → 代號
  autocomplete (suggests from registered instruments; unknown symbol shows
  「未註冊 — 前往標的管理」link) → 買/賣 segmented control → 股數 → 價格
  (MY instruments allow 3 dp) → 交易日期 date picker.
- **Auto fee/tax panel** (updates live): 手續費 + 證交稅 computed per the account's
  fee rules, each with an 覆寫 pencil icon; overridden values get an「已覆寫」chip.
- Preview card: 總成本 (含費稅) / 淨收款, fee breakdown, issues list.
- Mock: account=台灣券商, 2330 買 1,000 股 @ 612.5 → 手續費 873 (0.1425%, min 20,
  rounded to integer NT$), 稅 0 (buy side), 總成本 613,373. Soft-warning mock:
  賣出 1,500 股 but 持有 1,000.

### Tab 2 CSV 匯入 (sub-select: 交易 / 股利 / 換匯 / 期初)
- Upload dropzone + paste-text area (either).
- **Preview table**: one row per CSV line with a status column — 綠 OK / 黃 警告
  (importable, reason shown) / 紅 錯誤 (excluded, reason shown). Row checkboxes
  (errors locked off). Header shows counts: 可寫入 12 · 警告 2 · 錯誤 1.
- 確認寫入 → result summary banner: 成功 14 筆 · 跳過 1 筆 (展開原因列表).
- Mock rows: 3 transactions — one OK, one warning 「賣出超過持股」, one error
  「未知代號 23300」.

### Tab 3 AI 輸入 (text or screenshot → drafts)
- Input: large textarea 「貼上對帳單文字…」 + image dropzone 「或上傳券商 App 截圖」
  (vision). 解析 button.
- Result: **editable draft table** (帳戶/代號/買賣/股數/價格/費用/稅/日期 all
  editable inline) + per-row confidence note from the model + issues. Row checkboxes,
  全選寫入 / 逐列寫入.
- **Three first-class degradation states** (design all three as distinct full-panel
  states, not toasts):
  - AI 未啟用: 「尚未設定任何 LLM 模型 — 前往『設定 › LLM 與額度』」 (button link)
  - 額度用盡: 「AI Agents 額度用盡 — 前往額度設定重置」 (button link)
  - 服務不可用: 「LLM 服務暫時無法連線,請稍後重試」 (retry button)
- Mock drafts: 2 rows parsed from a Schwab screenshot (AAPL 買 10 @ 211.40;
  MSFT 買 2 @ 498.20), one with an amber issue 「日期無法辨識,已預設今日」.

### Tab 4 股利
- 帳戶 select first — **the form morphs by the account's dividend model**:
  - 台灣券商 (現金/配股): 代號, 日期, segmented 現金股利/配股, gross/net 欄,
    配股 shows 配股股數 instead of cash.
  - Schwab / Moomoo 美股 (DRIP): gross → auto 30% 預扣 → net (read-only calc) →
    再投資股數 + 再投資價格 (note: 「$0 成本再買回」).
  - Moomoo 馬股: single 淨額 field (single-tier).
- Preview card restates the model applied (e.g. 「DRIP:預扣 30%,net 將以 $0 成本
  股數入帳」).
- Mock: 2330 現金股利 gross 5,000 net 5,000; AAPL DRIP gross 7.50 → withholding
  2.25 → net 5.25, reinvest 0.0248 股 @ 211.40.

### Tab 5 換匯 + 期初 (two stacked forms)
- 換匯: 帳戶, 日期, from 幣別+金額 → to 幣別+金額, **隱含匯率 live readout**
  (32,000 TWD → 1,000 USD ⇒ 1 USD = 32.0000 TWD).
- 期初庫存: 帳戶/代號/股數/原始均價/原始總成本/建檔日 + helper text
  「期初不是交易流量,但其建檔日與成本會計入 XIRR」.

## 4. Page 3 — 標的管理 (`instruments.html`)

Purpose: register/maintain instruments (watchlist); TW board resolution UX.

- **新增標的 row** (top): 代號 input + 市場 select (台股/美股/馬股) + 查詢 button.
- **Probe result card** (TW only): 「2330 → 台積電,判定 **TWSE 上市** — 正確嗎?」
  with 確認 / 改為 TPEx / 改為其他 options; US/MY skip the probe (board is
  deterministic) and go straight to the detail form.
- Detail form: 名稱, 產業 (free text with suggestions), 幣別 (auto from market,
  read-only). 確認註冊 writes.
- **Unresolved state**: probe failed → amber banner 「板別未解析 — 已以預設 TWSE
  抓報價,請稍後手動確認」; instrument saved with a persistent amber badge.
- **標的清單 table**: 代號 / 名稱 / 市場 / 板別 badge (TWSE 藍 · TPEx 紫 · .KL 綠 ·
  未解析 琥珀) / 產業 / 幣別 / actions (編輯 inline, 重新探測板別 for TW).
- Mock list: 2330 TWSE · 0056 TWSE · 6488 TPEx · 8069 未解析(amber) · AAPL — ·
  1155.KL .KL.

## 5. Page 4 — 帳本檢視 (`ledger.html`)

Purpose: read-only browsing of the four source-of-truth ledgers.

- **Four tabs**: 交易 / 股利 / 換匯 / 期初庫存. Shared filter bar: 帳戶 chips ·
  代號 search · 日期區間 picker.
- 交易 table: 日期 / 帳戶 / 代號 / 買賣 (紅買綠賣 chip per §2.4 of dashboard brief
  color rules — use 紅=買? NO: 買賣 is direction not P&L; use neutral chips 買=filled,
  賣=outline) / 股數 / 價格 / 手續費 / 稅 / 總額. Row expander reveals
  fee-rule snapshot (JSON key-values, monospace) + note.
- 股利 table: 日期 / 帳戶 / 代號 / 類型 chip (現金/配股/DRIP) / gross / 預扣 / net /
  再投資股數+價.
- 換匯 table: 日期 / 帳戶 / from→to amounts / 隱含匯率.
- 期初 table: 帳戶 / 代號 / 股數 / 原始均價 / 原始總成本 / 建檔日.
- **更正流程**: each row has a 「以新列更正」 action → opens the input form
  (Page 2) pre-filled; a footnote on the page: 「帳本為 append-only:更正以新紀錄
  沖銷,原紀錄永久保留」.
- Mock: 6 transactions, 4 dividends, 2 fx conversions, 2 openings (reuse dashboard
  mock symbols/accounts for consistency).

## 6. Page 5 — 設定 · LLM 與額度 (`settings-llm.html`)

Top: **AI 狀態 chip** — 「AI:啟用中」(green, when any role default set) /
「AI:已關閉」(gray, all four roles empty).

### Section A 模型註冊表
- Table: 別名 / 供應商 / 模型名 / Vision ✓ / 輸入價 ($/Mtok) / 輸出價 / context /
  timeout / 啟用 toggle / actions (編輯, 刪除 with confirm modal).
- 編輯 drawer (right slide-in): all `ModelConfig` fields — alias, provider select
  (openrouter / openai-compatible / anthropic), model_name, api_base, **api_key
  (masked, §2.4)**, vision toggle, input/output price per Mtok, context_window,
  max_output_tokens, timeout_seconds, max_retries, enabled, notes.
- Mock models: `claude-sonnet` (anthropic, vision ✓) · `gpt-4o-mini`
  (openai-compatible) · `qwen-vl` (openrouter, vision ✓, 停用).

### Section B 角色預設 (four selects in one card)
- Default 模型 / Default 後備 / Vision 模型 / Vision 後備 — each a select listing
  enabled models **plus a「(空 = 關閉)」option**. Helper text: 「四者皆空時,所有
  AI 功能停用,輸入頁將顯示『AI 未啟用』」.
- Mock: Default=claude-sonnet, Default 後備=gpt-4o-mini, Vision=claude-sonnet,
  Vision 後備=(空).

### Section C 額度治理
- **剩餘額度 card**: big number `$3.84` (or 「無上限」 state when never set) +
  「重置額度」button → modal with $ input → writes a reset event.
- **重置歷史** mini-table: 時間 / 重置金額.
- Mock: remaining $3.84; resets: 2026-06-01 $10.00 · 2026-04-15 $10.00.

### Section D 用量與趨勢
- Per-model cumulative table: 模型 / 呼叫數 / 輸入 tokens / 輸出 tokens / 累計成本.
- ECharts line: daily cost, one series per model, 30-day window.
- Per-agent cost chips (e.g. ai_agents_input $1.92 · insight $0.00).
- Mock: claude-sonnet 42 calls $4.83 · gpt-4o-mini 18 calls $1.33.

### Section E 還原預設
- 「還原 LLM 預設設定」 danger-outline button + confirm modal (「清空所有模型與
  角色設定,回到 AI 關閉狀態」).

## 7. Page 6 — 設定 · 排程 (`settings-scheduler.html`)

### Section A 排程工作表 (one row per job)
Columns: 工作 (id + 中文描述) / 啟用 toggle / **cron 欄 (editable text) + 人話
translation underneath** (「週一至五 14:00」) / 時區 / 上次執行 (status dot +
relative time, hover = detail) / 下次執行 / **立即執行** button.
- Jobs (mock): `quotes_tw` 台股收盤報價+匯率 ✓ `0 14 * * mon-fri` Asia/Taipei ·
  `quotes_us` 美股 ✓ `30 16 * * mon-fri` America/New_York · `quotes_my` 馬股 ✓
  `30 17 * * mon-fri` Asia/Kuala_Lumpur · `history_daily` 歷史回補 ✓ `0 2 * * *` ·
  `dividends_daily` 除息資料 ✓ `0 3 * * *`.
- 立即執行 → row enters spinner state → inline result chip: 「成功 7 檔 · 失敗 1
  (00919: 來源無資料)」.
- One mock row in error state: quotes_my 上次執行 紅點 「HTTP 502 from provider」.

### Section B 執行歷史
Dense table: 時間 / 工作 / 狀態 (ok 綠 / error 紅) / 摘要 detail / 耗時. Filter by
工作. Mock: 8 rows mixed.

## 8. Page 7 — 設定 · 帳戶與費率 + 一般 (`settings-accounts.html`)

### Section A 帳戶卡 ×4 (read-only v1)
Card per account: 名稱 / 券商 / 結算幣別 / 資金幣別 / 股利模式 badge
(成本沖減 / DRIP 30% / 淨額) / 費率規則 ref. Mock = the four real accounts
(台灣券商 TWD/TWD · 嘉信 USD/TWD · Moomoo 美股 USD/MYR · Moomoo 馬股 MYR/MYR).

### Section B 費率明細 (read-only v1, expandable per account)
- 台灣券商: 券商費率 0.1425% · 折扣 1.0 · 最低 NT$20 · 證交稅 現股 0.3% / ETF 0.1%
  / 當沖 0.15% · 「費稅四捨五入至整數 NT$」note.
- Moomoo 馬股: clearing 0.03% (cap RM1,000) · stamp duty · SST — value slots shown
  as 「依設定檔」 where rates are placeholders.
- Banner: 「費率為版本化設定 — 修改需經設定檔變更紀錄 (v1 唯讀)」.

### Section C 一般
讀-only rows: 報告幣別 TWD · 顯示時區 Asia/Taipei · 環境 dev. 資料來源 status:
provider 順序 chips (twse → tpex → yfinance) · FinMind token: 已設定 ✓ (boolean
only, never the value).

## 9. Page 8 — AI 洞察 (`insights.html`) — placeholder page

Design the page frame only: title, 「產生洞察」 button (disabled with tooltip
「AI 洞察模組即將推出」), and the **empty state** (same illustration family as the
dashboard insight section): 「尚無 AI 洞察 — 洞察卡片由排程批次產生」. Card grid
reuses the dashboard's insight-card component (title/body/timestamp/AI badge) —
show 2 sample cards in a dimmed 「預覽」 state using the dashboard mock's
insights[].

## 10. Page 9 — 登入 (`login.html`)

Minimal: centered card, app logo/name, 使用者 + 密碼 fields, 登入 button, error
state 「帳號或密碼錯誤」. Dark theme, same tokens. No registration, no forgot-password.

---

## 11. Export acceptance checklist (run BEFORE downloading the export)

Per page, verify in the Design preview:
- [ ] Every `null`-able field has a designed "—" + badge state (not blank/0).
- [ ] P2: all three AI degradation panels exist (未啟用 / 額度用盡 / 不可用).
- [ ] P2: soft-warning checkbox flow blocks 確認 until ticked.
- [ ] P3: TW probe confirm card + 未解析 amber badge both present.
- [ ] P5: api_key is masked; AI 狀態 chip has both states; 「(空 = 關閉)」 option
      visible in role selects; budget 「無上限」 state designed.
- [ ] P6: cron 人話 translation + error-state row + inline trigger result exist.
- [ ] All tables: thousands separators, tabular numerals, sticky headers.
- [ ] 紅漲綠跌 applied to every P&L number (and NOT to non-P&L chips like 買/賣).
- [ ] All pages share one `:root` token block; no page-local color overrides.
- [ ] Files: one HTML per page (§1 list), shared `styles.css` + `app.js` helpers.
