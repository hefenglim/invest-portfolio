# 17 — 全端自動測試與閉環修復(Testing & Regression Harness)(P0 — 與 spec 08 同步建立)

> 目的:讓 Claude Code 在完成各 spec 後,**自主執行「跑測試 → 讀失敗 → 修復 → 重跑」閉環**,
> 並在前後端組合層級做成品級回歸測試。本 spec 定義工具鏈、決定性環境、測試矩陣與閉環守則。
> **沒有本 spec,閉環不成立**:行情來源、LLM、系統時鐘都是非決定性外部依賴,測試會隨機紅綠。

## 17.0 能力邊界(誠實聲明)

Claude Code **可以**自主完成:
- pytest 單元/合約測試的全閉環(跑、讀 traceback、修、重跑)
- FastAPI `TestClient` 的 API 合約測試(無需起 server)
- Playwright headless 的前後端組合 E2E(起 uvicorn + 真 SQLite + 驅動瀏覽器斷言 DOM)
- 回歸快照比對(golden JSON)與失敗 diff 分析

Claude Code **不能/不應**:
- 打真實外部服務(TWSE/yfinance/FinMind/LiteLLM)— 測試一律走 stub(17.3)
- 像素級視覺驗證 — E2E 斷言以 DOM 結構/文字/數值為準
- 以「改測試讓它變綠」收斂 — 守則見 17.7

## 17.1 工具鏈與目錄

```
tests/
  conftest.py            -- 黃金 fixture、凍結時鐘、fake providers/completer、TestClient
  unit/                  -- 計算核心(pure functions,既有 + 新增)
  contract/              -- 每個 API spec 一檔:test_spec08_dashboard.py ...
  e2e/                   -- Playwright:test_flow_*.py
  golden/                -- 黃金快照 JSON(dashboard payload 等)
Makefile                 -- make test / make e2e / make regress / make all
```
依賴:`pytest`、`pytest-socket`(禁網路)、`httpx`、`playwright`、`freezegun`。
**`pytest-socket` 全域啟用 `--disable-socket --allow-unix-socket`** — 任何測試打到真網路即紅。

## 17.2 黃金資料集(Golden Fixture)— 回歸測試的地基

**核心設計:`mock-data.js` 的數字就是期望值。** 建 `tests/conftest.py::golden_db`:
以 spec 12 的同一套寫入路徑(`enter_transaction`/`commit_preview`/`upsert_opening`…)
seed 一個 in-memory SQLite,重現 mock 的完整情境:
- 4 帳戶、8 標的(含 00919 缺價、8069 板別未解析)、期初 2 筆、交易 6 筆、股利 4 筆、換匯 2 筆
- 價格庫:各標的最近 30 個交易日 close(固定數列,含 MSFT stale 情境 price_as_of=06-06)
- 匯率:USD/TWD=32.90、USD/MYR=4.42、MYR/TWD 固定值

驗收等式(回歸錨點):`GET /api/dashboard` 對 golden_db 的輸出,
KPI/holdings/realized/fx 各欄位 == `mock-data.js` 對應值(Decimal 字串精確比對)。
**做不平 = seed 劇本或計算層有 bug,兩者必居其一 — 這正是回歸測試要抓的。**

## 17.3 決定性三支柱(所有測試強制)

| 依賴 | 處理 | 實作 |
|---|---|---|
| 時鐘 | 凍結 `now = 2026-06-11T14:30:00+08:00` | `freezegun`;API 層 `now` 一律可注入(spec 08 router 簽名已留 `now` 參數) |
| 行情來源 | `FakeProvider(ProviderBase)` 回固定 `PriceRow/FxRow/DividendEvent`;可程式化注入失敗(HTTP 502 情境) | `Registry` 建構注入 — 既有架構已支援 |
| LLM | `FakeCompleter` 回固定 `AiDraftList`/文字;可注入 `LLMBudgetExceeded` 等三例外 | `agents.ai_agents_input(completer=…)` 既有注入點;`shared/llm` 補同款注入 |

## 17.4 合約測試矩陣(每 endpoint 最低要求)

每支 endpoint 至少 4 類 case;表列各 spec 重點補充:

| 類 | 內容 |
|---|---|
| C1 happy | 200/201/202,shape 與 spec JSON 範例逐欄比對(Decimal 字串、enum 小寫、時區 +08:00) |
| C2 驗證 | 400/422 各一,error 格式 = spec 08 §8.0 |
| C3 邊界 | 空資料庫、缺價標的、stale、null 欄位(00919/8069 情境) |
| C4 不變量 | 試算類呼叫後 DB 雜湊不變(絕不寫入);commit 類重送冪等性或拒絕 |

spec 專屬必測:
- **08**:dashboard 黃金等式(17.2);跨幣別欄位絕無加總(TWD+USD 不出現在同一 sum)
- **09**:訪客→保護模式切換;401 守門;移除自己→session 失效
- **11/12**:append-only(無任何 UPDATE/DELETE 交易列的路徑);oversell 軟警告 ack 流程;
  CSV error 列送回 commit 必拒
- **03**:whatif 與 manual/preview 對同輸入回同費稅(同一引擎證明);8 條 alert 規則各一觸發 case
- **15**:invalid cron 400 且 DB 未變;reschedule 後 next 更新
- **02**:匯出 CSV raw Decimal(無千分位)、BOM、CRLF;tax-package 各幣別分列
- **04/07**:R1–R8 守門各一 case;**preflight 與實跑共用函式**(monkeypatch 證明同一 code path);
  skipped 必帶 reason 枚舉

## 17.5 E2E 組合測試(Playwright,真 server + 真前端)

起 `uvicorn`(golden_db 檔案版)+ 靜態前端,headless 驅動。**前端接線採漸進式**:
每完成一支 endpoint,前端 mock 改為 fetch(spec 各檔「前端接線」節),E2E 隨之解鎖。

| # | 流程 | 斷言 |
|---|---|---|
| E1 | 開儀表板 | KPI 卡文字 == 黃金值;00919 顯示「缺價」badge;asof chip 正確 |
| E2 | 手動輸入:2330 買 1000@612.5 → 確認寫入 | toast 成功;帳本頁出現該列;dashboard KPI 變化量 == 預期 Decimal |
| E3 | CSV 匯入 3 列(ok/warn/error) | 預覽表 3 列狀態正確;commit 後 written=2 |
| E4 | 賣超軟警告 | 賣 1500 > 持 1000 → 警示文案;ack 後可寫 |
| E5 | 個股抽屜 | 任意頁點代號 → 抽屜開、均價線、配息史筆數正確 |
| E6 | 登入閉環 | 新增用戶 → 登出 → 錯密碼 401 留在 login → 對密碼進儀表板 → 鎖定/解鎖 |
| E7 | 排程 | 改 cron → next 更新;立即執行(FakeProvider) → 歷史表出現 run |
| E8 | 預警鈴鐺 | 改門檻(權重>20%)→ 鈴鐺數變化、跨頁 storage 同步 |
| E9 | AI 輸入 | FakeCompleter 草稿 → 預覽表 → commit → 帳本含 note「AI 輸入」 |
| E10 | 匯出 | 點產生下載 → 檔名/Content-Disposition/首列欄位正確 |

## 17.6 回歸機制(Regression)

1. **黃金快照**:`tests/golden/*.json` 存正規化 API 回應(排序鍵、固定 as_of)。
   任何 PR 級改動後 `make regress` 比對;**有意契約變更必須同 commit 更新快照＋spec**,
   快照 diff 即變更審查面。
2. **全綠門檻**:任一 spec 完成的定義 = `make all`(unit+contract+e2e+regress)全綠,
   不是單支測試綠。
3. **回歸觸發點**:每完成一個 spec → 跑全套(早期套件小,成本低);
   修任何 bug → 先寫紅測試重現,再修到綠(測試即回歸資產)。

## 17.7 Claude Code 閉環守則(硬規則)

1. 失敗時**先讀 traceback 定位層級**:seed 劇本 / 計算核心 / API 序列化 / 前端接線 — 修對層,
   禁止在 API 層 patch 計算層的錯。
2. **禁止為通過而改測試或快照**,除非同時引用 spec 條文變更(commit message 註明 spec 章節)。
3. 同一測試連續 3 次修復失敗 → 停止迭代,輸出失敗分析報告(現象/假設/已試方案)等人工決策。
4. E2E 不穩定(flaky)不准 retry 掩蓋:查 race(等待條件用 expect polling,禁 sleep)。
5. 每輪閉環結束輸出:綠/紅統計、修了什麼、動到哪些檔 — 可稽核。

## 17.8 對其他 spec 的回寫要求

- spec 08:所有 router 的 `now`/`Registry`/`completer` 必須可注入(測試建構 app 時傳入)。
- spec 12/16:LLM 呼叫點全部經由可注入 completer,不得內聯 import 呼叫。
- 前端各頁接線時保留 `data-*` 測試錨點(KPI 值、列 id),E2E 斷言不依賴文案排版。
