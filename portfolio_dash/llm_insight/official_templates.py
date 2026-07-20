"""The OFFICIAL prompt-template library (AI-input optimization program, 2026-07-05).

Single source of truth for the shipped best-default prompt content — the user-facing
first-touch experience. The settings UI reads it via ``GET /api/prompt-templates``,
"reset to official" restores :data:`SYSTEM_PROMPT_BODY`, and "add from library" copies
a strategy body the user then owns. Fresh installs seed the system prompt from here
(``system_prompt.DEFAULT_SYSTEM_PROMPT`` aliases it).

Content is Traditional Chinese by design (it is LLM- and user-facing); identifiers and
comments stay English per the bilingual protocol. Every template carries a version tag
so a future library update can offer "official has a newer version" upgrades. The
master-role prompt (``master.py``) and the AI-input parse prompt
(:data:`AI_INPUT_PROMPT_BODY`, below) are code-owned and NOT user-editable — the
AI-parse prompt lives here as a versioned constant but is deliberately kept OUT of the
user-facing :func:`library_wire` payload (``agents.py`` imports it and fills the dynamic
``{accounts}`` / ``{today}`` / ``{text}`` placeholders).
"""

from typing import Literal, TypedDict

from portfolio_dash.shared.sectors import GICS_SECTOR_KEYS

# LIBRARY_VERSION tags the shipped default prompt CONTENT — bump it whenever any default
# prompt body/version below changes (the user-visible "official has a newer version" signal).
LIBRARY_VERSION = "official-v10 (2026-07-21)"

# ─── HOW TO ADD A PROMPT (FU-D30 site-wide prompt registry) ────────────────────────────
# Every prompt the app sends to an LLM MUST be traceable to THIS module:
#   1. Add the prompt body here as a module constant WITH a sibling ``*_VERSION`` tag — never
#      inline a prompt literal at the call site (the completeness test rejects new call sites
#      that do not trace back here).
#   2. A USER-EDITABLE prompt keeps its live value in a DB row; the constant here is that
#      row's DEFAULT/seed (reset-to-official restores it). Declare the DB storage in the entry.
#   3. Register it in :data:`PROMPT_REGISTRY` (bottom of this file) so every prompt is
#      enumerable and ``tests/llm_insight/test_prompt_registry.py`` can trace each LLM call
#      site to an entry. Bump :data:`LIBRARY_VERSION` when default CONTENT changes.
# Architecture note (tiers / how features fetch / how to add a prompt):
#   docs/reports/2026-07-18-prompt-registry.md
# ───────────────────────────────────────────────────────────────────────────────────────

# --- AI transaction-input parse prompt (code-owned, FU-D20 2026-07-17) ---------------
# Moved here from ``data_ingestion/agents.py::_PROMPT`` so all shipped prompt content has
# a single home; still code-owned (NOT in the user-editable library dict). ``agents.py``
# calls ``.format(accounts=…, today=…, text=…)`` — the ``{{`` / ``}}`` escape the literal
# JSON braces so only those three placeholders interpolate. Extended for screenshots: an
# attached broker-statement image may list MULTIPLE transactions, so the model must emit
# one draft per visible row under the same JSON-only contract.
# v3 (FU-D41, 2026-07-19): explicit LOCAL-exchange-code rule — 「前天聯電買入1張」 on a TW
# account must parse to 2303, never the US ADR ticker UMC; agents.py adds a matching
# soft row-level format check post-parse (warning only, never rewrites the symbol).
# v4 (W1 batch-A, 2026-07-21): MY (Bursa) guidance raised to TW parity — 4-digit code
# exemplars, the ACE-market leading-zero rule (0166, never 166), and the brand/mall →
# listed-parent rule (IOI Mall ⇒ IOI Properties). Mirrors AI_INSTRUMENT_RESOLVE_PROMPT.
AI_INPUT_PROMPT_VERSION = "v4"
AI_INPUT_PROMPT_BODY = (
    "<task>Extract stock transactions from the user's text and any attached statement\n"
    "screenshot into JSON.</task>\n"
    '<schema>{{"drafts": [{{"account_id","symbol","side":"BUY|SELL","date":"YYYY-MM-DD",\n'
    '"shares","price","daytrade":false,"is_etf":false,"note"}}]}}</schema>\n'
    "<accounts>{accounts}</accounts>\n"
    "<today>{today}</today>\n"
    "<example_input>在元大買 10 股 2330 @ 600</example_input>\n"
    '<example_output>{{"drafts":[{{"account_id":"tw_broker","symbol":"2330","side":"BUY",\n'
    '"date":"2026-06-01","shares":"10","price":"600"}}]}}</example_output>\n'
    "<example_input>7/1 嘉信買 AAPL 5股 @210，隔天再買 5 股 @212</example_input>\n"
    '<example_output>{{"drafts":[{{"account_id":"schwab","symbol":"AAPL","side":"BUY",\n'
    '"date":"2026-07-01","shares":"5","price":"210"}},{{"account_id":"schwab",\n'
    '"symbol":"AAPL","side":"BUY","date":"2026-07-02","shares":"5","price":"212"}}]}}\n'
    "</example_output>\n"
    "<rules>Return JSON only, no prose. account_id MUST be one of the ids listed in\n"
    "<accounts> (match the user's broker wording to the account name); never invent\n"
    "an id. symbol MUST be the LOCAL exchange code of the ACCOUNT's market, never a\n"
    "cross-listed/ADR ticker from another exchange: a TW (TWD) account takes the\n"
    "TWSE/TPEx numeric code (聯電⇒2303, 台積電⇒2330, 鴻海⇒2317 — NEVER US tickers\n"
    "like UMC/TSM even if the company also trades as a US ADR); a US (USD) account\n"
    "takes the US ticker (AAPL); a MY (MYR) account takes the 4-digit Bursa code\n"
    "(Maybank⇒1155, Tenaga⇒5347, Inari⇒0166 — ACE-market codes KEEP the leading\n"
    "zero: 0166, never 166; map a brand/mall/subsidiary to its LISTED parent, e.g.\n"
    "IOI Mall⇒IOI Properties 5249). A Chinese company name on a TW account always\n"
    "maps to its numeric code. Dates resolve against <today>: a month/day without a\n"
    "year means the most\n"
    "recent PAST occurrence (a trade date is never in the future); relative words\n"
    "(今天/昨天/上週五) resolve from <today>. One draft per transaction — text may\n"
    "contain several. An attached screenshot is a broker statement that may list\n"
    "MULTIPLE transactions: read every visible row and emit one draft per row; the\n"
    "same JSON-only contract applies to text and images alike.</rules>\n"
    "<user_text>{text}</user_text>"
)

# The news-organizer system prompt (batch ④): the default LLM turns a fetched article's
# text into a structured, faithful summary. Editable by the user (news settings), with a
# reset-to-official path — same first-touch-optimum + customization model as the others.
NEWS_ORGANIZER_PROMPT_VERSION = "v2"
NEWS_ORGANIZER_PROMPT = (
    "你是財經新聞整理員。輸入是一篇新聞文章的正文（可能夾雜網頁雜訊）。\n"
    "請忠實整理成結構化資訊，只根據原文，不得杜撰或加入原文沒有的內容或數字。\n"
    "<rules>\n"
    "1. 一律使用繁體中文（台灣用語）。\n"
    "2. body_summary：2–4 句重點摘要，忠於原文、不評論、不加料；原文若含數字照原文引用，"
    "不得自行計算或推估。\n"
    "3. news_date：文章日期，格式 YYYY-MM-DD；原文無明確日期時，留給呼叫端提供的預設。\n"
    "4. related_stocks：文章提及的個股，回傳其代號（台股用數字代號如 2330、美股用英文代號"
    "如 AAPL）；沒有明確提及個股時回空陣列。\n"
    "5. title：若原文標題可辨識則沿用，否則以一句話擬定精簡標題。\n"
    "6. 若正文並非實質新聞內容（如程式碼、樣式表、導覽選單雜訊），body_summary 一律留空"
    "字串，不得描述或摘要這些雜訊。\n"
    "</rules>\n"
    "只回傳一個 JSON 物件，不要 Markdown 圍欄、不要額外散文。"
)

SYSTEM_PROMPT_VERSION = "v2"
SYSTEM_PROMPT_BODY = """你是資深投資組合分析師，服務一位同時持有台股、美股、馬股的個人長期投資者。

原則：
1. 一律使用繁體中文（台灣用語）；損益語意採台灣慣例：紅漲綠跌；金額必須標注幣別，
   不同幣別不可加總。
2. 時效第一：市場變化快速。每個結論標注所依據資料的基準日；區分最新與較舊資料，
   愈近期的資料權重愈高；輸入標記過期（stale）或缺漏時必須如實點名，絕不以猜測或
   外部記憶填補數字。
3. 所有判讀必須引用輸入資料中的具體數字。
4. 輸出結構：title 一眼可讀（不超過 20 字，含主體與方向）；summary 為 2-3 句可獨立
   成立的重點；body_md 展開細節（條理分節、精簡扼要）；tags 從「趨勢、風險、配置、
   股利、匯率、籌碼、估值、技術、事件」中選用。
5. 可給方向性判讀（偏多／偏空／觀望）與條件式情境（例：「跌破 60 日均線宜重新評估」），
   並說明所依據的數據；不給具體買賣指令、不建議部位大小、不代替使用者決策。
6. confidence＝你對預測命中的真實機率估計，寧可保守也不要過度自信；系統會回測你的
   信心值與實際命中率的落差。"""

# --- Code-owned prompts consolidated into the library (FU-D30, 2026-07-18) -------------
# Previously inline literals at their call sites; moved here so ALL shipped prompt content
# has one home + a version tag. They stay code-owned (NOT surfaced in the user-editable
# ``library_wire`` payload). Bodies are BYTE-IDENTICAL to the pre-migration literals — only
# their home changed; the call sites now import these names.

# Digest daily one-liner (api/digest_service.py::_llm_note): the model narrates ONLY the
# pre-computed numbers handed to it (invariant #1: the LLM emits no numbers of record).
# ``{numbers}`` is the sole ``str.format`` placeholder (a JSON blob of already-computed
# figures — its own braces are the substituted VALUE, never re-scanned by ``format``).
DIGEST_NOTE_PROMPT_VERSION = "digest-daily-note-v1"
DIGEST_NOTE_PROMPT_BODY = (
    "你是投資組合摘要助理。以下是今日已計算好的數字（JSON）。\n"
    "<numbers>\n"
    "{numbers}\n"
    "</numbers>\n"
    "請用繁體中文寫『一句話』的收盤摘要，只能引用上面提供的數字，"
    "不得杜撰任何新數字或金額。不要加上金額符號。"
)

# AI instrument-resolve prompt (api/routers/instruments.py::ai_resolve, R6-B 2026-07-19).
# ONE code-owned prompt serving every registration entry point (manual-trade / AI-input / CSV
# quick-add dialogs + the watchlist surfaces): the DEFAULT role maps the user's raw input
# (company name / wrong-form ticker such as a US ADR code) to the target market's LOCAL
# exchange code + name + GICS sector (+ optional GICS industry) in a SINGLE structured reply.
# It SUPERSEDES the two former single-purpose prompts (AI_SECTOR_PROMPT + AI_SYMBOL_RESOLVE_
# PROMPT), consolidating symbol-resolution and sector-classification behind one contract.
#
# The reply is ADVISORY ONLY: the endpoint re-maps gics_sector through ``canonical_sector``
# and re-verifies the returned symbol against the REAL provider quote/name lookup before any
# auto-fill (invariant #1: the LLM supplies no number of record — a symbol/sector/industry is
# a qualitative identification, not a price/return).
#
# The 11 GICS sector keys are embedded from ``GICS_SECTOR_KEYS`` (shared/sectors.py — the
# single vocabulary source) via a MODULE-LEVEL join, so the allowed list can never drift from
# the canonical vocabulary while the constant below stays a plain str. ``.format`` placeholders
# are {query} and {market} ONLY; the ``{{`` / ``}}`` escape the literal JSON braces in the
# one-shot example (whose sector uses a real GICS key).
_GICS_SECTOR_LIST = ", ".join(GICS_SECTOR_KEYS)

# v2 (W1 batch-A, 2026-07-21): the MY (Bursa) clause was one thin line vs. rich TW guidance —
# the app's weakest resolve market. Raised to TW parity: 4-digit code exemplars (verified
# against the fetched Bursa directory), the ACE-market leading-zero rule (0166, never 166),
# and the brand/mall/subsidiary → LISTED-parent rule (IOI Mall → IOI Properties). Paired with
# the baked ``pricing.bursa_registry`` so a valid code now verifies offline → status:"resolved".
AI_INSTRUMENT_RESOLVE_PROMPT_VERSION = "v2"
AI_INSTRUMENT_RESOLVE_PROMPT = (
    "<task>你是股票標的判讀助理。使用者輸入了一段可能是公司名稱、俗稱，或某種形式的代號"
    "（可能是錯誤形式，例如把台股打成美股 ADR 代號）。請在『目標市場』中判讀他實際想指的"
    "股票，並一次回傳：該市場的『當地交易所代號』、正式名稱、GICS 產業類別，以及可選的 "
    "GICS 產業細分。</task>\n"
    "<market_rules>symbol 必須是『目標市場』的當地交易所代號，格式如下，且絕不可回傳其他"
    "交易所的代號：\n"
    "・TW（台股，TWSE/TPEx）：4-6 位數字＋可選英文字尾（如 2330、00878B）。必須輸出當地"
    "交易所代號：聯電⇒2303、台積電⇒2330、開發金⇒2883，絕不可回傳 UMC／TSM 等美股 ADR "
    "代號；中文公司名一律對應其數字代號。\n"
    "・US（美股，NYSE/NASDAQ）：1-5 位英文字母＋可選 .X 類別字尾（如 AAPL、BRK.B）。\n"
    "・MY（馬股，Bursa Malaysia）：4 位數字代號（Main Market 多為 1xxx–9xxx，ACE Market "
    "多以 0 開頭）。必須輸出當地交易所代號：Maybank／馬銀行⇒1155、Public Bank／大眾銀行"
    "⇒1295、Tenaga Nasional／國家能源⇒5347、CIMB⇒1023、Inari Amertron⇒0166、IOI "
    "Corporation⇒1961、IOI Properties⇒5249。ACE Market 及以 0 開頭之代號必須保留前導零"
    "（Inari 為 0166，絕不可寫成 166）。品牌、商場或子公司名一律對應其『上市母公司』"
    "（例：IOI Mall／IOI 廣場 → IOI Properties＝5249）；若查無對應之上市母公司則回 "
    "not_found，絕不可捏造代號。</market_rules>\n"
    "<gics_sectors>gics_sector 欄位必須『完全等於』下列 11 個英文 GICS 產業鍵之一（大小寫、"
    "空格、字元皆同），不得自創、翻譯或輸出清單以外的類別：" + _GICS_SECTOR_LIST +
    "</gics_sectors>\n"
    "<input>使用者輸入：{query}\n目標市場：{market}</input>\n"
    "<rules>\n"
    "1. 有把握（單一明確標的）：confidence=\"high\"，填 symbol／name／gics_sector"
    "（gics_industry 選填，填 GICS 產業細分或給 null），candidates 一律留空陣列。\n"
    "2. 不確定（可能對應多檔）：confidence=\"medium\" 或 \"low\"，並在 candidates 給 2-5 個"
    "候選 {{symbol,name,gics_sector}} 供使用者挑選。\n"
    "3. 若該標的在『目標市場』中不存在／查無此標的：not_found=true，symbol 留空、candidates "
    "留空陣列，絕不可捏造任何代號。\n"
    "4. 你只輸出識別與分類這類定性資訊，不得輸出任何價格、報酬或數字資料；你的回覆僅是建議，"
    "系統會以真實報價覆核，覆核不通過即不採用。\n"
    "</rules>\n"
    "<example_output>{{\"symbol\": \"2303\", \"name\": \"聯華電子\", "
    "\"gics_sector\": \"Information Technology\", \"gics_industry\": \"Semiconductors\", "
    "\"confidence\": \"high\", \"candidates\": [], \"not_found\": false}}</example_output>\n"
    "只回傳一個 JSON 物件，不要 Markdown 圍欄、不要額外散文。"
)

# On-alert insight addendum (llm_insight/generate.py): appended to an assembled insight
# prompt when a card is risk-alert-triggered, forcing the ≤3-trading-day window (spec 4.10).
ON_ALERT_NOTE_VERSION = "v1"
ON_ALERT_NOTE = (
    "\n\n[預警解讀守則] 本卡由風險預警觸發，請給出極短期（≤3 個交易日）的觀察與預測，"
    "聚焦此事件的即時影響。"
)

# Master-role system prompts (llm_insight/master.py): the scoring rubric, the calibration
# §4.8 safety lock, and the calibration safety validator. Code-owned, master-role, each a
# strict JSON-only contract. ``master.py`` imports these (aliased to its private names).
MASTER_SCORE_PROMPT_VERSION = "v2"  # 2026-07-05 audit §2.4 rubric
MASTER_SCORE_SYSTEM = (
    "<role>你是投資洞察回測評分大師。輸入為一張到期洞察卡的原文、產卡當時的輸入快照、"
    "以及到期時的實際結果。請依下列準則評估該卡的『敘事準確度』。</role>\n"
    "<rubric>\n"
    "四維度加權（總分 0-100）：\n"
    "1. 方向正確性 40%：卡片的方向性判讀（偏多／偏空／觀望）與實際走勢的相符程度。\n"
    "2. 數字引用正確性 30%：卡內引用的數字是否忠於產卡當時的輸入快照；"
    "引用快照中不存在的數字＝捏造，重罰。\n"
    "3. 條件情境效度 20%：卡片給的條件式情境（如「跌破 60 日均線宜重新評估」）"
    "是否被實際走勢觸發且有效。\n"
    "4. 時間性 10%：卡片是否標注資料基準日、結論是否與資料時效相符"
    "（把過期資料當即時資料使用＝扣分）。\n"
    "分數錨：90-100＝方向對、數字全對、情境被驗證；70-89＝方向對但有小瑕疵；"
    "50-69＝方向模糊或部分引用有誤；30-49＝方向錯但有誠實的避險語；"
    "0-29＝方向錯且引用捏造或誤導。\n"
    "miss=true 的定義：方向判讀與實際走勢相反，或卡內出現快照中不存在的捏造數字。"
    "資料不足以判定時維持 miss=false 並在 note 說明原因。\n"
    "</rubric>\n"
    "<rules>數字一律以輸入為準，不得自行捏造價格或報酬；只評敘事品質，不重算損益。"
    "note 必須引用具體證據（卡內原句對照實際數字），不接受空泛評語。</rules>\n"
    "<output>僅回傳 JSON："
    "{\"narrative_score\": 0-100 整數, \"miss\": true|false, \"note\": \"具體證據簡評\"}，"
    "不要 Markdown 圍欄、不要額外散文。</output>"
)

MASTER_CALIBRATION_PROMPT_VERSION = "v1"
MASTER_CALIBRATION_SYSTEM = (
    "<role>你是投資洞察系統的校正大師。你只負責改寫『敘事品質校正規則』，"
    "絕不下達任何買賣、加減碼、調整持倉部位的越權建議，也絕不要求幣別混算。</role>\n"
    "<safety_lock>\n"
    "1. 校正規則只能附加/精煉：新增規則時必須重構並精簡既有邏輯，避免條款膨脹。\n"
    "2. 全文總字數不得超過 600 字（精簡為先）。\n"
    "3. 不得為了避免失誤而產出含糊、無預測價值的廢話；每條規則都要可檢驗。\n"
    "4. 保留仍有效的條款，只修訂已失效的；個股層級的失誤寫成「（個股）…」條款。\n"
    "5. 時效優先：校正規則應要求洞察標注資料基準日，並以近期資料為主要判讀依據。\n"
    "</safety_lock>\n"
    "<output>僅回傳 JSON：{\"body\": \"完整新版校正規則\", \"cause\": \"本次修訂原因\"}，"
    "不要 Markdown 圍欄、不要額外散文。</output>"
)

MASTER_VALIDATE_PROMPT_VERSION = "v1"
MASTER_VALIDATE_SYSTEM = (
    "<role>你是校正規則安全審查員。審查一段候選校正規則是否越權"
    "（下達買賣/加減碼/調整持倉等投資指令）或要求幣別混算。</role>\n"
    "<output>僅回傳 JSON：{\"ok\": true|false, \"reasons\": [\"...\"]}；"
    "ok=true 表示安全可採用。不要 Markdown 圍欄。</output>"
)

_WEEKLY_BODY = """讀者是長期投資人，每週檢視一次組合。請以「本週持倉週報」為題產出一張綜合洞察卡。

〇、本週一句話 — 綜合全部輸入，指出本週組合最值得注意的一件事及其數據依據（放在最前面）。

一、組合總覽 — 引用總市值、總報酬、XIRR、已實現/未實現拆分，總評組合狀態，標注資料基準日。
{{kpis_json}}

二、配置觀察 — 由產業配置與持倉權重點出集中度最高的部位及其風險意涵，與總覽相互印證。
{{allocation_json}}
{{holdings_json}}

三、幣別與匯率 — 各幣別報酬分列評述（不可加總）；以換匯損益歸因說明匯率對組合的影響方向，
並連結第二節的配置結論。引用換匯損益時注意幣別與量級：它是總報酬的歸因拆解（不可疊加），
引用前先與組合總市值對照合理性。
{{returns_by_ccy_json}}
{{fx_json}}
{{fx_rates_json}}

四、股利現金流 — 合併評述未來除息事件與年度已宣告股利：下一筆現金流的時點、金額、距今天數。
{{ex_dividend_calendar_json}}
{{dividend_projection_json}}

五、市場環境 — 以情緒指標與三地大盤 20 日動能一句話定調環境，並說明各指標的資料時點。
{{market_sentiment_json}}
{{index_quotes_json}}

守則：現在時間 {{now}}、資料基準 {{as_of}} — 請在卡首標注基準日；依 {{freshness_json}}
檢查新鮮度，缺價或過期的標的必須點名並排除於結論之外；愈近期的資料權重愈高。
本卡為純敘事回顧，不附預測（prediction 留空）。"""

_CHECKUP_BODY = (
    "讀者是長期投資人。請對下列標的做一次持股健檢，產出一張洞察卡"
    "（title 含標的與方向）：\n"
    + """{{symbol_detail_json}}

一、部位現況 — 現價相對原始/調整均價的位置與未實現損益（引用具體數字與資料基準日）。
同一標的可能分佈於多個帳戶：請分帳戶列示或明確標注「合計」，不得把單一帳戶數字當成總計。
{{price_vs_cost_json}}

二、技術面 — 均線位置與乖離、30 日波動與回撤；並解讀整合技術訊號：RSI(14) 的超買/超賣、
20/60 均線的黃金/死亡交叉與距今天數、52 週位階、趨勢結構（上升/下降/區間）。價格序列為
「近 30 個交易日逐日＋其餘每 5 日取樣」：以近 30 日為主要判讀窗口，較早的取樣點僅作趨勢脈絡。
{{ma_signals_json}}
{{technical_signals_json}}
{{volatility_json}}
{{price_history_json}}

三、法則訊號（TechScore 與四法則綜合）— 引用法則引擎的輸出，只詮釋、不重算、不虛構：
1) TechScore（0-100）與涵蓋度 coverage：一句話定調綜合技術強弱，並說明有幾條法則可評估；
2) 逐一點名四法則的狀態與其關鍵證據數字：趨勢濾網（相對 MA200 的偏離 price_vs_ma）、
均線交叉（黃金／死亡與距今天數 days_ago）、12-1 動能（return_12_1）、RSI 情境（rsi14）；
3) 條件語：照實引述引擎給的情境註記（evaluation_context／context_note），不得改寫其結論；
4) 法則訊號須與第二節的逐項技術指標相互印證；若 rule_signals_json 為 unavailable，直說
「法則訊號資料不足」，不得自行計算 TechScore 或杜撰任何法則狀態。
本標的若未持倉（held=false 或 symbol_detail_json 無部位），本節與第八節一律改以「建倉評估」
視角敘述（此刻是否為進場／觀望時機），而非加碼／減碼。
{{rule_signals_json}}

四、籌碼與基本面（僅台股有值；變數為空時整節跳過，不得虛構）— 法人買賣超與連買賣天數、
融資融券變化、月營收動能、估值位階（PER/PBR 歷史百分位）、近四季財報摘要；點出籌碼大戶
動向與基本面是否相互印證，並注意各資料的日期新舊。
{{institutional_json}}
{{margin_json}}
{{monthly_revenue_json}}
{{valuation_json}}
{{financials_json}}
（非台股標的：改以技術面、價格 vs 成本、環境對照為判讀支柱，並如實說明籌碼資料不適用。）

五、分析師共識 — 引用分析師目標價區間：現價相對均值／中位／最高／最低目標價的位置，
以及與均值目標價的上檔空間（upside_vs_mean_pct）；本月評級分布（強力買進…強力賣出）與
加權評級分數，並對照上月分布點出月度變化（趨勢轉強或轉弱）。評估市場對此標的的集體看法
是否與前述技術／基本面相互印證；共識僅供估值脈絡，不取代自身判讀。若 consensus_json 為
unavailable（無分析師覆蓋），必須明講「無分析師覆蓋」，不得虛構任何目標價或評級。
{{consensus_json}}

六、新聞事件 — 近期經整理的個股新聞（標題／日期／摘要）：解讀近期催化劑或風險事件，
與前述技術/基本面是否相互印證。新聞僅供背景判讀，不得從新聞取價格或報酬等數字；
無新聞時如實說明「近期無新聞」。
{{symbol_news_json}}

七、環境對照 — 相對所屬大盤的強弱與當前市場情緒，標注指標時點。
{{index_quotes_json}}
{{market_sentiment_json}}

八、方向性判讀與預測 — 綜合以上給出偏多／偏空／觀望之一，並附：
1) 加碼／減碼參考框架（作為長期持倉評估依據，不是買賣指令；未持倉標的改以建倉／觀望情境
描述，取代加碼／減碼）：以技術與法則訊號描述條件式情境，例「黃金交叉成立且 RSI 未過熱
（<70）、趨勢結構為上升、TechScore 偏強 → 屬偏多的加碼（或建倉）評估情境」、「跌破 60 日
均線且趨勢結構轉為下降、RSI 走弱 → 屬減碼（或觀望）重新評估情境」；明確寫出觸發條件
與對應方向，只到條件與方向，不給部位大小或買賣指令；
2) prediction：metric 一律用 price_change；direction 用 up/down/flat（預期兩週內漲跌幅在
±0.5% 以內才用 flat）；target_pct 僅在有明確依據時提供（小數比率，如 0.03＝+3%）；
3) confidence＝此預測命中的真實機率估計（0-100，寧可保守）。

守則：現在時間 {{now}}、資料基準 {{as_of}} — 在卡首標注基準日；依 {{freshness_json}}
標記新鮮度；缺漏資料如實說明；愈近期的資料權重愈高。"""
)

_MARKET_BODY = (
    "讀者是長期投資人。以下輸入已由系統切成「單一市場」的資料切片"
    "（只含這個市場的持倉），\n"
    + """請產出這個市場的部位週報卡（title 含市場名稱與本週重點）：

〇、本市場一句話 — 指出這個市場部位本週最值得注意的一件事及其數據依據（放在最前面）。

一、部位與配置 — 這個市場的持倉明細與市場內產業配置：點出最大部位、集中度與風險意涵，
引用具體數字與資料基準日。
{{holdings_json}}
{{allocation_json}}

二、報酬 — 這個市場（原幣別）的已實現/未實現與報酬率；只談本市場，不與其他市場比較加總。
{{returns_by_ccy_json}}

三、股利現金流 — 本市場未來除息事件與年度已宣告股利：下一筆現金流的時點、金額、距今天數。
{{ex_dividend_calendar_json}}
{{dividend_projection_json}}

四、市場環境 — 以本市場大盤指數與全球情緒指標定調環境，標注資料時點。
{{index_quotes_json}}
{{market_sentiment_json}}

守則：現在時間 {{now}}、資料基準 {{as_of}} — 在卡首標注基準日；依 {{freshness_json}}
檢查新鮮度，缺價或過期的標的必須點名並排除於結論之外；愈近期的資料權重愈高。
金額一律照輸入數字的原始數值與單位逐字引用，不得自行換算成「萬／百萬」等單位
（例：輸入 4290.80 就寫 4,290.80，不得寫成 429 萬）。
匯率換算與換匯損益屬全組合層次，請見全組合週報，本卡不評匯率歸因。
本卡為純敘事回顧，不附預測（prediction 留空）；tags 請包含市場名稱（台股／美股／馬股）。"""
)

# Strategy templates: (name, version, scope hint, body). ``scope`` is advisory — the
# composer binds scope on the insight TYPE; the hint tells the UI which tasks fit.
STRATEGY_TEMPLATES: list[dict[str, str]] = [
    {"name": "持倉週報策略", "version": "v2.1", "scope": "portfolio", "body": _WEEKLY_BODY},
    {"name": "個股健檢策略", "version": "v2.5", "scope": "per_symbol", "body": _CHECKUP_BODY},
    {"name": "市場週報策略", "version": "v1.1", "scope": "per_market", "body": _MARKET_BODY},
]


class TaskPreset(TypedDict):
    """One official-pack insight-task preset (a complete, schedulable task)."""

    preset_key: str  # stable provenance key stamped on created tasks (M3 fix)
    name: str
    version: str
    scope: str
    strategy: str  # references a STRATEGY_TEMPLATES entry by name
    use_system_prompt: bool
    self_correct: bool
    horizon_days: int
    suggested_cron: str  # Asia/Taipei; the pack mounts this on creation
    description: str


# The official pack (usability decision ①, 2026-07-05): one click creates these tasks
# complete with strategy, knobs, and a mounted weekly schedule — prod ignition becomes
# key → roles → top-up → one click. Crons: weekly report Saturday morning (after the US
# Friday close); per-symbol checkup Monday morning (TW chips from Friday are in).
TASK_PRESETS: list[TaskPreset] = [
    {
        "preset_key": "weekly",
        "name": "持倉週報",
        "version": "v1",
        "scope": "portfolio",
        "strategy": "持倉週報策略",
        "use_system_prompt": True,
        "self_correct": False,
        "horizon_days": 14,
        "suggested_cron": "0 9 * * sat",
        "description": "全組合敘事週報（純敘事，不附預測）",
    },
    {
        "preset_key": "checkup",
        "name": "個股健檢",
        "version": "v1",
        "scope": "per_symbol",
        "strategy": "個股健檢策略",
        "use_system_prompt": True,
        "self_correct": True,
        "horizon_days": 14,
        "suggested_cron": "0 9 * * mon",
        "description": "逐持股健檢（帶方向預測＋信心值，宇宙跟隨持倉）",
    },
    {
        "preset_key": "market",
        "name": "市場週報",
        "version": "v1",
        "scope": "per_market",
        "strategy": "市場週報策略",
        "use_system_prompt": True,
        "self_correct": False,
        "horizon_days": 14,
        "suggested_cron": "30 9 * * sat",
        "description": "台股／美股／馬股各一張市場部位週報（純敘事，資料自動市場切片）",
    },
]


def library_wire() -> dict[str, object]:
    """The ``GET /api/prompt-templates`` payload: version + system prompt + strategies
    + task presets (the one-click official pack)."""
    return {
        "library_version": LIBRARY_VERSION,
        "system_prompt": {
            "version": SYSTEM_PROMPT_VERSION,
            "body": SYSTEM_PROMPT_BODY,
        },
        "strategies": [dict(t) for t in STRATEGY_TEMPLATES],
        "task_presets": [dict(p) for p in TASK_PRESETS],
    }


# --- Site-wide prompt registry (FU-D30, 2026-07-18) ------------------------------------


class PromptRegistryEntry(TypedDict):
    """One row of the site-wide prompt registry: the single authoritative index of every
    prompt the app sends to an LLM.

    Two legitimate tiers, both enumerated here:
      * ``code-owned`` — a versioned default constant in THIS module IS the prompt of record
        (``storage`` empty).
      * ``user-editable`` — the live value is a DB row; the constant here is its DEFAULT/seed
        (reset-to-official restores it). ``storage`` names the DB table/row.
    ``runtime-generated`` covers the self-correct calibration layer (produced by the master,
    stored in the composer tables — no static default, so ``default_constant`` is empty).

    Fields: ``key`` (stable id) · ``feature`` (zh-TW description) · ``tier`` · ``version``
    (the default's tag) · ``agent`` (the ``llm_usage`` agent label of the call site) ·
    ``default_constant`` (name of the constant in THIS module, ``""`` when none) · ``storage``
    (DB table/row for editable/runtime, ``""`` for code-owned) · ``call_site`` (module:symbol).
    """

    key: str
    feature: str
    tier: Literal["code-owned", "user-editable", "runtime-generated"]
    version: str
    agent: str
    default_constant: str
    storage: str
    call_site: str


PROMPT_REGISTRY: list[PromptRegistryEntry] = [
    {
        "key": "ai_input",
        "feature": "AI 交易輸入解析（文字／截圖 → 交易草稿）",
        "tier": "code-owned",
        "version": AI_INPUT_PROMPT_VERSION,
        "agent": "ai_agents_input",
        "default_constant": "AI_INPUT_PROMPT_BODY",
        "storage": "",
        "call_site": "data_ingestion/agents.py:ai_agents_input",
    },
    {
        "key": "news_organizer",
        "feature": "新聞整理（文章正文 → 結構化摘要）",
        "tier": "user-editable",
        "version": NEWS_ORGANIZER_PROMPT_VERSION,
        "agent": "news_organize",
        "default_constant": "NEWS_ORGANIZER_PROMPT",
        "storage": "news_prompt_config (id=1)",
        "call_site": "news/organizer.py:organize",
    },
    {
        "key": "insight_system",
        "feature": "洞察卡系統提示詞（assemble 第 1 層；亦供 /api/prompts/test）",
        "tier": "user-editable",
        "version": SYSTEM_PROMPT_VERSION,
        "agent": "insight_generate, prompt_test",
        "default_constant": "SYSTEM_PROMPT_BODY",
        "storage": "system_prompt_config (id=1)",
        "call_site": "llm_insight/assemble.py:assemble_layers (system layer)",
    },
    {
        "key": "insight_strategy",
        "feature": "洞察策略提示詞（assemble 第 2 層：週報／健檢／市場…）",
        "tier": "user-editable",
        "version": "(per STRATEGY_TEMPLATES[].version)",
        "agent": "insight_generate",
        "default_constant": "STRATEGY_TEMPLATES",
        "storage": "strategy_prompts / insight_type_strategies",
        "call_site": "llm_insight/assemble.py:assemble_layers (template layers)",
    },
    {
        "key": "insight_calibration",
        "feature": "自我校正層（assemble 第 3 層，self_correct 開啟時；由大師生成）",
        "tier": "runtime-generated",
        "version": "(per calibration_prompts.version)",
        "agent": "master_calibrate → insight_generate",
        "default_constant": "",
        "storage": "calibration_prompts",
        "call_site": "llm_insight/assemble.py:assemble_layers (calibration layer)",
    },
    {
        "key": "insight_on_alert_note",
        "feature": "洞察卡預警附加守則（generate 對 on_alert 卡附加，強制 ≤3 交易日）",
        "tier": "code-owned",
        "version": ON_ALERT_NOTE_VERSION,
        "agent": "insight_generate",
        "default_constant": "ON_ALERT_NOTE",
        "storage": "",
        "call_site": "llm_insight/generate.py:run_insight_type",
    },
    {
        "key": "master_score",
        "feature": "大師敘事評分（到期洞察卡回測評分）",
        "tier": "code-owned",
        "version": MASTER_SCORE_PROMPT_VERSION,
        "agent": "master_score",
        "default_constant": "MASTER_SCORE_SYSTEM",
        "storage": "",
        "call_site": "llm_insight/master.py:score_narrative",
    },
    {
        "key": "master_calibrate",
        "feature": "大師校正規則生成（§4.8 安全鎖）",
        "tier": "code-owned",
        "version": MASTER_CALIBRATION_PROMPT_VERSION,
        "agent": "master_calibrate",
        "default_constant": "MASTER_CALIBRATION_SYSTEM",
        "storage": "",
        "call_site": "llm_insight/master.py:generate_calibration",
    },
    {
        "key": "master_validate",
        "feature": "大師校正規則安全審查",
        "tier": "code-owned",
        "version": MASTER_VALIDATE_PROMPT_VERSION,
        "agent": "master_validate",
        "default_constant": "MASTER_VALIDATE_SYSTEM",
        "storage": "",
        "call_site": "llm_insight/master.py:validate_calibration",
    },
    {
        "key": "digest_note",
        "feature": "每日摘要一句話（預設關閉；僅敘述已算好的數字）",
        "tier": "code-owned",
        "version": DIGEST_NOTE_PROMPT_VERSION,
        "agent": "digest_note",
        "default_constant": "DIGEST_NOTE_PROMPT_BODY",
        "storage": "",
        "call_site": "api/digest_service.py:_llm_note",
    },
    # R6-B (wave W-B, 2026-07-19) — UNIFIED AI instrument-resolve prompt (code-owned; DEFAULT
    # role). ONE prompt for every registration entry point: raw input + target market → local
    # exchange code + name + GICS sector (+ optional industry) in a single reply. Supersedes
    # the former ai_sector + ai_symbol_resolve entries. ``agent`` matches the llm_usage label
    # the call site logs under (``ai_instrument_resolve``).
    {
        "key": "ai_instrument_resolve",
        "feature": "AI 標的判讀（名稱／代號＋市場 → 當地代號＋名稱＋GICS 產業，查價覆核）",
        "tier": "code-owned",
        "version": AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
        "agent": "ai_instrument_resolve",
        "default_constant": "AI_INSTRUMENT_RESOLVE_PROMPT",
        "storage": "",
        "call_site": "api/routers/instruments.py:ai_resolve",
    },
]

