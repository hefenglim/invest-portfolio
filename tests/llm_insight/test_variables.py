"""Unit tests for llm_insight.variables — registry, token validation, value assembly.

The registry mirrors web/vars.js EXACTLY (26 vars / 8 categories). validate_tokens is
the single reusable core (spec 04 R1 + spec 07 preflight): preview lists diagnostics,
the execution path turns them into 422s. Value assembly reads only already-computed
DashboardData / per-symbol detail / technicals — it computes no numbers of record.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.llm_insight import variables as V
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_registry_has_26_and_categories() -> None:
    assert len(V.REGISTRY) == 26
    assert len({v.category for v in V.REGISTRY}) == 8
    # tokens are unique
    assert len({v.token for v in V.REGISTRY}) == 26
    # BY_TOKEN index covers every spec
    assert set(V.BY_TOKEN) == {v.token for v in V.REGISTRY}


def test_category_counts_mirror_vars_js() -> None:
    counts: dict[str, int] = {}
    for v in V.REGISTRY:
        counts[v.category] = counts.get(v.category, 0) + 1
    assert counts == {
        "position": 6, "price": 4, "dividend": 3, "fx": 2,
        "chips": 5, "sentiment": 2, "ai": 2, "system": 2,
    }


def test_available_split_24_now_2_later() -> None:
    available = [v.token for v in V.REGISTRY if v.available]
    unavailable = [v.token for v in V.REGISTRY if not v.available]
    # chips(5) + sentiment(2) went live (spec 20.2): 17 + 7 = 24 available.
    assert len(available) == 24
    assert len(unavailable) == 2
    # only the 2 'ai' vars remain deferred (spec 04).
    assert {v.token for v in V.REGISTRY if v.category == "ai"} == set(unavailable)


def test_scope_enum_is_english() -> None:
    scopes = {v.scope for v in V.REGISTRY}
    assert scopes == {"portfolio", "per_symbol"}
    assert V.BY_TOKEN["holdings_json"].scope == "portfolio"
    assert V.BY_TOKEN["symbol_detail_json"].scope == "per_symbol"


def test_tokens_in_dedup_first_seen() -> None:
    got = V.tokens_in("{{a_json}} text {{b_json}} {{a_json}}")
    assert got == ["a_json", "b_json"]


def test_validate_unknown_and_scope() -> None:
    r = V.validate_tokens(
        "{{holdings_json}} {{nope_json}} {{symbol_detail_json}}", "portfolio"
    )
    assert "nope_json" in r.unknown_tokens
    assert "symbol_detail_json" in r.scope_violations  # per_symbol var in portfolio body
    assert "holdings_json" in r.tokens_used
    # per_symbol body may use portfolio vars freely AND per_symbol vars
    r2 = V.validate_tokens("{{symbol_detail_json}} {{holdings_json}}", "per_symbol")
    assert r2.scope_violations == []
    assert r2.unknown_tokens == []


def test_validate_unknown_not_double_counted_as_scope() -> None:
    r = V.validate_tokens("{{ghost}}", "portfolio")
    assert r.unknown_tokens == ["ghost"]
    assert r.scope_violations == []


def _data(golden_db: sqlite3.Connection) -> object:
    return build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)


def test_render_real_values(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    out, used = V.render_prompt("持倉：{{holdings_json}}", V.VarContext(data=data))  # type: ignore[arg-type]
    assert "2330" in out and "holdings_json" in used and "{{holdings_json}}" not in out


def test_render_unknown_marker(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    out, used = V.render_prompt("{{nope_json}}", V.VarContext(data=data))  # type: ignore[arg-type]
    assert "nope_json" in out and "⚠" in out
    assert used == []  # unknown tokens are not "used" registry vars


def test_unavailable_var_renders_marker(golden_db: sqlite3.Connection) -> None:
    # backtest_json is available=false (spec 04) -> {"unavailable": true}
    assert any(v.token == "backtest_json" and not v.available for v in V.REGISTRY)
    data = _data(golden_db)
    out, _ = V.render_prompt("{{backtest_json}}", V.VarContext(data=data))  # type: ignore[arg-type]
    assert '"unavailable"' in out and "true" in out


def test_external_var_degrades_without_fed_value(golden_db: sqlite3.Connection) -> None:
    # institutional_json is now available but degrades when no external value is fed.
    assert any(v.token == "institutional_json" and v.available for v in V.REGISTRY)
    data = _data(golden_db)
    out, _ = V.render_prompt("{{institutional_json}}", V.VarContext(data=data))  # type: ignore[arg-type]
    assert '"unavailable"' in out and "true" in out


def test_required_tier_map() -> None:
    # The 5 FinMind chips vars require the "free" tier; sentiment/index need none.
    for token in (
        "institutional_json", "margin_json", "monthly_revenue_json", "valuation_json",
        "financials_json",
    ):
        assert V.required_tier(token) == "free", token
    assert V.required_tier("market_sentiment_json") is None
    assert V.required_tier("index_quotes_json") is None
    assert V.required_tier("holdings_json") is None


def test_tier_ok_helper() -> None:
    # No required tier -> always ok regardless of token tier.
    assert V.tier_ok(None, None) is True
    assert V.tier_ok(None, "free") is True
    # free required: an unset (None) token tier counts as free -> ok.
    assert V.tier_ok("free", None) is True
    assert V.tier_ok("free", "free") is True
    assert V.tier_ok("free", "backer") is True
    # backer required: free token is NOT ok; backer/higher is ok.
    assert V.tier_ok("backer", None) is False
    assert V.tier_ok("backer", "free") is False
    assert V.tier_ok("backer", "backer") is True
    assert V.tier_ok("backer", "sponsor") is True


def test_external_var_degrade_carries_reason(golden_db: sqlite3.Connection) -> None:
    # A reason fed via VarContext.external_reasons surfaces in the degrade payload.
    data = _data(golden_db)
    ctx = V.VarContext(data=data, external_reasons={"institutional_json": "需要 Backer 方案"})  # type: ignore[arg-type]
    out, _ = V.render_prompt("{{institutional_json}}", ctx)
    import json as _json

    value = _json.loads(out)
    assert value["unavailable"] is True
    assert value["reason"] == "需要 Backer 方案"


def test_value_for_decimals_are_strings(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    out, _ = V.render_prompt("{{kpis_json}}", V.VarContext(data=data))  # type: ignore[arg-type]
    # money serialized as JSON strings, never bare floats
    assert '"total_market_value"' in out
    # a Decimal market value renders quoted
    assert ': "' in out


def test_symbol_detail_assembly(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    ctx = V.VarContext(data=data, symbol="2330", closes=[Decimal("600")])  # type: ignore[arg-type]
    out, used = V.render_prompt("{{symbol_detail_json}}", ctx)
    assert "2330" in out and "symbol_detail_json" in used


def test_price_history_and_technicals_assembly(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    closes = [Decimal(str(x)) for x in range(580, 610)]  # 30 closes
    points = [{"date": "2026-06-09", "close": "600"}]
    ctx = V.VarContext(data=data, symbol="2330", closes=closes, price_points=points)  # type: ignore[arg-type]
    out_ph, _ = V.render_prompt("{{price_history_json}}", ctx)
    assert "points" in out_ph
    out_ma, _ = V.render_prompt("{{ma_signals_json}}", ctx)
    assert "ma20" in out_ma
    out_vol, _ = V.render_prompt("{{volatility_json}}", ctx)
    assert "vol_30d_annualized" in out_vol
    out_pvc, _ = V.render_prompt("{{price_vs_cost_json}}", ctx)
    assert "price_vs_original" in out_pvc
