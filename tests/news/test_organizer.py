"""Unit tests for the news organizer (LLM seam monkeypatched; no network)."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.news import organizer as org
from portfolio_dash.news.organizer_prompt import get_news_prompt
from portfolio_dash.news.sources import NewsLink
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class _Usage:
    prompt_tokens = 500
    completion_tokens = 60


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = _Usage()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    upsert_model(c, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2")))
    set_role(c, LLMRole.DEFAULT, "m")
    add_topup(c, Decimal("100"))
    yield c
    c.close()


def _patch(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(content))


def test_organize_builds_row_from_llm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, '{"title":"台積電法說","news_date":"2026-07-05",'
                        '"body_summary":"台積電將於 7/16 法說。","related_stocks":["2330","2454"]}')
    link = NewsLink(title="原標題", link="http://a", source="CM", date="2026-07-04", lang="zh")
    out = org.organize(link, "文章正文…", get_news_prompt(conn)["body"], conn=conn, now=NOW)
    assert out.title == "台積電法說" and out.news_date == "2026-07-05"
    assert out.related_stocks == ["2330", "2454"] and out.source == "CM" and out.lang == "zh"
    assert out.body_summary.startswith("台積電")


def test_organize_falls_back_on_blank_fields(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # model returns a blank title + invalid date -> use the discovery link's fallbacks.
    _patch(monkeypatch, '{"title":"","news_date":"n/a","body_summary":"摘要","related_stocks":[]}')
    link = NewsLink(title="探索標題", link="http://b", source="src", date="2026-07-03", lang="en")
    out = org.organize(link, "text", get_news_prompt(conn)["body"], conn=conn, now=NOW)
    assert out.title == "探索標題"       # fell back to link title
    assert out.news_date == "2026-07-03"  # fell back to link date
    assert out.related_stocks == []


def test_organize_uses_now_when_no_date_anywhere(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, '{"title":"T","news_date":"","body_summary":"s","related_stocks":[]}')
    link = NewsLink(title="T", link="http://c")  # no date on the link either
    out = org.organize(link, "text", get_news_prompt(conn)["body"], conn=conn, now=NOW)
    assert out.news_date == "2026-07-06"  # today


def test_news_prompt_default_is_official(conn: sqlite3.Connection) -> None:
    from portfolio_dash.llm_insight import official_templates as ot
    assert get_news_prompt(conn)["body"] == ot.NEWS_ORGANIZER_PROMPT
