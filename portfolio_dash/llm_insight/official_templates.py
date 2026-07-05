"""The OFFICIAL prompt-template library (AI-input optimization program, 2026-07-05).

Single source of truth for the shipped best-default prompt content — the user-facing
first-touch experience. The settings UI reads it via ``GET /api/prompt-templates``,
"reset to official" restores :data:`SYSTEM_PROMPT_BODY`, and "add from library" copies
a strategy body the user then owns. Fresh installs seed the system prompt from here
(``system_prompt.DEFAULT_SYSTEM_PROMPT`` aliases it).

Content is Traditional Chinese by design (it is LLM- and user-facing); identifiers and
comments stay English per the bilingual protocol. Every template carries a version tag
so a future library update can offer "official has a newer version" upgrades. The
master-role and AI-parse prompts are code-owned (``master.py`` / ``agents.py``) and are
NOT user-editable, so they live outside this library.
"""

from typing import TypedDict

LIBRARY_VERSION = "official-v2 (2026-07-05)"

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

二、技術面 — 均線位置與乖離、30 日波動與回撤；價格序列為「近 30 個交易日逐日＋其餘每 5 日
取樣」：以近 30 日為主要判讀窗口，較早的取樣點僅作趨勢脈絡。
{{ma_signals_json}}
{{volatility_json}}
{{price_history_json}}

三、籌碼與基本面（僅台股有值；變數為空時整節跳過，不得虛構）— 法人買賣超與連買賣天數、
融資融券變化、月營收動能、估值位階（PER/PBR 歷史百分位）、近四季財報摘要；點出籌碼大戶
動向與基本面是否相互印證，並注意各資料的日期新舊。
{{institutional_json}}
{{margin_json}}
{{monthly_revenue_json}}
{{valuation_json}}
{{financials_json}}
（非台股標的：改以技術面、價格 vs 成本、環境對照為判讀支柱，並如實說明籌碼資料不適用。）

四、環境對照 — 相對所屬大盤的強弱與當前市場情緒，標注指標時點。
{{index_quotes_json}}
{{market_sentiment_json}}

五、方向性判讀與預測 — 綜合以上給出偏多／偏空／觀望之一，並附：
1) 條件式情境：明確的觸發條件與對應的重新評估方向，作為長期持倉加碼/減碼評估的參考框架
（不是買賣指令）；
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
    {"name": "個股健檢策略", "version": "v2.1", "scope": "per_symbol", "body": _CHECKUP_BODY},
    {"name": "市場週報策略", "version": "v1.1", "scope": "per_market", "body": _MARKET_BODY},
]


class TaskPreset(TypedDict):
    """One official-pack insight-task preset (a complete, schedulable task)."""

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
