# 批次④ 新聞內容管線 — 完工報告（供晨間審查）

> 完工日:2026-07-06 · 分支 `feat/task-pack-and-composer-cleanup` @ `245e555`(部署測試站)
> 範圍:HTML 抓取器 → AI 整理 → 獨立新聞資料庫 → 個股新聞變數 → 洞察卡
> 依據:使用者 2026-07-06 指令(完整新聞內容管線 + 派 Opus 4.8 subagent 系統級深審)

---

## 一、結論(TLDR)

**整條新聞內容管線已建成、閘門全綠、真站端到端跑通。** 昨夜在測試站手動觸發一次真實
`news_daily`:抓取真實網路新聞、用 Default 模型整理 **43 則**入獨立新聞庫,2 則抓不到降級
只留標題、1 則去重跳過,成本 **US$0.154**(約 $0.0036/則)。個股新聞已能被變數與健檢卡引用
(台股中文、美股英文)。**Opus 4.8 subagent 的系統級深審正在背景執行,裁決結果附於文末(見
第六節,完成後補上)。**

## 二、建了什麼(新模組 `portfolio_dash/news/`)

| 元件 | 職責 | 關鍵設計 |
| --- | --- | --- |
| `store.py` | **獨立 SQLite 新聞庫**(news.db) | 與主帳本庫分離(利多帳戶共享);`organized_news` + `news_mentions` 精準代號索引;link 去重;日期區間查詢。只存 AI 摘要+來源連結,不存全文 |
| `fetcher.py` | 通用 HTML 抓取器 | 零外部依賴(regex 去標籤);位元組上限;失敗一律降級 None;`fetch_html` 原始變體給清單頁 |
| `sources.py` | 逐股連結探索 | FinMind(中文)+ yfinance(英文,含 .TW)+ Yahoo 台股清單;客戶端可注入;單源失敗不拖垮其餘 |
| `organizer.py` | LLM 整理器 | Default 模型 → {標題/日期/摘要/相關股票};缺欄回退探索值;不變式 #1(新聞不出數字) |
| `pipeline.py` | 純編排 | discover→fetch→organize→store;每股上限;跨股去重;抓不到降級;預算耗盡停止(partial) |
| `organizer_prompt.py` | 可編輯新聞提示詞 | config_store 單列;官方預設 + 重置;GET/PUT/POST `/api/news-prompt(/reset)` |

**接線**:`api/news_service.py`(真實客戶端 + `run_news_daily`)、排程 `news_daily`(每晚 06:00)+
`register_news_runner`(app 啟動註冊,排程不 import api)、`finmind_datasets.fetch_taiwan_stock_news`
(免 token 可用,有 token 提高額度)。

**變數**:新增 `symbol_news_json`(新「新聞」分類、per_symbol、由 router 讀新聞庫餵入、近 7 日/
最多 10 則)—— 出現在設定頁變數區可自訂調用。註冊表 31→32、分類 8→9;vars.js 同步。
**個股健檢策略 v2.3** 新增「新聞事件」節;官方新聞提示詞納入模板庫(v3)。

## 三、閘門(全綠)

- pytest 全套(排除 e2e):**exit 0**(新增約 40 個單元/契約測:`portfolio_dash/news/*` + `test_news_api`)
- mypy --strict:**160 檔零錯**
- ruff:**全過**
- vars.js 語法:OK

## 四、真站端到端驗證(測試站,真實網路+LLM)

| 檢項 | 結果 |
| --- | --- |
| `news_daily` 真跑 | **organized 43 · headline-only 2 · skipped(去重)1**,status=ok |
| 新聞庫(直查 news.db) | 45 列(43 有 AI 摘要 + 2 只標題)、105 個提及索引、摘要品質良好 |
| `symbol_news_json`(台股 2330) | count=9,中文(FinMind+yfinance 合併,如 UDN) |
| `symbol_news_json`(美股 AAPL) | count=6,英文來源(如 Motley Fool)、摘要整理為繁中 |
| 新聞提示詞端點 | 官方預設已 seed、GET/PUT/reset 可用 |
| 成本 | news_organize $0.154 整理 43 則;額度剩 $4.15 |
| 健檢卡引用新聞(v2.3) | **9/9 張卡引用新聞事件**,run=ok;範例「NVDA 短線回檔、死亡交叉示弱,觀望為宜」(技術訊號＋新聞合流) |

## 五、設計決定與已知風險(誠實列表)

**記錄的架構決定**:新增**獨立 SQLite 新聞庫**(偏離「單一 DB 檔」的慣例)—— 使用者明示,
理由:新聞文字量大、與帳本分離、利未來多帳戶共享。路徑由 `db_path.parent/news.db` 推導,
兩環境(prod/demo)隔離自動生效。出版 v0.1.11 時寫入 CHANGELOG。

| 風險 | 等級 | 緩解 |
| --- | --- | --- |
| yfinance 逐股新聞相關性寬鬆(可能回大盤/他股新聞) | 中 | discovered_for 一律納入提及 → 該股查得到;卡片提示詞要求「解讀催化劑」由模型脈絡化;相關性為 yfinance 本質限制 |
| Yahoo 台股清單頁為 SPA,regex 解析產出可能少 | 低 | 已標為 best-effort,抓不到回 []、不影響 FinMind+yfinance 主源 |
| 抓取任意新聞網址(SSRF/重導/逾時/付費牆) | 中 | 抓取器有逾時、位元組上限、非 HTML 拒收、一律不 raise;**subagent 深審重點檢查此項** |
| 惡意文章提示詞注入(誘導模型輸出假代號/連結) | 中 | 整理提示詞限定忠於原文、不杜撰;**subagent 評估此風險** |
| 每晚成本隨持股數線性 | 低 | 每股上限 5、跨股去重、預算耗盡自動停;實測 43 則 $0.15 |
| 全文送 LLM 但只存摘要 | 低(法務) | 個人自用;只存 2-4 句摘要 + 來源連結,不存全文 |

## 六、Opus 4.8 subagent 系統級深審結果

> __狀態:背景執行中(think mode xHigh);審查範圍涵蓋整條分支(v0.1.10→HEAD),
> 重點為新聞管線的 runtime/coding/spec/risk bug 與穩定度。裁決與發現完成後補於此節。__

## 七、交付狀態與下一步

- 分支累計多個 commit(批次①任務包/收斂 + ②per_market + ③技術訊號/F&G + ④新聞管線),
  **仍未併版**,依約捆綁待出 **v0.1.11**(subagent 審查通過後)。
- 待你晨間決定:①subagent 若有 CHANGES-REQUIRED 項,我先修再出;②prod 點火時機;
  ③新聞管線是否要調整(每股則數、Yahoo 是否加強、成本上限)。
- 後續路線(依定案):分析師共識 → Vision/手動報告餵入。

---
*Human-facing completion deliverable (Traditional Chinese per report precedent);
code artifacts remain English. No credentials appear in this file.*
