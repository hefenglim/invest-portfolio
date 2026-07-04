# 「LLM 驅動的智慧投資組合監控」主題完整性評估報告

> 評估日期:2026-07-04 · 版本 v0.1.9 (commit dab235a) · 測試環境:test/demo instance(合成資料+真實資料源)
> 檢視:桌面 1440×900 / iPhone 390×844 · 本檔為報告存檔(原始互動版為工作階段 Artifact)

---

## 結論

「追蹤(track)」與「量化分析(analyze)」兩支柱已達生產級穩定 —— 雙版本 12 頁全數零錯誤、零溢出,35+ 項真實環境驗證全綠。主題第三支柱「LLM 驅動(powered by LLM)」處於**「已建成、未點火」**狀態:四角色模型管理、批次洞察管線、自我回測評分迴路(Loop 1–4)與 24 個資料變數全部就緒,僅差 API 金鑰與預算設定。對照業界做法,本專案架構原則(LLM 永不出具數字、結構化輸出驗證、批次+快取、自評迴路)與 2026 年公認最佳實踐一致,且自我進化迴路超前多數同類產品。

| 指標 | 結果 |
| --- | --- |
| 雙版本實測 | 24/24(12 頁 × 2 視窗,零 console 錯誤) |
| 主題功能覆蓋 | 2.5 / 3 支柱(track ✓ analyze ✓ LLM=待點火) |
| 七輪累計 | v0.1.3 → v0.1.9;3 個潛伏核心 bug 被真資料逼出並修復 |

## §1 測試方法與範圍

- **雙版本排版**:Playwright 以桌面(1440×900)與 iPhone(390×844、touch、DPR 3)逐頁載入,量測頁面本體水平溢出、收集 console/page 錯誤。
- **功能實測**:沿用前七輪真實驗證(一步新增、交易寫入、帳本更正、配息收件匣、現金池、匯出五端點、報價鏈順位),本輪加測:AI 輸入真實降級路徑、再平衡試算抽屜、what-if API、行動版漢堡導航。
- **AI 子系統探測**:不設金鑰打真端點,驗證「休眠但健康」——降級誠實、不假造、不崩潰。

## §2 雙版本實測結果

| 檢項 | 桌面 1440 | iPhone 390 | 備註 |
| --- | --- | --- | --- |
| 12 頁載入+零水平溢出 | PASS | PASS | 行動版由改造前 260–957px 溢出 → 全數 0(儀表板殘餘 5px 次感知) |
| 零 console/page 錯誤 | PASS | PASS | 全站掃描 |
| 導航 | 側欄 | 抽屜 ✓ | 漢堡 tap 開、遮罩/選單項/Esc 關、徽章隨行 |
| AI 輸入 → 真實降級 | PASS | — | 解析觸發 402 額度面板(預算 $0)— 誠實不假造 |
| 再平衡試算抽屜 | PASS | — | 現權重/目標%/動作/費稅欄完整,純試算不寫入 |
| What-if 試算 API | PASS | PASS | 買 2330 100 股 → 費 342(真台股費率 0.1425%·min 20) |
| iOS 人體工學 | — | 達標 | 16px 輸入防自動縮放、38–40px 觸控目標、safe-area |

## §3 主題對照:功能完整性地圖

### 支柱一:Track(追蹤)— 生產級 ✓

| 功能 | 狀態 | 實測證據 |
| --- | --- | --- |
| 五本帳(期初/交易/股利/換匯/現金)+重算重建 | 穩定 | append-only 精神+顯式更正;每筆更正經賣超/負池重放檢核 |
| 三市場報價(TW/US/MY)+可調抓取鏈 | 穩定 | 順位改了下一次刷新即生效(yfinance 優先 → 12 檔全由其回答) |
| 一步新增標的(板別+名稱+即時價+12 個月歷史) | 穩定 | 2317 鴻海 4.6 秒完成;查無報價擋下打錯代號 |
| 配息偵測 → 待確認收件匣(台美馬全模型) | 穩定 | 真實台積電配息 12,000 / NVDA DRIP / Maybank 淨額入帳驗證 |
| 現金池(入金/出金/換匯/收付)+負池守門 | 穩定 | −184,000 → 入金 → 換匯 → 池 +2,000 分毫不差 |
| 系統操作記錄+排程執行歷史(含來源明細) | 穩定 | 每筆變更可回溯;job detail 標明哪個來源答了哪些代號 |

### 支柱二:Analyze(量化分析)— 生產級 ✓

| 功能 | 狀態 | 實測證據 |
| --- | --- | --- |
| 調整成本法 P&L / 總報酬 / 回本進度 | 穩定 | 股利沖減不雙算;original cost 永不覆寫 |
| FX-aware XIRR(單一報告幣別) | 穩定 | FX 歷史回補後由「無法計算」→ 正常輸出 |
| 總市值 vs 累計淨投入逐日趨勢 | 穩定 | 179 點滿版;賣超日誠實標記 incomplete |
| 產業配置 / 幣別組成 / 換匯損益歸因 | 穩定 | 換匯損益=歸因拆解,永不疊加 |
| What-if 試算 / 再平衡試算(不寫入) | 穩定 | 費稅以帳戶規則組計算 |
| 月度 KPI 快照 | 穩定 | 每晚覆寫當月,月底定格;查表不重算 |
| 風險警示(觸價/集中度/FX 漂移/校準落差) | 運作中 | 收盤後排程掃描;測試站 4–5 則活警示 |

### 支柱三:Powered by LLM — 已建成,未點火 ⏸

缺的不是建設,是啟用:

| 子系統 | 建置 | 本輪探測 |
| --- | --- | --- |
| LiteLLM 多供應商接入(四角色:預設/視覺/主控/備援) | 已建成 | `/api/llm/config` 健康,四角色全 `null`(未設模型) |
| USD 預算治理+用量記帳 | 已建成 | 預算 $0 → 所有 AI 呼叫被 402 正確攔截 |
| 批次洞察管線(手動/排程,絕不同步於頁面載入) | 已建成 | `/api/insights`、`/api/insight-tasks` 健康、空狀態誠實 |
| 提示詞系統(系統提示+策略庫+版本化+快取指紋) | 已建成 | prompt-vars 健康;24 變數就緒(籌碼/估值/情緒/技術) |
| 自我進化迴路 Loop 1–4(產卡→每日評分→週校準→影子驗證) | 已建成 | 排程已註冊;無 runner 時安全略過 |
| AI 對帳單解析(文字→交易草稿) | 已建成 | 觸發真實 402 降級;Vision 截圖未接線(誠實標示) |
| **實際 LLM 產出洞察** | **未點火** | 需:金鑰+四角色模型+預算 → 首次批次執行 |

> 架構事實:點火後,報價/損益/報酬率仍 100% 由本地計算核心產出——LLM 只消費既算好的數字做敘事判讀(核心不變式 #1)。

## §4 探索性研究:業界做法 vs 本專案

| 業界共識做法(2025–2026) | 本專案現況 | 對照 |
| --- | --- | --- |
| 「LLM 解讀、引擎算數」:數字由計算引擎產出,LLM 只解讀 | 核心不變式 #1 | 一致 |
| 結構化輸出+schema 驗證;評測發現「格式品質與事實準確度幾乎零相關」 | Pydantic 驗證、失敗重試後回退快取/空狀態 | 一致 |
| groundedness 防幻覺:限定引用餵入上下文 | 提示詞帶量化錨點+XML 結構;變數缺失誠實降級 | 一致 |
| 回饋迴路與行為評測:事後評分、校準、LLM-judge | Loop 2–4:每日回測評分(pending-data 防毒)、週校準、影子批次+auto-promote | **超前** |
| 供應商中立、資料主權自持 | LiteLLM 換供應商=改設定;資料在自有 SQLite | 一致 |
| Agentic 即時網搜合成(研究:找贏家有效、辨輸家較弱) | 新聞/網搜變數未接線(qualitative 輸入位已預留) | **差距** |

評測警訊:多個 2026 基準發現部分模型產出「乾淨專業表格、填滿捏造數字」,正確答案反而是「有空格的表」——印證本專案「缺資料誠實標示、絕不填補」路線。

## §5 功能增強建議(優先序)

### P0 — 點火 LLM(設定級工程)
1. 設定 › LLM 與額度:填供應商金鑰(OpenRouter / Anthropic / OpenAI-compatible)、指派四角色模型、撥小額預算(如 $5/月)。
2. 先在**測試站**手動觸發首次批次洞察,走 Loop 1 → 隔日 Loop 2 評分 → 驗證快取與降級 → 才在 prod 啟用;用量成本已有逐 run 記帳。
3. 首批建議「持倉週報」型洞察(低頻、高價值、易評分)。

### P1 — 補業界差距(各半天~兩天)
- 新聞/qualitative 上下文接線(NewsAPI 已在資料源目錄 pending)—「smart monitor」與純報表的分水嶺。
- Vision 對帳單截圖解析(後端視覺角色已支援,前端上傳位已預留)。
- 洞察評分儀表化:/api/ai-score 已有命中率/校準資料,AI 洞察頁補「這個 AI 最近準不準」卡。

### P2 — 擇機
- 月報趨勢圖(快照累積 3+ 個月後)、行動版 PWA manifest、T3 防火牆收斂(運維)。

## §6 現況總覽(七輪版本歷程)

| 版本 | 輪次主題 | 關鍵產出 |
| --- | --- | --- |
| v0.1.3 | 倉位管理穩定化 | 按鈕接真、未註冊硬擋、永不 500 |
| v0.1.4 | UX round 2 | 一步新增、帳本更正、Progress 系統、建置識別 |
| v0.1.5 | 12 項指示 | 輸入中心收尾、操作記錄、市場報價順位(真鏈) |
| v0.1.6 | 4 項決定 | 配息收件匣點火、智慧回補窗+FX 歷史 |
| v0.1.7 | 收件匣全市場 | US DRIP / MY NET / 台股配股;NET 核心崩潰修復 |
| v0.1.8 | 8 項核准 | 現金池第五本帳+資金管理頁+月度快照 |
| v0.1.9 | 行動版 | ≤760px 版面層:抽屜導航、單欄化、iOS 人體工學 |

閘門狀態:pytest 全套+38 e2e 綠 · mypy --strict 150 檔零錯 · CHANGELOG 11 版 · prod = tag v0.1.9。
七輪間由真實資料逼出並修復的三個潛伏核心缺陷:賣超帳本經趨勢重放 500、持股數漏算期初/配股(誤報賣超)、MY 淨額股利型別使重建崩潰——均以「單一定義+端到端回歸」釘死(見 LESSONS_LEARNED)。

## §7 研究來源

- [Benchmark of 40+ LLMs in Finance (AIMultiple)](https://aimultiple.com/finance-llm)
- [Best Free AI Portfolio Trackers 2026 (Portfolio Genius)](https://portfoliogenius.ai/blog/best-free-ai-portfolio-trackers)
- [Best LLMs for Financial Analysis (Neurons Lab)](https://neurons-lab.com/articles/llms-for-finance/)
- [Best LLM for Financial Advice 2026 (FomoDejavu)](https://www.fomodejavu.com/blog/best-llm-ai-for-financial-advice-2026/)
- [AI in Portfolio Management (LeewayHertz)](https://www.leewayhertz.com/ai-for-portfolio-management/)
- [LLM Guardrails for Fintech (Maxim)](https://www.getmaxim.ai/articles/llm-guardrails-for-fintech-compliance-hallucination-prevention-and-audit-trails/)
- [JurisTech Hallucination Benchmark Report 2026](https://juristech.net/best-llm-tools-for-financial-analysis-2026/)
- [FinReasoning: Hierarchical Benchmark for Reliable Financial Research Reporting (arXiv)](https://arxiv.org/pdf/2603.19254)
- [Behavioral Evaluation of Agentic Stock Prediction Systems (arXiv)](https://arxiv.org/pdf/2605.05739)
- [Agentic AI Nowcasting Predicts Stock Returns (arXiv)](https://arxiv.org/pdf/2601.11958)
- [Minimize Hallucination Risk (Alkymi)](https://www.alkymi.io/resources/blog/minimize-hallucination-risk-while-maximizing-llm-output)
- [GenAI Use Cases in Portfolio Management (INDATA)](https://www.indataipm.com/top-3-use-cases-for-generative-ai-in-portfolio-management-trading/)

---
*Note: this report is intentionally in Traditional Chinese — it is a human-facing assessment deliverable stored at the user's explicit request (2026-07-04); code artifacts remain English per the bilingual protocol.*
