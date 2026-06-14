"""Unit tests for layer assembly (spec 04.0): system + strategies + active calibration.

Hard order (spec 4.0): system prompt (if use_system_prompt) + strategy1..n (ordered,
enabled only) + active calibration (if self_correct AND an active non-archived version
exists). Each layer is rendered via the 06a render_prompt over a fed VarContext, so this
layer recomputes no number and never reads pricing/data_ingestion (architecture.md).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import assemble
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded, set_system_prompt
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    ensure_system_prompt_seeded(golden_db)
    cs.ensure_seeded(golden_db)
    yield golden_db


def _ctx(conn: sqlite3.Connection) -> V.VarContext:
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    return V.VarContext(data=data, now=NOW)


def test_system_plus_strategies_in_order(conn: sqlite3.Connection) -> None:
    set_system_prompt(conn, "SYSTEM-RULES", now=NOW)
    a = cs.create_strategy(conn, name="A", body="strat-A {{kpis_json}}", now=NOW)
    b = cs.create_strategy(conn, name="B", body="strat-B", now=NOW)
    it = cs.create_insight_type(conn, name="Combo", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(b.id, 0), (a.id, 1)])  # B first, then A

    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    kinds = [lyr.kind for lyr in result.layers]
    names = [lyr.name for lyr in result.layers]
    assert kinds == ["system", "template", "template"]
    assert names[1:] == ["B", "A"]  # ordered by position
    # system layer rendered first; strategy A's variable was rendered (not raw token).
    assert "SYSTEM-RULES" in result.layers[0].rendered
    assert "{{kpis_json}}" not in result.layers[2].rendered
    assert "strat-A" in result.layers[2].rendered


def test_system_skipped_when_use_system_prompt_false(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    it = cs.create_insight_type(
        conn, name="NoSys", scope="portfolio", use_system_prompt=False, now=NOW
    )
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.kind for lyr in result.layers] == ["template"]


def test_disabled_and_archived_strategies_skipped(conn: sqlite3.Connection) -> None:
    enabled = cs.create_strategy(conn, name="On", body="on", now=NOW)
    disabled = cs.create_strategy(conn, name="Off", body="off", now=NOW)
    cs.update_strategy(conn, disabled.id, name="Off", body="off", enabled=False, now=NOW)
    archived = cs.create_strategy(conn, name="Arc", body="arc", now=NOW)
    cs.archive_strategy(conn, archived.id, now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, now=NOW
    )
    cs.set_strategies(conn, it.id, [(enabled.id, 0), (disabled.id, 1), (archived.id, 2)])
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.name for lyr in result.layers] == ["On"]


def test_active_calibration_appended_when_self_correct(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, self_correct=True,
        now=NOW,
    )
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    cs.create_calibration(conn, it.id, body="CALIB-V1", cause="seed", now=NOW)
    v2 = cs.create_calibration(conn, it.id, body="CALIB-V2 {{now}}", cause="miss", now=NOW)
    cs.set_active_calibration(conn, it.id, v2.version)

    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.kind for lyr in result.layers] == ["template", "calibration"]
    calib = result.layers[-1]
    assert "CALIB-V2" in calib.rendered
    assert "{{now}}" not in calib.rendered  # variable rendered in the calibration body too


def test_calibration_skipped_when_not_self_correct(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, self_correct=False,
        now=NOW,
    )
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    v1 = cs.create_calibration(conn, it.id, body="CALIB", cause="seed", now=NOW)
    cs.set_active_calibration(conn, it.id, v1.version)
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.kind for lyr in result.layers] == ["template"]


def test_calibration_skipped_when_no_active_version(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, self_correct=True,
        now=NOW,
    )
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    cs.create_calibration(conn, it.id, body="CALIB", cause="seed", now=NOW)
    # no active version selected -> calibration layer omitted
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.kind for lyr in result.layers] == ["template"]


def test_archived_active_calibration_not_used(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, self_correct=True,
        now=NOW,
    )
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    c1 = cs.create_calibration(conn, it.id, body="CALIB", cause="seed", now=NOW)
    cs.set_active_calibration(conn, it.id, c1.version)
    cs.archive_calibration(conn, c1.id)  # archiving also clears the active selection
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert [lyr.kind for lyr in result.layers] == ["template"]


def test_joined_prompt_concatenates_layers_in_order(conn: sqlite3.Connection) -> None:
    set_system_prompt(conn, "SYS", now=NOW)
    a = cs.create_strategy(conn, name="A", body="AAA", now=NOW)
    b = cs.create_strategy(conn, name="B", body="BBB", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", self_correct=True, now=NOW
    )
    cs.set_strategies(conn, it.id, [(a.id, 0), (b.id, 1)])
    v1 = cs.create_calibration(conn, it.id, body="CAL", cause="seed", now=NOW)
    cs.set_active_calibration(conn, it.id, v1.version)
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    # joined prompt holds every layer in order
    assert result.prompt.index("SYS") < result.prompt.index("AAA")
    assert result.prompt.index("AAA") < result.prompt.index("BBB")
    assert result.prompt.index("BBB") < result.prompt.index("CAL")


def test_tokens_used_aggregated_across_layers(conn: sqlite3.Connection) -> None:
    a = cs.create_strategy(conn, name="A", body="{{kpis_json}}", now=NOW)
    b = cs.create_strategy(conn, name="B", body="{{allocation_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="C", scope="portfolio", use_system_prompt=False, now=NOW
    )
    cs.set_strategies(conn, it.id, [(a.id, 0), (b.id, 1)])
    result = assemble.assemble_layers(conn, it.id, _ctx(conn))
    assert "kpis_json" in result.tokens_used
    assert "allocation_json" in result.tokens_used
