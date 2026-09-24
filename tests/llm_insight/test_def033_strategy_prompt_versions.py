"""DEF-033 (functional test H-03, owner ruling 2026-09-24): strategy prompts keep EVERY version.

Measured on the demo (R1): 設定 › AI 提示詞 › any strategy card → edit → 儲存 (PUT
``/api/strategy-prompts/{id}`` 200) overwrote the body in place. The card offered 儲存／預覽
提示詞／測試送出／同步官方／封存 and nothing else — no history, no way back, and 同步官方's own
confirm text admitted 「你的自訂修改將遺失」. Nothing recorded which body an insight card had
been generated from either.

Ruling: every save keeps a version; the owner can read the diff and restore ANY version; a
card records the version it was generated with. This file pins the store half (the
``strategy_prompt_versions`` table, the backfill, the restore, and the card's record); the
API half is ``tests/contract/test_def033_strategy_prompt_versions_api.py`` and the page half
``tests/e2e/test_def033_prompt_versions_flow.py``.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate, prompt_diff
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.portfolio.dashboard import build_dashboard
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

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    cs.ensure_seeded(c)
    yield c
    c.close()


def _history(conn: sqlite3.Connection, sid: int) -> list[tuple[int, str, str, int | None]]:
    return [(v.version, v.source, v.body, v.restored_from) for v in cs.list_versions(conn, sid)]


# --- every save keeps a version ---------------------------------------------------------


def test_a_new_strategy_is_version_one(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="A", now=NOW, source="create")
    assert sp.current_version == 1
    assert _history(conn, sp.id) == [(1, "create", "A", None)]


def test_a_save_that_changes_the_body_appends_a_version_and_keeps_the_old_one(
    conn: sqlite3.Connection,
) -> None:
    sp = cs.create_strategy(conn, name="S", body="A", now=NOW)
    after = cs.update_strategy(conn, sp.id, name="S", body="B", enabled=True, now=LATER,
                               source="user_save")
    assert after is not None and after.body == "B" and after.current_version == 2
    # newest first; the body the save overwrote is still there, unchanged
    assert _history(conn, sp.id) == [(2, "user_save", "B", None), (1, "create", "A", None)]
    assert cs.get_version_by_number(conn, sp.id, 2).saved_at == LATER.isoformat()  # type: ignore[union-attr]


def test_a_save_that_keeps_the_body_adds_no_version(conn: sqlite3.Connection) -> None:
    """The enable toggle PUTs the stored body; a rename keeps it. Neither is a new prompt."""
    sp = cs.create_strategy(conn, name="S", body="A", now=NOW)
    cs.update_strategy(conn, sp.id, name="S", body="A", enabled=False, now=LATER)
    cs.update_strategy(conn, sp.id, name="S2", body="A", enabled=True, now=LATER)
    assert [v.version for v in cs.list_versions(conn, sp.id)] == [1]


# --- restore ANY version, as a new version ---------------------------------------------


def test_restore_is_a_new_version_and_never_rewrites_history(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="A\nB", now=NOW)
    cs.update_strategy(conn, sp.id, name="S", body="A\nC", enabled=True, now=NOW)
    cs.update_strategy(conn, sp.id, name="S", body="D", enabled=True, now=NOW)
    before = [v.model_dump() for v in cs.list_versions(conn, sp.id)]
    v1 = cs.get_version_by_number(conn, sp.id, 1)
    assert v1 is not None
    out = cs.restore_version(conn, v1.id, now=LATER)
    assert out is not None
    restored, changed = out
    assert changed is True and restored.body == "A\nB" and restored.current_version == 4
    hist = [v.model_dump() for v in cs.list_versions(conn, sp.id)]
    assert hist[1:] == before  # v1..v3 byte-identical
    assert (hist[0]["version"], hist[0]["source"], hist[0]["body"], hist[0]["restored_from"]) \
        == (4, "restore", "A\nB", 1)
    # and the restore itself can be undone the same way (any version, not just the last)
    v3 = cs.get_version_by_number(conn, sp.id, 3)
    assert v3 is not None
    again = cs.restore_version(conn, v3.id, now=LATER)
    assert again is not None and again[0].body == "D" and again[0].current_version == 5


def test_restoring_the_current_body_changes_nothing(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="A", now=NOW)
    v1 = cs.latest_version(conn, sp.id)
    assert v1 is not None
    out = cs.restore_version(conn, v1.id, now=LATER)
    assert out is not None and out[1] is False
    assert len(cs.list_versions(conn, sp.id)) == 1


def test_restore_keeps_the_current_name(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="舊名", body="A", now=NOW)
    cs.update_strategy(conn, sp.id, name="新名", body="B", enabled=True, now=NOW)
    v1 = cs.get_version_by_number(conn, sp.id, 1)
    assert v1 is not None
    out = cs.restore_version(conn, v1.id, now=LATER)
    assert out is not None and out[0].name == "新名" and out[0].body == "A"


def test_an_archived_strategy_cannot_be_restored(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="S", body="A", now=NOW)
    cs.update_strategy(conn, sp.id, name="S", body="B", enabled=True, now=NOW)
    cs.archive_strategy(conn, sp.id, now=NOW)
    v1 = cs.get_version_by_number(conn, sp.id, 1)
    assert v1 is not None
    with pytest.raises(cs.RestoreRefusedError):
        cs.restore_version(conn, v1.id, now=LATER)
    assert cs.get_strategy(conn, sp.id).body == "B"  # type: ignore[union-attr]


def test_an_unknown_version_is_none(conn: sqlite3.Connection) -> None:
    assert cs.restore_version(conn, 999, now=NOW) is None


# --- backfill: every existing prompt becomes version 1 ----------------------------------


def _legacy(conn: sqlite3.Connection) -> None:
    """A database from before this table: strategies exist, no version rows."""
    conn.execute(
        "CREATE TABLE strategy_prompts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT "
        "NULL, body TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT "
        "NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO strategy_prompts (name, body, created_at, updated_at) "
        "VALUES ('持倉健診', 'X', '2026-07-01T00:00:00+08:00', '2026-09-01T09:00:00+08:00')"
    )
    conn.execute(
        "INSERT INTO strategy_prompts (name, body, archived, created_at, updated_at) "
        "VALUES ('舊策略', 'Y', 1, '2026-07-01T00:00:00+08:00', '2026-07-02T00:00:00+08:00')"
    )
    conn.commit()


def test_migration_makes_every_existing_body_version_one() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _legacy(c)
    cs.ensure_seeded(c)
    assert _history(c, 1) == [(1, "migration", "X", None)]
    assert _history(c, 2) == [(1, "migration", "Y", None)]  # archived ones keep history too
    v = cs.latest_version(c, 1)
    assert v is not None and v.saved_at == "2026-09-01T09:00:00+08:00"  # the body's own date
    cs.ensure_seeded(c)  # idempotent: runs on every request
    cs.ensure_seeded(c)
    assert [x.version for x in cs.list_versions(c, 1)] == [1]


def test_a_body_written_around_the_store_is_recorded_as_the_next_version() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _legacy(c)
    cs.ensure_seeded(c)
    c.execute("UPDATE strategy_prompts SET body = 'X2' WHERE id = 1")  # a direct SQL edit
    c.commit()
    cs.ensure_seeded(c)
    assert _history(c, 1) == [(2, "backfill", "X2", None), (1, "migration", "X", None)]
    cs.ensure_seeded(c)
    assert len(cs.list_versions(c, 1)) == 2


# --- the diff -----------------------------------------------------------------------


def test_line_diff_reads_older_to_newer() -> None:
    d = prompt_diff.line_diff("a\nb\nc", "a\nB\nc\nd")
    assert [(ln.op, ln.text) for ln in d.lines] == [
        ("same", "a"), ("del", "b"), ("add", "B"), ("same", "c"), ("add", "d"),
    ]
    assert (d.added, d.removed, d.identical) == (2, 1, False)
    assert d.lines[1].old_no == 2 and d.lines[1].new_no is None
    assert d.lines[4].new_no == 4 and d.lines[4].old_no is None
    assert prompt_diff.line_diff("x\ny", "x\ny").identical is True


# --- the card records the version it was generated with -------------------------------

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


def test_a_generated_card_records_the_strategy_version_it_was_built_from(
    gconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    a = cs.create_strategy(gconn, name="甲", body="觀察 {{kpis_json}}", now=NOW)
    b = cs.create_strategy(gconn, name="乙", body="補充", now=NOW)
    cs.update_strategy(gconn, b.id, name="乙", body="補充 v2", enabled=True, now=NOW)
    it = cs.create_insight_type(gconn, name="T", scope="portfolio", now=NOW)
    cs.set_strategies(gconn, it.id, [(a.id, 0), (b.id, 1)])
    _run(gconn, it.id)
    first = istore.list_cards(gconn, insight_type_id=it.id)
    assert len(first) == 1
    assert [r.model_dump() for r in first[0].strategy_versions or []] == [
        {"strategy_id": a.id, "name": "甲", "version": 1},
        {"strategy_id": b.id, "name": "乙", "version": 2},
    ]
    # a save changes the prompt → the next card is a new card recording the new version,
    # and the first card keeps the version it was actually generated with
    cs.update_strategy(gconn, a.id, name="甲", body="觀察 {{kpis_json}} 更新", enabled=True,
                       now=NOW)
    _run(gconn, it.id)
    cards = istore.list_cards(gconn, insight_type_id=it.id)
    assert len(cards) == 2
    assert [r.version for r in cards[0].strategy_versions or []] == [2, 2]
    assert [r.version for r in cards[1].strategy_versions or []] == [1, 2]


def test_a_card_from_before_the_record_reads_as_not_recorded(conn: sqlite3.Connection) -> None:
    istore.ensure_tables(conn)
    rec = istore.add_card(
        conn, insight_type_id=1, card=InsightCard(title="t", summary="s", body_md="b"),
        fingerprint="f", calibration_version=None, horizon_days=5, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW,
    )
    assert rec.strategy_versions is None  # never a version guessed from today's history
