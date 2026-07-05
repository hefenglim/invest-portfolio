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

**裁決:APPROVE-WITH-FOLLOWUPS** —— 無 critical/high 缺陷,五大不變式全數成立,閘門全綠
(subagent 自行實跑:pytest **1270 passed / 3 skipped**、mypy 160 檔零錯、ruff 全過)。
發現 6 個 MEDIUM + 若干 LOW,**皆非出版阻擋項**(降級皆優雅、無資料損壞)。

**不變式與層界稽核(subagent 逐項)**:①LLM 不出數字 PASS(訊號皆純 Decimal;新聞只存摘要)
· ②金錢不用 float PASS(Decimal.sqrt、Decimal(str(x)))· ③LLM 批次+快取 PASS(整理只在
news_daily cron,`_news_var` 讀快取零 LLM)· ④單向層界 PASS(news/ 只依賴 shared+stdlib+
llm_insight 常數,不碰 api/web;排程用 register_news_runner)· ⑤優雅降級 PASS(強;每個失敗
模式都追過)。

**穩定度**:網路斷、付費牆、bot 擋、預算耗盡中途、空持股、重複重跑 —— subagent 逐一追蹤,
全部乾淨降級。

## 七、深審發現的即時修復(出版前三項 + 三項 trivial,已修並重驗)

趁夜間把 subagent 標為「出版前該修」的三項(清楚、低風險、真實)加上三個 trivial LOW
一併修掉,並補回歸測試、重跑全閘門:

| # | subagent 發現 | 修復 |
| --- | --- | --- |
| 1 (MED) | 去重時漏記「本股」提及 → 同類股可能查不到自己來源的新聞 | 去重分支改為 `add_mention(link, 本股)` 補記提及索引(+回歸測試) |
| 2 (MED) | 技術訊號只餵 180 日曆天(≈123 交易日),52 週位階/MA120 名不副實 | 技術訊號改餵 **400 天** close 序列(回填已存 365 天);price_history_json 仍只取近 180 天再降採樣(token 不變) |
| 3 (MED) | HTML 抓取器無 scheme 白名單(file:///SSRF 風險) | `_default_opener` 只允許 http(s),其餘請求前即拒(+回歸測試) |
| 4 (LOW) | from_yfinance 把 UNIX epoch 當成 "1720" 垃圾日期 | `_parse_yf_date`:strptime 驗真日期 + 轉換 epoch 秒(+回歸測試) |
| 5 (LOW) | 死碼 `llm_off` flag | 移除 |
| 6 (LOW) | 預覽路徑未把 now 傳入新聞窗口 | `_build_context` 補 `now=now` |

**修後重驗**:mypy 160 檔零錯、ruff 全過、full suite exit 0(新增 3 個 SR 回歸測試)。

**記錄為待你裁決的 follow-up(取捨型,未擅自改)**:
- (MED-4)自訂 per_market 提示詞若引用 `kpis_json/fx_json/fx_rates_json` 會看到全組合彙總
  (官方市場模板已刻意不用,故現況安全)—— 要「切片」還是「在 validate_tokens 擋下」由你定。
- (MED-5)整理器抽出的 `related_stocks` 用於跨股索引 → 惡意文章可能讓假關聯股現身他股卡
  (僅敘事層,不變式 #1 不破)—— 要不要「只信 discovered_for/限held 清單」由你定。
- (MED-3)抓不到的降級列永不重整理 → 一次性 blip 會讓該文永遠只剩標題(7 日內自然滾出)
  —— 要「隔日重試」還是「維持不重試省成本」由你定。
- 其餘 LOW:Big5 解碼、news.db 保留策略/納入備份 —— 皆可延後。

## 七、交付狀態與下一步

- 分支累計多個 commit(批次①任務包/收斂 + ②per_market + ③技術訊號/F&G + ④新聞管線),
  **仍未併版**,依約捆綁待出 **v0.1.11**(subagent 審查通過後)。
- 待你晨間決定:①subagent 若有 CHANGES-REQUIRED 項,我先修再出;②prod 點火時機;
  ③新聞管線是否要調整(每股則數、Yahoo 是否加強、成本上限)。
- 後續路線(依定案):分析師共識 → Vision/手動報告餵入。

---
*Human-facing completion deliverable (Traditional Chinese per report precedent);
code artifacts remain English. No credentials appear in this file.*
