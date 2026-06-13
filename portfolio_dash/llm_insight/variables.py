"""Data-variable registry + reusable token validation + per-token value assembly (spec 06a).

The single source of truth for the prompt "Lego blocks". Three pieces:

1. **REGISTRY** — the 26 data variables across 8 categories, mirroring ``web/vars.js``
   exactly (token ids, categories, scope, description, sample). 17 are available now;
   the 9 external/AI-self variables (chips → spec 06b, sentiment → 06b, ai → spec 04)
   are registered ``available=False`` and render as ``{"unavailable": true}``.

2. **validate_tokens** — THE single validation core, reused by the preview endpoint
   (diagnostic, always 200), ``/prompts/test`` (execution path, 422), spec 04 R1
   runtime gating, and spec 07 preflight. A token absent from the registry is *unknown*;
   a ``per_symbol`` variable used in a ``portfolio``-scope body is a *scope violation*
   (= spec 04 R1). ``per_symbol`` bodies may use ``portfolio`` variables freely.

3. **Value assembly** — ``value_for`` / ``render_prompt`` turn each ``{{token}}`` into a
   JSON value drawn straight from the already-computed :class:`DashboardData` (+ per-symbol
   detail + ``portfolio.technicals``). Numbers are NEVER recomputed here beyond trivial
   shaping; Decimals stay Decimal until the Decimal-aware dumper serializes them to strings.
"""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from portfolio_dash.api.serialize import to_wire
from portfolio_dash.portfolio import technicals
from portfolio_dash.portfolio.dashboard_models import DashboardData

Scope = Literal["portfolio", "per_symbol"]

# Identical to web/vars.js: {{token}} with [a-z0-9_] ids, case-insensitive.
_TOKEN_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}", re.IGNORECASE)

# Marker left in rendered output for an unknown token (preview shows it; the execution
# path rejects via validate_tokens BEFORE rendering, so this never reaches the LLM).
_UNKNOWN_MARKER = "⚠unknown:{{%s}}"


@dataclass(frozen=True)
class VarSpec:
    """One data variable. Mirrors a row of ``web/vars.js`` (scope translated to English)."""

    token: str
    name: str
    category: str  # vars.js category id: position|price|dividend|fx|chips|sentiment|ai|system
    scope: Scope
    available: bool
    desc: str
    sample: str


# --- Registry (mirrors web/vars.js PD_VARS.CATEGORIES exactly) ----------------------
# scope mapping: 全組合 -> "portfolio", 單一標的 -> "per_symbol".
# available: position(6)+price(4)+dividend(3)+fx(2)+system(2) = 17 True now;
#            chips(5)+sentiment(2)+ai(2) = 9 False (06b / spec 04). vars.js marks the
#            'ai' category source:'ready', but those need spec-04 evaluations -> False.

REGISTRY: tuple[VarSpec, ...] = (
    # --- position (部位與績效) — all available ---
    VarSpec(
        "holdings_json", "持倉明細", "position", "portfolio", True,
        "全部持倉：股數、原始/調整均價、現價、市值、未實現損益、權重、回本率",
        '[{"symbol":"2330","shares":1000,"original_avg":"500.00","adjusted_avg":"495.00",'
        '"market_price":"612.50","unrealized_pnl":"117500","weight":"0.378",'
        '"payback_ratio":"0.010"}, …共 7 檔]',
    ),
    VarSpec(
        "allocation_json", "產業配置", "position", "portfolio", True,
        "各產業權重（報告幣別市值佔比）",
        '{"Semiconductors":"0.378","ETF":"0.305","Tech":"0.196","Financials":"0.088",'
        '"Banks":"0.033"}',
    ),
    VarSpec(
        "kpis_json", "組合 KPI", "position", "portfolio", True,
        "總市值、總報酬、XIRR、已實現/未實現、匯損益（報告幣別）",
        '{"total_market_value":"1618683","total_return":"308530","xirr":"0.1832",'
        '"realized_total":"34931","unrealized_total":"273599"}',
    ),
    VarSpec(
        "returns_by_ccy_json", "各幣別報酬", "position", "portfolio", True,
        "各幣別已實現/未實現/投入/報酬率（原幣，不可跨幣加總）",
        '{"TWD":{"total_return":"162850","rate":"0.1836"},'
        '"USD":{"total_return":"4498.50","rate":"0.2828"},'
        '"MYR":{"total_return":"688.30","rate":"0.0973"}}',
    ),
    VarSpec(
        "realized_json", "已實現損益明細", "position", "portfolio", True,
        "每筆賣出的淨收款、調整成本移除、已實現損益",
        '[{"symbol":"2330","shares_sold":200,"proceeds_net":"119350","realized":"21350",'
        '"quote_ccy":"TWD"}, …]',
    ),
    VarSpec(
        "symbol_detail_json", "單一標的全檔", "position", "per_symbol", True,
        "per_symbol 範圍專用：該標的部位、成本、配息史、交易事件、已實現記錄",
        '{"symbol":"2330","shares":1000,"adjusted_avg":"495.00","dividend_events":[…],'
        '"trade_events":[…]}',
    ),
    # --- price (價格與技術) — all available ---
    VarSpec(
        "price_history_json", "歷史日線", "price", "per_symbol", True,
        "近 180 個交易日收盤序列（含 staleness 標記）",
        '{"symbol":"2330","points":[{"date":"2026-06-11","close":"612.50"}, …180 點],'
        '"stale":false}',
    ),
    VarSpec(
        "ma_signals_json", "均線位置", "price", "per_symbol", True,
        "現價相對 20/60/120 日均線的位置與乖離率（由日線計算）",
        '{"ma20":"598.40","ma60":"571.20","price_vs_ma20":"+0.0236",'
        '"price_vs_ma60":"+0.0723"}',
    ),
    VarSpec(
        "volatility_json", "波動度", "price", "per_symbol", True,
        "30 日年化波動率與最大回撤",
        '{"vol_30d_annualized":"0.284","max_drawdown_90d":"-0.062"}',
    ),
    VarSpec(
        "price_vs_cost_json", "價格 vs 成本", "price", "per_symbol", True,
        "現價相對原始/調整均價的距離（決策核心比值）",
        '{"price_vs_original":"+0.2250","price_vs_adjusted":"+0.2374"}',
    ),
    # --- dividend (股利) — all available ---
    VarSpec(
        "dividends_json", "配息史", "dividend", "portfolio", True,
        "帳本全部股利記錄（type/gross/net/再投資，含幣別）",
        '[{"symbol":"0056","date":"2026-04-15","type":"cash","net":"8500","ccy":"TWD"}, …]',
    ),
    VarSpec(
        "ex_dividend_calendar_json", "除息日曆", "dividend", "portfolio", True,
        "未來已宣告除息事件（除息日/發放日/每股金額）",
        '[{"symbol":"2330","ex_date":"2026-06-20","cash_amount":"5.00",'
        '"currency":"TWD"}, …]',
    ),
    VarSpec(
        "dividend_projection_json", "年度股利預估", "dividend", "portfolio", True,
        "年內已宣告股利現金流預估（各幣別分列，稅後淨額）",
        '{"TWD":{"declared_net":"13500","events":2},'
        '"MYR":{"declared_net":"320.00","events":1}}',
    ),
    # --- fx (匯率) — all available ---
    VarSpec(
        "fx_json", "換匯損益", "fx", "portfolio", True,
        "各帳戶外幣池均價、現匯、已實現/未實現匯損益（股+現金拆分）",
        '{"schwab":{"avg_rate":"31.80","current_spot":"32.90",'
        '"unrealized_fx_stocks":"13552"}, …}',
    ),
    VarSpec(
        "fx_rates_json", "即期匯率", "fx", "portfolio", True,
        "報告幣別對各持倉幣別的最新匯率與取得時間",
        '{"USD_TWD":"32.90","MYR_TWD":"7.05","as_of":"2026-06-11T14:30:00+08:00"}',
    ),
    # --- chips (籌碼與基本面 / FinMind) — available=False until spec 06b ---
    VarSpec(
        "institutional_json", "法人買賣超", "chips", "per_symbol", False,
        "外資/投信/自營近 20 日買賣超與連買連賣天數（台股）",
        '{"symbol":"2330","foreign_net_20d":"+48200","consecutive_buy_days":6}',
    ),
    VarSpec(
        "margin_json", "融資融券", "chips", "per_symbol", False,
        "融資餘額/融券餘額近 20 日變化（台股）",
        '{"margin_balance_chg_20d":"-0.031","short_balance_chg_20d":"+0.012"}',
    ),
    VarSpec(
        "monthly_revenue_json", "月營收", "chips", "per_symbol", False,
        "近 12 個月營收與 YoY/MoM（台股）",
        '{"latest":{"month":"2026-05","yoy":"+0.31","mom":"+0.04"},"trailing_12m":[…]}',
    ),
    VarSpec(
        "valuation_json", "估值（PER/PBR）", "chips", "per_symbol", False,
        "本益比/股價淨值比與 5 年歷史百分位",
        '{"per":"24.1","per_5y_percentile":"0.78","pbr":"6.2"}',
    ),
    VarSpec(
        "financials_json", "季度財報摘要", "chips", "per_symbol", False,
        "近 4 季營收/毛利率/EPS（台股；美股 v2）",
        '{"quarters":[{"q":"2026Q1","revenue_yoy":"+0.28","gross_margin":"0.532",'
        '"eps":"14.2"}, …]}',
    ),
    # --- sentiment (市場情緒) — available=False until spec 06b ---
    VarSpec(
        "market_sentiment_json", "情緒指標", "sentiment", "portfolio", False,
        "VIX、Fear & Greed 指數與所處區間",
        '{"vix":"14.2","vix_zone":"low","fear_greed":62,"fear_greed_zone":"greed"}',
    ),
    VarSpec(
        "index_quotes_json", "大盤指數", "sentiment", "portfolio", False,
        "加權指數/S&P 500/KLCI 近 20 日漲跌",
        '{"TAIEX":{"chg_20d":"+0.042"},"SPX":{"chg_20d":"+0.031"},'
        '"KLCI":{"chg_20d":"+0.008"}}',
    ),
    # --- ai (AI 自身 / 校正用) — available=False until spec 04 ---
    VarSpec(
        "backtest_json", "回測命中分佈", "ai", "portfolio", False,
        "該洞察組合的歷史命中率信心分桶 — 校正提示詞錨定信心值用",
        '{"bins":[{"conf":"0.7-0.8","actual_rate":"0.66","n":6}],'
        '"overall_hit_rate":"0.625"}',
    ),
    VarSpec(
        "calibration_gap_json", "校準誤差", "ai", "portfolio", False,
        "該組合信心 vs 實際命中的 rolling 偏差",
        '{"gap":"+0.085","window_n":16}',
    ),
    # --- system (系統狀態) — all available ---
    VarSpec(
        "freshness_json", "資料新鮮度", "system", "portfolio", True,
        "缺價/過期標的清單 — 讓 AI 知道哪些數字不可信",
        '{"missing_prices":["00919"],"stale":[{"symbol":"MSFT","as_of":"2026-06-06"}]}',
    ),
    VarSpec(
        "as_of", "資料時間", "system", "portfolio", True,
        "本次快照的資料時間戳",
        '"2026-06-11T14:30:00+08:00"',
    ),
)

BY_TOKEN: dict[str, VarSpec] = {v.token: v for v in REGISTRY}


# --- Token extraction + validation (the single reusable core) -----------------------


def tokens_in(body: str) -> list[str]:
    """Extract ``{{token}}`` ids from *body*, de-duplicated, in first-seen order."""
    out: list[str] = []
    for match in _TOKEN_RE.finditer(body):
        token = match.group(1)
        if token not in out:
            out.append(token)
    return out


@dataclass(frozen=True)
class TokenValidation:
    """Result of :func:`validate_tokens`.

    ``unknown_tokens``: ids not in the registry. ``scope_violations``: registry
    variables with ``scope == 'per_symbol'`` used in a ``portfolio``-scope body (the two
    sets are disjoint — an unknown token is never also a scope violation).
    """

    tokens_used: list[str]
    unknown_tokens: list[str]
    scope_violations: list[str]


def validate_tokens(body: str, scope: str) -> TokenValidation:
    """Validate a prompt body's ``{{token}}`` references against the registry + scope.

    THE single validation core (reused by spec 06 preview/test, spec 04 R1 runtime
    gating, and spec 07 preflight):

    * a token absent from the registry → ``unknown_tokens``;
    * a registry variable whose ``scope == 'per_symbol'`` used when the body's *scope*
      is ``'portfolio'`` → ``scope_violations`` (= spec 04 R1).

    ``per_symbol`` bodies may use ``portfolio`` variables freely (no violation). The
    preview path lists these as diagnostics (always 200); the execution path turns any
    non-empty list into a 422.
    """
    used = tokens_in(body)
    unknown: list[str] = []
    violations: list[str] = []
    for token in used:
        spec = BY_TOKEN.get(token)
        if spec is None:
            unknown.append(token)
            continue
        if scope == "portfolio" and spec.scope == "per_symbol":
            violations.append(token)
    return TokenValidation(tokens_used=used, unknown_tokens=unknown, scope_violations=violations)


# --- Value assembly -----------------------------------------------------------------


@dataclass
class VarContext:
    """Inputs for value assembly. ``data`` carries every portfolio-scope value; the
    per-symbol fields are populated only for a ``per_symbol`` render target."""

    data: DashboardData
    symbol: str | None = None
    closes: list[Decimal] | None = None  # chronological closes (pricing.store.get_price_history)
    price_points: list[dict[str, Any]] = field(default_factory=list)  # [{date, close}]
    # Router-fed (the api layer reads conn; this layer must not import pricing/data_ingestion):
    fx_rates: dict[str, Any] | None = None       # {"USD_TWD": {"rate", "as_of", "stale"}}
    dividend_rows: list[dict[str, Any]] | None = None  # per-event ledger rows incl. ccy


_UNAVAILABLE: dict[str, Any] = {"unavailable": True}


def _holdings_for_symbol(data: DashboardData, symbol: str) -> list[Any]:
    return [h for h in data.holdings if h.symbol == symbol]


def _symbol_detail(ctx: VarContext) -> dict[str, Any]:
    """Assemble the per-symbol full record from the computed dashboard (no router call).

    Mirrors the spec-01 symbol-detail shape but is built inline from ``DashboardData``
    (position + dividends + realized rows filtered to the symbol) so this layer adds no
    dependency on the API router and recomputes nothing.
    """
    symbol = ctx.symbol
    if symbol is None:
        return {"unavailable": True}
    rows = _holdings_for_symbol(ctx.data, symbol)
    if not rows:
        return {"symbol": symbol, "unavailable": True}
    # Q1: the account holding the most shares (matches spec-01 cost_basis selection).
    primary = max(rows, key=lambda h: h.shares)
    realized_rows = [r for r in ctx.data.realized.rows if r.symbol == symbol]
    return {
        "symbol": symbol,
        "shares": primary.shares,
        "original_avg": primary.original_avg,
        "adjusted_avg": primary.adjusted_avg,
        "market_price": primary.market_price,
        "market_value": primary.market_value,
        "unrealized_pnl": primary.unrealized_pnl,
        "payback_ratio": primary.payback_ratio,
        "quote_ccy": primary.quote_ccy,
        "positions": [h.model_dump() for h in rows],
        "realized_rows": [r.model_dump() for r in realized_rows],
    }


def _price_history(ctx: VarContext) -> dict[str, Any]:
    if not ctx.price_points:
        return {"symbol": ctx.symbol, "points": [], "unavailable": True}
    # staleness for this symbol from the freshness report (latest-quote concern).
    stale = False
    for pf in ctx.data.freshness.prices:
        if pf.symbol == ctx.symbol:
            stale = pf.stale
            break
    return {"symbol": ctx.symbol, "points": ctx.price_points, "stale": stale}


def _ma_signals(ctx: VarContext) -> dict[str, Any]:
    if not ctx.closes:
        return {"unavailable": True}
    return technicals.ma_signals(ctx.closes)


def _volatility(ctx: VarContext) -> dict[str, Any]:
    if not ctx.closes:
        return {"unavailable": True}
    return {
        "vol_30d_annualized": technicals.annualized_volatility(ctx.closes, window=30),
        "max_drawdown_90d": technicals.max_drawdown(ctx.closes, window=90),
    }


def _price_vs_cost(ctx: VarContext) -> dict[str, Any]:
    if ctx.symbol is None:
        return {"unavailable": True}
    rows = _holdings_for_symbol(ctx.data, ctx.symbol)
    if not rows:
        return {"unavailable": True}
    primary = max(rows, key=lambda h: h.shares)
    if primary.market_price is None:
        return {"unavailable": True}
    # Per-ratio: a non-positive cost yields None for that ratio only (domain-ledger allows
    # adjusted_avg <= 0); the valid ratio is still surfaced.
    return technicals.price_vs_cost(
        primary.market_price, primary.original_avg, primary.adjusted_avg
    )


def _allocation(data: DashboardData) -> dict[str, Any]:
    if data.allocation is None:
        return {"unavailable": True}
    return dict(data.allocation.weights)


def _returns_by_ccy(data: DashboardData) -> dict[str, Any]:
    if data.returns is None:
        return {"unavailable": True}
    return {
        ccy.value: {
            "realized": cr.realized,
            "unrealized": cr.unrealized,
            "total_return": cr.total_return,
            "gross_invested": cr.gross_invested,
            "rate": cr.rate,
        }
        for ccy, cr in data.returns.by_currency.items()
    }


def _fx(data: DashboardData) -> dict[str, Any]:
    if data.fx is None:
        return {"unavailable": True}
    return {acct_id: r.model_dump() for acct_id, r in data.fx.by_account.items()}


def _fx_rates(ctx: VarContext) -> dict[str, Any]:
    """Spot rates per reporting-currency pair. The actual rate is resolved by the router
    (conn-bearing) and fed via ``ctx.fx_rates`` — ``data.freshness.fx`` carries only
    as_of/stale, NOT the rate. Unavailable when the router did not supply rates."""
    if not ctx.fx_rates:
        return {"unavailable": True}
    out: dict[str, Any] = dict(ctx.fx_rates)
    out["as_of"] = ctx.data.as_of
    return out


def _dividends(ctx: VarContext) -> Any:
    """Per-event dividend ledger rows (symbol/date/type/gross/net/ccy), fed by the router.
    Falls back to the computed yearly summary when no rows are supplied (conn-less callers)."""
    if ctx.dividend_rows is not None:
        return ctx.dividend_rows
    return ctx.data.dividends.model_dump()


def _dividend_projection(data: DashboardData) -> dict[str, Any]:
    if data.dividend_projection is None:
        return {"unavailable": True}
    return {
        ccy.value: dpc.model_dump()
        for ccy, dpc in data.dividend_projection.by_currency.items()
    }


def value_for(token: str, ctx: VarContext) -> Any:
    """Return the JSON-able value for *token* from already-computed inputs.

    ``available=False`` variables and any variable whose backing data is missing return
    ``{"unavailable": true}``. Decimals are returned as :class:`Decimal` (the dumper in
    :func:`render_prompt` converts them to strings); no number is recomputed here.
    """
    spec = BY_TOKEN.get(token)
    if spec is None or not spec.available:
        return _UNAVAILABLE
    data = ctx.data
    if token == "holdings_json":
        return [h.model_dump() for h in data.holdings]
    if token == "allocation_json":
        return _allocation(data)
    if token == "kpis_json":
        return data.kpis.model_dump()
    if token == "returns_by_ccy_json":
        return _returns_by_ccy(data)
    if token == "realized_json":
        return [r.model_dump() for r in data.realized.rows]
    if token == "symbol_detail_json":
        return _symbol_detail(ctx)
    if token == "price_history_json":
        return _price_history(ctx)
    if token == "ma_signals_json":
        return _ma_signals(ctx)
    if token == "volatility_json":
        return _volatility(ctx)
    if token == "price_vs_cost_json":
        return _price_vs_cost(ctx)
    if token == "dividends_json":
        return _dividends(ctx)
    if token == "ex_dividend_calendar_json":
        return [e.model_dump() for e in data.ex_dividend_calendar]
    if token == "dividend_projection_json":
        return _dividend_projection(data)
    if token == "fx_json":
        return _fx(data)
    if token == "fx_rates_json":
        return _fx_rates(ctx)
    if token == "freshness_json":
        return data.freshness.model_dump()
    if token == "as_of":
        return data.as_of
    # Defensive: a registered-available token with no mapping (should not happen).
    return _UNAVAILABLE


def _dumps(value: Any) -> str:
    """JSON-dump a value tree with the API's Decimal/date/enum-aware wire encoder."""
    return json.dumps(to_wire(value), ensure_ascii=False)


def render_prompt(body: str, ctx: VarContext) -> tuple[str, list[str]]:
    """Replace every ``{{token}}`` with its JSON value; return ``(rendered, tokens_used)``.

    Each value is dumped via the Decimal-aware encoder (money/price/rate → strings).
    Unknown tokens are left as a visible ``⚠unknown:{{token}}`` marker — the preview path
    shows them, while the execution path rejects them via :func:`validate_tokens` BEFORE
    calling this. ``tokens_used`` lists only the registry tokens that were rendered.
    """
    used: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in BY_TOKEN:
            return _UNKNOWN_MARKER % token
        if token not in used:
            used.append(token)
        return _dumps(value_for(token, ctx))

    rendered = _TOKEN_RE.sub(_replace, body)
    return rendered, used
