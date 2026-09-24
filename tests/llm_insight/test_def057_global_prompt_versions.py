"""DEF-057 (functional test H-03, owner ruling 2026-09-24): the SYSTEM prompt and the NEWS-
ORGANIZER prompt keep every version, exactly as the strategy prompts do since DEF-033.

Measured on the demo (R3, developer decision ④): ``PUT /api/system-prompt``, ``POST
/api/system-prompt/reset`` and ``PUT /api/news-prompt`` all overwrote the single config row in
place — no history, no way back, and nothing recorded which system prompt an insight card or
which organizer prompt a news summary had been produced with.

This file pins the store half: the ``prompt_versions`` table (``shared/prompt_versions.py``),
the v1 back-fill of the bodies that existed before versioning, every write door of both stores
(儲存 / 還原官方 / 回復), the unchanged-body rule, the out-of-band repair, and what a generated
card and an organized news row record. The API half is
``tests/contract/test_def057_global_prompt_versions_api.py``; the page half
``tests/e2e/test_def057_global_prompt_versions_flow.py``.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import news_service
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate, official_templates
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import system_prompt as sp
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.news import organizer_prompt as npr
from portfolio_dash.news import pipeline as news_pipeline
from portfolio_dash.news import store as ns
from portfolio_dash.news.sources import NewsLink
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared import prompt_versions as pv
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
LATER = NOW + timedelta(hours=1)
OFFICIAL_SYS = official_templates.SYSTEM_PROMPT_BODY
OFFICIAL_NEWS = official_templates.NEWS_ORGANIZER_PROMPT


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _history(conn: sqlite3.Connection, kind: pv.Kind) -> list[tuple[int, str, str, int | None]]:
    return [(v.version, v.source, v.body, v.restored_from) for v in pv.list_versions(conn, kind)]


# --- the back-fill: the bodies that existed before versioning become v1 -------------------


def test_a_fresh_install_records_the_seeded_bodies_as_v1(conn: sqlite3.Connection) -> None:
    assert sp.get_system_prompt(conn)["current_version"] == 1
    assert npr.get_news_prompt(conn)["current_version"] == 1
    assert _history(conn, "system") == [(1, "migration", OFFICIAL_SYS, None)]
    assert _history(conn, "news") == [(1, "migration", OFFICIAL_NEWS, None)]
    # idempotent: every later read leaves the history alone
    sp.get_system_prompt(conn)
    npr.get_news_prompt(conn)
    assert len(pv.list_versions(conn, "system")) == 1
    assert len(pv.list_versions(conn, "news")) == 1


def _legacy(conn: sqlite3.Connection) -> None:
    """A database from before DEF-057: both configs seeded and CUSTOMISED, no history table."""
    sp.ensure_system_prompt_seeded(conn)
    npr.ensure_news_prompt_seeded(conn)
    conn.execute("DROP TABLE prompt_versions")
    conn.execute("UPDATE system_prompt_config SET body = '我的系統', updated_at = '2026-08-01'")
    conn.execute("UPDATE news_prompt_config SET body = '我的新聞', updated_at = '2026-08-02'")
    conn.commit()


def test_an_existing_customised_body_is_back_filled_as_v1_not_the_official_one(
    conn: sqlite3.Connection,
) -> None:
    _legacy(conn)
    assert sp.get_system_prompt(conn)["body"] == "我的系統"
    assert _history(conn, "system") == [(1, "migration", "我的系統", None)]
    assert pv.list_versions(conn, "system")[0].saved_at == "2026-08-01"
    assert npr.get_news_prompt(conn)["body"] == "我的新聞"
    assert _history(conn, "news") == [(1, "migration", "我的新聞", None)]


def test_a_body_written_around_the_store_is_recorded_as_the_next_version(
    conn: sqlite3.Connection,
) -> None:
    sp.get_system_prompt(conn)
    conn.execute("UPDATE system_prompt_config SET body = '直接改'")  # a direct SQL edit
    conn.commit()
    assert sp.get_system_prompt(conn)["current_version"] == 2
    assert _history(conn, "system")[0] == (2, "backfill", "直接改", None)


# --- every write door keeps a version ------------------------------------------------------


def test_every_system_prompt_door_keeps_a_version_with_its_own_source(
    conn: sqlite3.Connection,
) -> None:
    sp.set_system_prompt(conn, "第二版", now=NOW)
    sp.set_system_prompt(conn, "第二版", now=LATER)  # unchanged body → no version
    assert sp.reset_system_prompt(conn, now=LATER)["current_version"] == 3
    sp.reset_system_prompt(conn, now=LATER)  # already official → no version
    assert _history(conn, "system") == [
        (3, "reset_official", OFFICIAL_SYS, None),
        (2, "user_save", "第二版", None),
        (1, "migration", OFFICIAL_SYS, None),
    ]


def test_every_news_prompt_door_keeps_a_version_with_its_own_source(
    conn: sqlite3.Connection,
) -> None:
    w = npr.set_news_prompt(conn, "我的整理", now=NOW)
    assert w["current_version"] == 2 and w["is_official"] is False
    npr.set_news_prompt(conn, "我的整理", now=LATER)
    r = npr.reset_news_prompt(conn, now=LATER)
    assert r["current_version"] == 3 and r["is_official"] is True
    assert [(v, s) for v, s, _b, _r in _history(conn, "news")] == [
        (3, "reset_official"), (2, "user_save"), (1, "migration"),
    ]


def test_a_restore_is_a_new_version_and_never_rewrites_history(
    conn: sqlite3.Connection,
) -> None:
    sp.set_system_prompt(conn, "二", now=NOW)
    sp.set_system_prompt(conn, "三", now=NOW)
    v1 = pv.get_by_number(conn, "system", 1)
    assert v1 is not None
    out = sp.restore_system_prompt_version(conn, v1.id, now=LATER)
    assert out is not None
    wire, changed, restored_from = out
    assert changed is True and restored_from == 1
    assert wire["body"] == OFFICIAL_SYS and wire["current_version"] == 4
    assert sp.get_system_prompt(conn)["body"] == OFFICIAL_SYS
    assert _history(conn, "system") == [
        (4, "restore", OFFICIAL_SYS, 1), (3, "user_save", "三", None),
        (2, "user_save", "二", None), (1, "migration", OFFICIAL_SYS, None),
    ]
    # restoring what is already current writes nothing
    again = sp.restore_system_prompt_version(conn, v1.id, now=LATER)
    assert again is not None and again[1] is False
    assert len(pv.list_versions(conn, "system")) == 4


def test_a_version_of_the_other_prompt_is_never_restored_here(
    conn: sqlite3.Connection,
) -> None:
    sp.get_system_prompt(conn)
    npr.set_news_prompt(conn, "新聞二", now=NOW)
    news_v1 = pv.get_by_number(conn, "news", 1)
    sys_v1 = pv.get_by_number(conn, "system", 1)
    assert news_v1 is not None and sys_v1 is not None
    assert sp.restore_system_prompt_version(conn, news_v1.id, now=NOW) is None
    assert npr.restore_news_prompt_version(conn, sys_v1.id, now=NOW) is None
    assert sp.restore_system_prompt_version(conn, 99999, now=NOW) is None
    # the news restore itself works and is its own kind's version
    out = npr.restore_news_prompt_version(conn, news_v1.id, now=NOW)
    assert out is not None and out[1] is True
    assert npr.get_news_prompt(conn)["body"] == OFFICIAL_NEWS
    assert _history(conn, "news")[0] == (3, "restore", OFFICIAL_NEWS, 1)
    assert len(pv.list_versions(conn, "system")) == 1  # untouched


def test_the_strategy_history_is_left_exactly_as_it_was(conn: sqlite3.Connection) -> None:
    """The DEF-033 table is NOT migrated: its rows and ids survive byte-identical."""
    cs.ensure_seeded(conn)
    s = cs.create_strategy(conn, name="甲", body="一", now=NOW)
    cs.update_strategy(conn, s.id, name="甲", body="二", enabled=True, now=NOW)
    before = [tuple(r) for r in conn.execute("SELECT * FROM strategy_prompt_versions")]
    sp.set_system_prompt(conn, "系統二", now=NOW)
    npr.set_news_prompt(conn, "新聞二", now=NOW)
    after = [tuple(r) for r in conn.execute("SELECT * FROM strategy_prompt_versions")]
    assert after == before
    # one vocabulary: a source both histories share reads the same word
    assert cs.VERSION_SOURCE_LABELS["user_save"] == pv.SOURCE_LABELS["user_save"] == "儲存"
    assert cs.VERSION_SOURCE_LABELS["restore"] == pv.SOURCE_LABELS["restore"] == "回復"


# --- a generated card records the system-prompt version it was built with ------------------

_CARD_JSON = (
    '{"title":"洞察","summary":"s","body_md":"b","tags":[],"symbol":null,"confidence":60,'
    '"prediction":{"metric":"price_change","direction":"up","horizon_days":5}}'
)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = _Usage()


@pytest.fixture
def gconn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    yield golden_db


def _run(conn: sqlite3.Connection, it_id: int) -> None:
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    generate.run_insight_type(
        conn, it_id, var_contexts={None: V.VarContext(data=data, now=NOW)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )


def test_a_generated_card_records_the_system_prompt_version_it_was_built_from(
    gconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    sp.set_system_prompt(gconn, "系統守則 甲", now=NOW)  # v2 (v1 = the seeded body)
    a = cs.create_strategy(gconn, name="甲", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(gconn, name="T", scope="portfolio", now=NOW)
    cs.set_strategies(gconn, it.id, [(a.id, 0)])
    _run(gconn, it.id)
    first = istore.list_cards(gconn, insight_type_id=it.id)
    assert len(first) == 1
    assert first[0].system_prompt_ref is not None
    assert first[0].system_prompt_ref.model_dump() == {"used": True, "version": 2}
    # a save changes the prompt → the next card is new and records v3; the first keeps v2
    sp.set_system_prompt(gconn, "系統守則 乙", now=NOW)
    _run(gconn, it.id)
    cards = istore.list_cards(gconn, insight_type_id=it.id)
    assert [c.system_prompt_ref.version if c.system_prompt_ref else None for c in cards] == [
        3, 2,
    ]


def test_a_task_without_the_system_layer_records_that_none_was_used(
    gconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    a = cs.create_strategy(gconn, name="甲", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(gconn, name="T", scope="portfolio", use_system_prompt=False,
                                now=NOW)
    cs.set_strategies(gconn, it.id, [(a.id, 0)])
    _run(gconn, it.id)
    (card,) = istore.list_cards(gconn, insight_type_id=it.id)
    assert card.system_prompt_ref is not None
    assert card.system_prompt_ref.model_dump() == {"used": False, "version": None}


def test_a_card_from_before_the_record_reads_as_not_recorded(conn: sqlite3.Connection) -> None:
    from portfolio_dash.llm_insight.cards import InsightCard

    istore.ensure_tables(conn)
    rec = istore.add_card(
        conn, insight_type_id=1, card=InsightCard(title="t", summary="s", body_md="b"),
        fingerprint="f", calibration_version=None, horizon_days=5, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW,
    )
    assert rec.system_prompt_ref is None  # never a version guessed from today's history


# --- an organized news row records the organizer-prompt version it was produced with -------


@pytest.fixture
def lconn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    with ns.news_session() as nconn:
        nconn.execute("DELETE FROM news_mentions")
        nconn.execute("DELETE FROM organized_news")
    yield golden_db


def test_a_news_run_records_the_organizer_prompt_version_on_every_summary(
    lconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``run_news_for`` seam (clients + pipeline loop faked, organizer + store real):
    the version stored is the one of the body the organizer was handed."""
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(
        '{"title":"台積電法說","news_date":"2026-09-24","body_summary":"摘要",'
        '"related_stocks":["2330"]}'))
    npr.set_news_prompt(lconn, "我的整理規則 title news_date body_summary related_stocks",
                        now=NOW)  # v2

    def fake_pipeline(nconn: sqlite3.Connection, holdings: list[tuple[str, str]],
                      **kw: Any) -> dict[str, int | bool]:
        link = NewsLink(title="t", link="http://def057/" + str(len(holdings)), source="s",
                        date="2026-09-24", lang="zh")
        ns.upsert_news(nconn, kw["organize"](link, "正文"), discovered_for="2330")
        return {"stored": 1}

    monkeypatch.setattr(news_pipeline, "run_news_pipeline", fake_pipeline)
    news_service.run_news_for(lconn, [("2330", "TW")], now=NOW)
    with ns.news_session() as nconn:
        (row,) = ns.query_by_symbol(nconn, "2330", since_date="2026-01-01")
    assert row.prompt_version == 2 and row.body_summary == "摘要"
    # a later save → the next run records the new version; the stored row keeps its own
    npr.set_news_prompt(lconn, "第三版 title news_date body_summary related_stocks", now=NOW)
    news_service.run_news_for(lconn, [("2330", "TW"), ("2317", "TW")], now=NOW)
    with ns.news_session() as nconn:
        rows = ns.query_by_symbol(nconn, "2330", since_date="2026-01-01")
    assert sorted(r.prompt_version or 0 for r in rows) == [2, 3]


def test_a_headline_only_row_records_no_prompt_version() -> None:
    """The degrade row was produced by no prompt, so it names none (never a guess)."""
    from portfolio_dash.news.pipeline import _headline_only

    row = _headline_only(NewsLink(title="t", link="http://x"), now=NOW)
    assert row.prompt_version is None
