"""End-to-end generation through the api service seam (spec 04b).

``insight_service.run_for_id`` is the ONLY place that reads pricing/portfolio to feed the
pure ``generate.run_insight_type``. These tests drive it against the golden DB with the LLM
seam monkeypatched (no network), proving the conn-bearing inputs flow correctly into a
stored card.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

_CARD_JSON = (
    '{"title":"組合洞察","summary":"穩健","body_md":"整體穩健。","tags":["portfolio"],'
    '"symbol":null,"confidence":60,"prediction":null}'
)


class _Usage:
    prompt_tokens = 80
    completion_tokens = 15


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = _Usage()


@pytest.fixture
def conn(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    return golden_db


def test_portfolio_run_for_id_stores_card(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    result = insight_service.run_for_id(conn, it.id, now=NOW, reporting=Currency.TWD)
    assert result.status == "ok"
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert len(cards) == 1
    assert cards[0].card.title == "組合洞察"


def test_per_symbol_run_for_id_uses_holdings_for_mode_all(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol", universe={"mode": "all"}, now=NOW
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    result = insight_service.run_for_id(conn, it.id, now=NOW)
    assert result.status == "ok"
    # golden DB holds 2330 + AAPL -> one card per holding.
    symbols = {c.symbol for c in istore.list_cards(conn, insight_type_id=it.id)}
    assert symbols == {"2330", "AAPL"}


def test_all_registered_universe_is_opt_in_expansion(conn: sqlite3.Connection) -> None:
    # P2 batch 3 item ④: the default per_symbol universe (mode:all) stays HOLDINGS ONLY;
    # the opt-in mode:all_registered expands to holdings + watchlist (each watch symbol is
    # explicit LLM cost). Unit-level: resolve the two modes over the same registry.
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.portfolio.dashboard import build_dashboard
    from portfolio_dash.shared.enums import Market
    from portfolio_dash.shared.models.assets import Instrument

    upsert_instrument(conn, Instrument(
        symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="Microsoft",
    ))  # a registered, UNHELD watchlist symbol
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    it_all = cs.create_insight_type(
        conn, name="Holds", scope="per_symbol", universe={"mode": "all"}, now=NOW
    )
    it_reg = cs.create_insight_type(
        conn, name="HoldsWatch", scope="per_symbol",
        universe={"mode": "all_registered"}, now=NOW,
    )
    # default: holdings only — the watch symbol is NOT swept in.
    assert insight_service._resolve_universe(conn, it_all, data) == ["2330", "AAPL"]
    # opt-in: holdings + watchlist.
    assert insight_service._resolve_universe(conn, it_reg, data) == ["2330", "AAPL", "MSFT"]


def test_custom_universe_with_missing_symbol_gets_anomaly_card(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "NOPE"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    insight_service.run_for_id(conn, it.id, now=NOW)
    cards = {c.symbol: c for c in istore.list_cards(conn, insight_type_id=it.id)}
    # NOPE has no price in the golden DB -> deterministic anomaly card, zero cost.
    assert "NOPE" in cards
    assert cards["NOPE"].cost_usd == "0"


def test_unknown_universe_mode_falls_back_to_holdings(conn: sqlite3.Connection) -> None:
    # Deep review 2026-07-11 (batch-3 adequacy gap): a typo'd universe mode (here
    # "all_registred") is SILENTLY accepted at write time (composer_store stores the universe
    # as opaque JSON) and resolves to holdings-only — identical to the default / mode:all.
    # This DOCUMENTS the accept-behavior: a run-time reject would brick stored scheduled tasks
    # (see the stored-bad-cron lesson) — degrading to the safe default beats crashing a saved
    # task. The saved-task resolver and its draft-preflight twin must agree.
    from portfolio_dash.api.insight_service import _resolve_universe, _resolve_universe_raw
    from portfolio_dash.portfolio.dashboard import build_dashboard

    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    it = cs.create_insight_type(
        conn, name="Typo", scope="per_symbol",
        universe={"mode": "all_registred"}, now=NOW,  # deliberate typo, not a rejected write
    )
    # holdings-only (golden holds 2330 + AAPL) — the same result as the default / mode:all.
    assert _resolve_universe(conn, it, data) == ["2330", "AAPL"]
    # the draft-preflight twin resolves identically from the raw universe value.
    assert _resolve_universe_raw(conn, {"mode": "all_registred"}, data) == ["2330", "AAPL"]


def test_all_registered_missing_price_symbol_gets_anomaly_card(conn: sqlite3.Connection) -> None:
    # Deep review 2026-07-11 (batch-3 adequacy gap): with universe mode:all_registered, a
    # REGISTERED watchlist symbol carrying NO stored prices must flow to the R4 zero-LLM
    # anomaly card path — NOT an LLM call — while the priced holdings still complete in the
    # same batch. Pins the downstream honesty of the opt-in expansion (the existing
    # test_all_registered_universe_is_opt_in_expansion only checks universe resolution).
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.shared.enums import Market
    from portfolio_dash.shared.models.assets import Instrument

    upsert_instrument(conn, Instrument(
        symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="Microsoft",
    ))  # registered + UNHELD watchlist symbol with NO price rows
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "all_registered"}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    result = insight_service.run_for_id(conn, it.id, now=NOW)
    assert result.status == "ok"
    cards = {c.symbol: c for c in istore.list_cards(conn, insight_type_id=it.id)}
    # MSFT (no price) -> deterministic zero-LLM anomaly card, NOT an LLM call.
    assert "MSFT" in cards
    assert cards["MSFT"].cost_usd == "0"
    assert "資料異常" in cards["MSFT"].card.title
    # the priced holdings still complete their LLM cards in the same run.
    symbols = set(cards)
    assert "2330" in symbols and "AAPL" in symbols
