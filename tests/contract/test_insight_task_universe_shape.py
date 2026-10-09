"""An insight task's universe in an unknown shape is refused loudly (owner 2026-09-30, item 11).

Measured by the verifier (R10): ``POST /api/insight-tasks`` with the universe written as a
plain list — ``["2884"]`` instead of the wizard's ``{"mode": "custom", "symbols": ["2884"]}``
— was accepted, stored verbatim, and resolved as "follow holdings": the run produced 13 cards
instead of 1 ($0.0174 extra). ``composer_store`` stored the value as opaque JSON and the one
resolver (``api/insight_service.py::_resolve_universe_raw``) maps anything it does not know to
holdings, so nothing between the door and the bill ever looked at the shape.

The rule is ``architecture.md``'s data_ingestion rule, applied to every door: reject bad
input loudly, never silently coerce. Every door that accepts a universe — create and update
on both route prefixes, and the draft preflight — answers 422 with ``field: "universe"``, a
zh message that names the accepted shapes in words, and the machine-readable shapes in
``issues``. The persistence seam refuses the same values as a backstop.

What must NOT change: a row written before the rule keeps loading. The read side
(``_json_or_none`` + the resolver) stays tolerant, so a stored list still reads and still
resolves to holdings — only re-saving it in that shape is refused.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import composer_store as cs

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_PREFIXES = ("/api/insight-tasks", "/api/insight-types")

#: Every shape the create wizard / the universe dialog can write, plus "not set".
_VALID: list[Any] = [
    None,
    {"mode": "all"},
    {"mode": "all_registered"},
    {"mode": "custom", "symbols": ["2330"]},
    {"mode": "custom", "symbols": []},  # R2's empty-universe state — the gate reports it
]

#: Shapes no door writes. Each one used to resolve SILENTLY to holdings (or, for the
#: number-as-symbol case, to a mangled symbol: ``0056`` sent as a JSON number is ``56``).
_INVALID: list[tuple[str, Any]] = [
    ("list", ["2884"]),
    ("string", "all"),
    ("typo_mode", {"mode": "all_registred"}),
    ("missing_mode", {}),
    ("custom_without_symbols", {"mode": "custom"}),
    ("symbols_not_a_list", {"mode": "custom", "symbols": "2884"}),
    ("symbol_not_a_string", {"mode": "custom", "symbols": [2884]}),
    ("blank_symbol", {"mode": "custom", "symbols": ["2884", " "]}),
    ("extra_key", {"mode": "all", "symbols": ["2884"]}),
]


def _strategy(api_client: TestClient) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    return int(sp["id"])


def _body(sid: int, universe: Any) -> dict[str, Any]:
    return {"name": "個股健檢", "scope": "per_symbol", "strategy_ids": [sid],
            "universe": universe}


def _task_count(golden_db: sqlite3.Connection) -> int:
    return int(golden_db.execute("SELECT COUNT(*) FROM insight_types").fetchone()[0])


def _assert_refused(resp: Any) -> None:
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert err["field"] == "universe"
    # the message names the accepted shapes in the owner's words, not by identifier
    for words in ("全部持倉", "持倉＋觀察清單", "自選標的"):
        assert words in err["message"], err["message"]
    # the machine-readable shapes ride in issues, for an API client
    accepted = err["issues"][0]["accepted"]
    assert {"mode": "all"} in accepted and {"mode": "all_registered"} in accepted
    assert any(a.get("mode") == "custom" and isinstance(a.get("symbols"), list)
               for a in accepted), accepted


# --- the measured case ---------------------------------------------------------------------


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_the_measured_list_universe_is_refused_at_create_and_nothing_is_stored(
    api_client: TestClient, golden_db: sqlite3.Connection, prefix: str
) -> None:
    sid = _strategy(api_client)
    before = _task_count(golden_db)
    _assert_refused(api_client.post(prefix, json=_body(sid, ["2884"])))
    assert _task_count(golden_db) == before, "a refused create must not write a task"


# --- every door, every bad shape -----------------------------------------------------------


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize(("label", "universe"), _INVALID, ids=[i[0] for i in _INVALID])
def test_create_refuses_every_unknown_shape(
    api_client: TestClient, prefix: str, label: str, universe: Any
) -> None:
    _assert_refused(api_client.post(prefix, json=_body(_strategy(api_client), universe)))


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize(("label", "universe"), _INVALID, ids=[i[0] for i in _INVALID])
def test_update_refuses_every_unknown_shape_and_leaves_the_row_alone(
    api_client: TestClient, prefix: str, label: str, universe: Any
) -> None:
    sid = _strategy(api_client)
    created = api_client.post(prefix, json=_body(sid, {"mode": "custom", "symbols": ["2330"]}))
    assert created.status_code == 200, created.text
    tid = created.json()["id"]
    _assert_refused(api_client.put(f"{prefix}/{tid}", json=_body(sid, universe)))
    row = next(t for t in api_client.get(prefix).json() if t["id"] == tid)
    assert row["universe"] == {"mode": "custom", "symbols": ["2330"]}


@pytest.mark.parametrize(("label", "universe"), _INVALID, ids=[i[0] for i in _INVALID])
def test_the_draft_preflight_refuses_every_unknown_shape(
    api_client: TestClient, label: str, universe: Any
) -> None:
    """The wizard's dry run of an unsaved draft is a door too: a list universe there
    estimated the holdings-wide run the verifier then paid for."""
    resp = api_client.post("/api/insight-tasks/0/preflight",
                           json=_body(_strategy(api_client), universe))
    _assert_refused(resp)


# --- what must keep working ----------------------------------------------------------------


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize("universe", _VALID, ids=repr)
def test_every_shape_the_wizard_writes_is_accepted_on_create_and_update(
    api_client: TestClient, prefix: str, universe: Any
) -> None:
    sid = _strategy(api_client)
    created = api_client.post(prefix, json=_body(sid, universe))
    assert created.status_code == 200, created.text
    assert created.json()["universe"] == universe
    tid = created.json()["id"]
    updated = api_client.put(f"{prefix}/{tid}", json=_body(sid, universe))
    assert updated.status_code == 200, updated.text
    draft = api_client.post("/api/insight-tasks/0/preflight", json=_body(sid, universe))
    assert draft.status_code == 200, draft.text


def test_a_custom_universe_resolves_to_its_one_symbol(
    api_client: TestClient,
) -> None:
    """The shape the verifier meant: ONE symbol, one card — never the whole book."""
    sid = _strategy(api_client)
    tid = api_client.post("/api/insight-tasks",
                          json=_body(sid, {"mode": "custom", "symbols": ["2330"]})).json()["id"]
    task = next(t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
                if t["id"] == tid)
    assert task["nodes"]["input"]["text"] == "1 檔標的"


def test_a_row_stored_before_the_rule_still_loads_and_resolves(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A task written before 2026-09-30 in the list shape (the verifier's R10 task is one)
    keeps reading: the list, the status card and the resolver all stay tolerant. Re-saving
    it unchanged is refused — that is the loud half — and 編輯標的 writes a valid shape."""
    from portfolio_dash.api.insight_service import _resolve_universe
    from portfolio_dash.portfolio.dashboard import build_dashboard
    from portfolio_dash.shared.enums import Currency

    sid = _strategy(api_client)
    tid = api_client.post("/api/insight-tasks",
                          json=_body(sid, {"mode": "all"})).json()["id"]
    golden_db.execute("UPDATE insight_types SET universe = ? WHERE id = ?", ('["2884"]', tid))
    golden_db.commit()

    row = next(t for t in api_client.get("/api/insight-tasks").json() if t["id"] == tid)
    assert row["universe"] == ["2884"]
    status = api_client.get("/api/insight-tasks/status")
    assert status.status_code == 200
    it = cs.get_insight_type(golden_db, tid)
    assert it is not None
    data = build_dashboard(golden_db, now=NOW, reporting=Currency.TWD)
    assert _resolve_universe(golden_db, it, data) == ["2330", "AAPL"]  # tolerant read

    _assert_refused(api_client.put(f"/api/insight-tasks/{tid}", json=_body(sid, ["2884"])))
    fixed = api_client.put(f"/api/insight-tasks/{tid}",
                           json=_body(sid, {"mode": "custom", "symbols": ["2884"]}))
    assert fixed.status_code == 200, fixed.text


# --- the persistence seam is a backstop --------------------------------------------------


@pytest.mark.parametrize(("label", "universe"), _INVALID, ids=[i[0] for i in _INVALID])
def test_the_store_refuses_the_same_shapes(
    golden_db: sqlite3.Connection, label: str, universe: Any
) -> None:
    with pytest.raises(cs.UniverseShapeError):
        cs.create_insight_type(golden_db, name="x", scope="per_symbol", universe=universe,
                               now=NOW)
    it = cs.create_insight_type(golden_db, name="y", scope="per_symbol", now=NOW)
    with pytest.raises(cs.UniverseShapeError):
        cs.update_insight_type(golden_db, it.id, name="y", scope="per_symbol",
                               universe=universe, now=NOW)
    assert cs.get_insight_type(golden_db, it.id).universe is None  # type: ignore[union-attr]
