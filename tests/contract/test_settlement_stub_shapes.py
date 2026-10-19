"""The e2e stubs of ``/api/settlement/*`` must be E's real shapes (wave-2 review F #5).

``tests/e2e/settlement_stubs.py`` builds the bodies the stubbed flows serve (the states the real
backend cannot produce cheaply). Its first version encoded shapes E never sends: a ``seq``
injected after the dump, ``queued`` inside the progress file, an unrendered ``{started_by}``,
no ``counters``, a restatement ``reason`` that was a code. This test runs the REAL service and
worker (inline, on a golden copy) through the real routes — a full run, a back-dated trade and
a price change, a dirty run with records, counters and restatements, a preparing run — and
compares, recursively, the KEYS of every builder's body with the route's: a builder that
gains, loses or renames a key fails here, not in a browser that silently renders nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import action_log
from portfolio_dash.api import settlement_service as service
from portfolio_dash.api.app import create_app
from portfolio_dash.shared.config import get_settings
from tests.conftest import _seed_golden, init_golden_base
from tests.e2e import settlement_stubs as stubs
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup

pytestmark = pytest.mark.enable_socket   # TestClient's anyio portal (a Windows socketpair)


#: Fields whose value is a MAP keyed by data (a field name, a currency, a day, a flag), not a
#: schema: their keys depend on the run, so nothing beneath them is compared.
_MAPS = frozenset({"counts", "detail", "flags_before", "flags_after", "notes_before",
                   "notes_after", "unconfirmed_closes", "by_currency_value"})


def _keys(stub: Any, real: Any, path: str = "$") -> list[str]:
    """Every place the two bodies' key sets differ (dicts by keys, lists by first element)."""
    out: list[str] = []
    if path.rsplit(".", 1)[-1] in _MAPS:
        return out
    if isinstance(stub, dict) and isinstance(real, dict):
        if set(stub) != set(real):
            out.append(f"{path}: stub-only {sorted(set(stub) - set(real))}, "
                       f"real-only {sorted(set(real) - set(stub))}")
        for k in set(stub) & set(real):
            if stub[k] is not None and real[k] is not None:
                out.extend(_keys(stub[k], real[k], f"{path}.{k}"))
    elif isinstance(stub, list) and isinstance(real, list) and stub and real:
        out.extend(_keys(stub[0], real[0], f"{path}[0]"))
    return out


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    mp = pytest.MonkeyPatch()
    path = tmp_path_factory.mktemp("stub_shapes") / "f.db"
    conn = open_db(path)
    init_golden_base(conn)
    _seed_golden(conn)
    action_log.ensure_table(conn)
    conn.commit()
    conn.close()
    sup.prepare(path, mp, stub_worker=False)
    client = TestClient(create_app(), raise_server_exceptions=False)
    out: dict[str, Any] = {}
    try:
        assert client.post("/api/settlement/run", json={"scope": "full"}).status_code == 202
        sup.back_dated_trade(path, date(2026, 2, 2))
        c: sqlite3.Connection = open_db(path)
        c.execute("UPDATE prices SET close = '612.5', close_raw = '612.5' "
                  "WHERE instrument = '2330' AND as_of_date = '2026-06-09'")
        c.commit()
        c.close()
        out["status_pending"] = client.get("/api/settlement/status").json()
        out["changes_pending"] = client.get("/api/settlement/changes",
                                            params={"state": "pending"}).json()
        rid = client.post("/api/settlement/run", json={"scope": "dirty"}).json()["run_id"]
        out["status_clean"] = client.get("/api/settlement/status").json()
        out["runs"] = client.get("/api/settlement/runs").json()
        out["report"] = client.get(f"/api/settlement/runs/{rid}").json()
        out["changes_run"] = client.get("/api/settlement/changes",
                                        params={"state": f"run:{rid}"}).json()
        out["diff"] = client.get(f"/api/settlement/runs/{rid}/diff").json()
        out["run_id"] = rid
        # package G's period closes: the year rows 月度成績's 年 toggle reads, and month rows
        out["periods_y"] = client.get("/api/settlement/periods", params={"kind": "Y"}).json()
        out["periods_m"] = client.get("/api/settlement/periods",
                                      params={"kind": "M", "limit": 3}).json()
        sup.fake_preparing_run(path, reason="button", started_by=stubs.GUEST)
        out["status_preparing"] = client.get("/api/settlement/status").json()
        yield out
    finally:
        service.reset_for_tests()
        get_settings.cache_clear()
        mp.undo()


def test_the_real_run_produced_every_shape_the_comparison_needs(real: dict[str, Any]) -> None:
    """Guard the guard: an empty list compares nothing."""
    assert real["changes_run"]["rows"] and real["changes_run"]["counters"], real["changes_run"]
    assert real["report"]["restatements"], "the price change moved no closed period"
    assert real["changes_pending"]["counters"] and real["changes_pending"]["tables"]
    assert real["status_preparing"]["run"] is not None
    assert real["status_clean"]["last_run"] is not None
    assert real["status_pending"]["seq"] is not None
    assert real["diff"]["rows"], "the back-dated trade changed no settled day"


def test_the_status_stubs_have_the_route_keys(real: dict[str, Any]) -> None:
    pending = stubs.status("pending", pending={"tables": [], "records_total": 0},
                           last_run=stubs.summary(1), last_change_run=stubs.summary(1))
    assert not _keys(pending, real["status_pending"])
    preparing = stubs.status("preparing", run=stubs.progress(2, status="preparing"),
                             last_run=stubs.summary(1), last_change_run=stubs.summary(1))
    assert not _keys(preparing, real["status_preparing"])


def test_the_run_page_and_report_stubs_have_the_route_keys(real: dict[str, Any]) -> None:
    assert not _keys({"rows": [stubs.summary(1)], "total_count": 1}, real["runs"])
    rep = stubs.report(5, restatements=[stubs.restatement(1)])
    assert not _keys(rep, real["report"])
    assert not _keys(stubs.restatement(1), real["report"]["restatements"][0])


def test_the_change_trail_stubs_have_the_route_keys(real: dict[str, Any]) -> None:
    rid = real["run_id"]
    page = stubs.changes_page([stubs.change(1, run_id=rid)], counters=[stubs.counter(1)],
                              tables=[stubs.table("prices", 1, "2026-06-09", "2026-06-09",
                                                  keys=1)], run_id=rid)
    assert not _keys(page, real["changes_run"])
    pend = stubs.changes_page([stubs.change(1)], counters=[stubs.counter(1)],
                              tables=[stubs.table("transactions", 1, "2026-02-02",
                                                  "2026-02-02")])
    assert not _keys(pend, real["changes_pending"])
    row = stubs.diff_row(rid, "2026-02-02", "total_value", "changed", before="1", after="2")
    assert not _keys({"rows": [row], "total_count": 1}, real["diff"])


def test_the_trigger_stub_is_what_the_route_serves(real: dict[str, Any]) -> None:
    """The dirty run's trigger sentence, as the guest viewer reads it, has the builder's form."""
    served = real["report"]["summary"]["trigger_text"]
    assert "{started_by}" not in served
    hm = served.split(" 於 ")[1][:5]
    at = real["report"]["summary"]["started_at"][:11] + hm + ":00+08:00"
    assert served == stubs.trigger("prompt", at, cause=served.split("確認提示：")[1][:-1])


def test_the_period_stub_has_the_route_keys(real: dict[str, Any]) -> None:
    """``stubs.period`` (the year close the 月度成績 flows stub) ≡ package G's real
    ``GET /api/settlement/periods`` rows — a store close and the live current period alike."""
    assert real["periods_y"]["rows"] and real["periods_m"]["rows"]
    assert {r["source"] for r in real["periods_m"]["rows"]} == {"store", "live"}
    stub = {"rows": [stubs.period("2025")], "total_count": 1}
    assert not _keys(stub, real["periods_y"])
    for row in real["periods_m"]["rows"]:
        assert not _keys(stub, {"rows": [row], "total_count": 1}), row["period"]
