"""Unit tests for llm_insight.variables — registry, token validation, value assembly.

The registry mirrors web/vars.js PLUS three backend-only date/time system tokens
(spec 04.10: now / card_created_at / eval_date), two batch-③ signal vars
(technical_signals_json / fear_greed_json), one batch-④ news var (symbol_news_json),
and one P1-batch-2 consensus var (consensus_json) — 33 total across 10 categories.
validate_tokens is the single reusable core (spec 04 R1 + spec 07 preflight):
preview lists diagnostics, the execution path turns them into 422s. Value assembly reads
only already-computed DashboardData / per-symbol detail / technicals / fed date context —
it computes no numbers of record.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.llm_insight import variables as V
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_registry_has_33_and_categories() -> None:
    # 26 vars.js mirror + 3 date/time (04.10) + 2 batch-③ signals + 1 batch-④ news
    # + 1 P1-batch-2 consensus (consensus_json) = 33.
    assert len(V.REGISTRY) == 33
    assert len({v.category for v in V.REGISTRY}) == 10  # + the 'consensus' category
    # tokens are unique
    assert len({v.token for v in V.REGISTRY}) == 33
    # BY_TOKEN index covers every spec
    assert set(V.BY_TOKEN) == {v.token for v in V.REGISTRY}


def test_category_counts_mirror_vars_js_plus_date_vars() -> None:
    counts: dict[str, int] = {}
    for v in V.REGISTRY:
        counts[v.category] = counts.get(v.category, 0) + 1
    assert counts == {
        # price gained technical_signals_json (4 → 5); sentiment gained
        # fear_greed_json (2 → 3) — batch ③; news gained symbol_news_json — batch ④;
        # consensus is the new P1-batch-2 category (consensus_json).
        "position": 6, "price": 5, "dividend": 3, "fx": 2,
        # system gained 3 date/time tokens (spec 04.10): 2 + 3 = 5.
        "chips": 5, "consensus": 1, "news": 1, "sentiment": 3, "ai": 2, "system": 5,
    }


def test_available_split_31_now_2_later() -> None:
    available = [v.token for v in V.REGISTRY if v.available]
    unavailable = [v.token for v in V.REGISTRY if not v.available]
    # 30 previously live + 1 P1-batch-2 consensus = 31; only the 2 'ai' vars stay deferred.
    assert len(available) == 31
    assert len(unavailable) == 2
    # only the 2 'ai' vars remain deferred (spec 04).
    assert {v.token for v in V.REGISTRY if v.category == "ai"} == set(unavailable)


def test_date_vars_registered_as_available_portfolio_system() -> None:
    for token in ("now", "card_created_at", "eval_date"):
        spec = V.BY_TOKEN[token]
        assert spec.category == "system"
        assert spec.scope == "portfolio"
        assert spec.available is True


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


def test_consensus_var_registered_per_symbol_available() -> None:
    spec = V.BY_TOKEN["consensus_json"]
    assert spec.category == "consensus"
    assert spec.scope == "per_symbol"
    assert spec.available is True
    assert "consensus_json" in V._EXTERNAL_TOKENS


def test_consensus_var_renders_fed_payload(golden_db: sqlite3.Connection) -> None:
    # The fed snapshot payload (as_of + targets + ratings) renders verbatim.
    import json as _json

    data = _data(golden_db)
    payload = {
        "as_of": "2026-07-09",
        "price_targets": {"current": "2465.0", "mean": "2819.8484"},
        "ratings": {"strong_buy": 9, "buy": 23, "hold": 1, "sell": 0,
                    "strong_sell": 0, "total": 33},
        "rating_score": "1.76",
        "upside_vs_mean_pct": "0.1440",
        "source": "yfinance",
    }
    ctx = V.VarContext(
        data=data,  # type: ignore[arg-type]
        symbol="2330",
        external_vars={"consensus_json": payload},
    )
    out, used = V.render_prompt("{{consensus_json}}", ctx)
    value = _json.loads(out)
    assert value["as_of"] == "2026-07-09"  # data age visible to the LLM
    assert value["rating_score"] == "1.76"
    assert value["upside_vs_mean_pct"] == "0.1440"
    assert "consensus_json" in used


def test_consensus_var_degrades_with_reason(golden_db: sqlite3.Connection) -> None:
    # No snapshot fed -> the var degrades, carrying the "no coverage" reason.
    import json as _json

    data = _data(golden_db)
    ctx = V.VarContext(
        data=data,  # type: ignore[arg-type]
        symbol="ZZZ",
        external_vars={"consensus_json": {"unavailable": True, "last_as_of": None}},
        external_reasons={"consensus_json": "無分析師覆蓋"},
    )
    out, _ = V.render_prompt("{{consensus_json}}", ctx)
    value = _json.loads(out)
    assert value["unavailable"] is True
    assert value["reason"] == "無分析師覆蓋"


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


# --- date/time vars (spec 04.10) ----------------------------------------------


def test_now_var_renders_iso_taipei(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    ctx = V.VarContext(data=data, now=_NOW)  # type: ignore[arg-type]
    out, used = V.render_prompt("現在：{{now}}", ctx)
    assert "now" in used
    assert "2026-06-11T14:30:00+08:00" in out


def test_now_var_converts_utc_to_taipei(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    utc_now = datetime(2026, 6, 11, 6, 30, tzinfo=ZoneInfo("UTC"))  # 14:30 Taipei
    ctx = V.VarContext(data=data, now=utc_now)  # type: ignore[arg-type]
    out, _ = V.render_prompt("{{now}}", ctx)
    assert "2026-06-11T14:30:00+08:00" in out


def test_eval_context_vars_render_when_fed(golden_db: sqlite3.Connection) -> None:
    data = _data(golden_db)
    created = datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    eval_d = datetime(2026, 6, 8, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    ctx = V.VarContext(data=data, now=_NOW, card_created_at=created, eval_date=eval_d)  # type: ignore[arg-type]
    out, used = V.render_prompt(
        "{{now}} {{card_created_at}} {{eval_date}}", ctx
    )
    assert "2026-06-11T14:30:00+08:00" in out
    assert "2026-06-01T09:00:00+08:00" in out
    assert "2026-06-08T09:00:00+08:00" in out
    assert set(used) == {"now", "card_created_at", "eval_date"}


def test_date_vars_degrade_gracefully_when_absent(golden_db: sqlite3.Connection) -> None:
    # No date context fed (generation path, not eval): card_created_at/eval_date null;
    # now also unavailable when not fed (the caller always feeds now for a real render).
    data = _data(golden_db)
    ctx = V.VarContext(data=data)  # type: ignore[arg-type]
    out, _ = V.render_prompt(
        "{{now}}|{{card_created_at}}|{{eval_date}}", ctx
    )
    assert out == "null|null|null"


# --- price-history data diet (2026-07-05 audit §3) -----------------------------


def test_downsample_short_series_untouched() -> None:
    from portfolio_dash.llm_insight.variables import _downsample_points

    pts = [{"date": f"d{i}", "close": str(i)} for i in range(30)]
    assert _downsample_points(pts) == pts


def test_downsample_keeps_daily_tail_and_sparse_head() -> None:
    from portfolio_dash.llm_insight.variables import _downsample_points

    pts = [{"date": f"d{i}", "close": str(i)} for i in range(180)]
    out = _downsample_points(pts)
    # 150 head points every 5th (aligned from the tail) + 30 daily tail = 60.
    assert len(out) == 30 + 30
    assert out[-30:] == pts[-30:]  # the timeliness-critical window is untouched
    dates = [p["date"] for p in out]
    assert dates == sorted(dates, key=lambda d: int(d[1:]))  # chronological order kept
    assert out[-31] == pts[149]  # sparse walk starts at the last head point (149,144,...)


# --- per_market slicing (2026-07-05 spec) ---------------------------------------


def _mctx(conn: sqlite3.Connection, market: str) -> V.VarContext:
    data = build_dashboard(conn, now=_NOW, reporting=Currency.TWD)
    return V.VarContext(data=data, now=_NOW, market=market)


def test_market_holdings_slice_only_own_market(golden_db: sqlite3.Connection) -> None:
    out, _ = V.render_prompt("{{holdings_json}}", _mctx(golden_db, "TW"))
    assert "2330" in out
    assert "AAPL" not in out  # the US holding never reaches a TW card


def test_market_allocation_reweights_within_market(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    tw = V.value_for("allocation_json", V.VarContext(data=data, now=_NOW, market="TW"))
    assert isinstance(tw, dict) and "unavailable" not in tw
    total = sum(Decimal(str(w)) for w in tw.values())
    assert Decimal("0.999") < total < Decimal("1.001")  # weights re-normalized in-market


def test_market_returns_only_own_currency(golden_db: sqlite3.Connection) -> None:
    out, _ = V.render_prompt("{{returns_by_ccy_json}}", _mctx(golden_db, "US"))
    assert "USD" in out and "TWD" not in out


def test_market_index_filtered_to_own_benchmark(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    ctx = V.VarContext(
        data=data, now=_NOW, market="TW",
        external_vars={"index_quotes_json": {
            "TAIEX": {"chg_20d": "+0.042"}, "SPX": {"chg_20d": "+0.031"},
            "KLCI": {"chg_20d": "+0.008"}, "last_as_of": "2026-06-11",
        }},
    )
    value = V.value_for("index_quotes_json", ctx)
    assert set(value) == {"TAIEX", "last_as_of"}


def test_per_market_scope_rejects_per_symbol_vars() -> None:
    v = V.validate_tokens("{{symbol_detail_json}} {{holdings_json}}", "per_market")
    assert v.scope_violations == ["symbol_detail_json"]
    assert v.unknown_tokens == []


def test_market_freshness_filters_to_own_symbols(golden_db: sqlite3.Connection) -> None:
    # SR gap-fill: the freshness slice was implemented but untested.
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    full = V.value_for("freshness_json", V.VarContext(data=data, now=_NOW))
    tw = V.value_for("freshness_json", V.VarContext(data=data, now=_NOW, market="TW"))
    tw_symbols = {h.symbol for h in data.holdings if h.market.value == "TW"}
    assert all(
        p.get("symbol") in tw_symbols for p in tw.get("prices", []) if isinstance(p, dict)
    )
    assert all(s in tw_symbols for s in tw.get("missing_prices", []))
    # the unfiltered dump is a superset
    assert len(full.get("prices", [])) >= len(tw.get("prices", []))


# --- batch ③ technical signals + fear_greed variable (2026-07-05) --------------


def test_technical_signals_var_from_closes(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    closes = [Decimal(str(100 + i)) for i in range(1, 80)]
    ctx = V.VarContext(data=data, now=_NOW, symbol="2330", closes=closes)
    val = V.value_for("technical_signals_json", ctx)
    assert isinstance(val, dict) and "rsi14" in val and "ma_cross" in val
    assert "volume" not in val  # probe-gated: no volumes fed
    # empty closes -> honest unavailable
    empty = V.value_for("technical_signals_json", V.VarContext(data=data, now=_NOW, symbol="X"))
    assert empty == {"unavailable": True}


def test_technical_signals_volume_probe_gate(golden_db: sqlite3.Connection) -> None:
    """ctx.volumes drives the volume section, and is gated on ≥1 non-None value (P1-④)."""
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    closes = [Decimal(str(100 + i)) for i in range(1, 80)]
    # all-None volumes (only older gap sessions) -> volume section stays absent
    none_vols: list[Decimal | None] = [None for _ in closes]
    ctx_none = V.VarContext(data=data, now=_NOW, symbol="2330", closes=closes,
                            volumes=none_vols)
    assert "volume" not in V.value_for("technical_signals_json", ctx_none)
    # real backfilled volumes aligned 1:1 with closes -> the volume section appears
    vols: list[Decimal | None] = [Decimal("1000000") for _ in closes]
    ctx_vol = V.VarContext(data=data, now=_NOW, symbol="2330", closes=closes, volumes=vols)
    val = V.value_for("technical_signals_json", ctx_vol)
    assert isinstance(val, dict) and "volume" in val
    assert set(val["volume"]) == {"ratio_to_avg", "surge"}


def test_fear_greed_var_is_external_fed(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    ctx = V.VarContext(
        data=data, now=_NOW,
        external_vars={"fear_greed_json": {
            "score": 62, "zone": "greed",
            "trend": {"direction": "rising", "change": "+8", "window_days": 7},
            "last_as_of": "2026-07-04",
        }},
    )
    val = V.value_for("fear_greed_json", ctx)
    assert val["zone"] == "greed" and val["trend"]["direction"] == "rising"
    # missing snapshot -> degrade shape
    missing = V.value_for("fear_greed_json", V.VarContext(data=data, now=_NOW))
    assert missing == {"unavailable": True}


def test_new_signal_vars_registered_scopes() -> None:
    assert V.BY_TOKEN["technical_signals_json"].scope == "per_symbol"
    assert V.BY_TOKEN["technical_signals_json"].category == "price"
    assert V.BY_TOKEN["fear_greed_json"].scope == "portfolio"
    assert V.BY_TOKEN["fear_greed_json"].category == "sentiment"


def test_per_market_kpis_and_fx_carry_scope_note(golden_db: sqlite3.Connection) -> None:
    # SR fix: whole-portfolio vars used in a per_market card are labelled, not fabricated.
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    ctx = V.VarContext(data=data, now=_NOW, market="TW",
                       fx_rates={"USD_TWD": {"rate": "32.9"}})
    kpis = V.value_for("kpis_json", ctx)
    assert "scope_note" in kpis and "全組合" in kpis["scope_note"]
    fx = V.value_for("fx_json", ctx)
    assert isinstance(fx, dict) and ("scope_note" in fx or fx.get("unavailable"))
    rates = V.value_for("fx_rates_json", ctx)
    assert "scope_note" in rates
    # without a market, no scope_note (portfolio card unchanged)
    p = V.value_for("kpis_json", V.VarContext(data=data, now=_NOW))
    assert "scope_note" not in p
