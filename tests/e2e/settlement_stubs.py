"""Stubbed ``/api/settlement/*`` bodies for the e2e flows the real backend cannot produce
cheaply (進行緩慢, a failure with its consequence, a self-check difference, 25 runs of history,
the 月度成績 year closes before package G).

Every body is E's REAL shape, not an idea of it (wave-2 review F #5):

- the contract models of ``portfolio/settlement_models.py``, dumped as the routes dump them
  (``mode="json", by_alias=True``);
- the extra keys the routes add, made by E's OWN renderers where they exist —
  ``settlement_service._record_row`` (a trail record's ``actor_text`` / ``op_text`` /
  ``table_label`` / ``account_text`` / ``attempted_text`` / ``handed_back``),
  ``settlement_service.trigger_text`` + ``render_trigger`` (a trigger sentence as the viewer
  reads it, ``{started_by}`` already rendered), the report's ``restatements``, the change
  page's ``counters`` / ``tables`` / ``handed_back_text``.

``tests/contract/test_settlement_stub_shapes.py`` compares every builder's keys, recursively,
with the real routes' output on a real run, so a stub cannot drift from E again.
Not a test module (no ``test_`` prefix): the flows import it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, Route
from pydantic import BaseModel

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio.settlement_models import (
    PHASES,
    ChangeCounter,
    ChangeRecord,
    ChangeTableCount,
    DiffRow,
    PeriodClose,
    Restatement,
    RunProgress,
    RunReport,
    RunSummary,
    SettlementStatus,
)

PATTERN = "**/api/settlement/**"
GUEST = "user:guest"      # the e2e servers run in guest mode: the viewer is 'user:guest'
_TPE = ZoneInfo("Asia/Taipei")


def _dump(model: BaseModel) -> dict[str, Any]:
    out: dict[str, Any] = model.model_dump(mode="json", by_alias=True)
    return out


def trigger(reason: str, at: str, *, started_by: str | None = GUEST, cause: str = "",
            viewer: str | None = GUEST) -> str:
    """A trigger sentence exactly as E serves it to *viewer* (stored, then rendered)."""
    now = datetime.fromisoformat(at).astimezone(_TPE)
    stored = service.trigger_text(reason, now=now, cause=cause)
    return service.render_trigger(stored, started_by, viewer)


def status(state: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": state, "generation": 41, "settled_through": "2026-06-10", "calc_ok": True,
        "live_from": "2026-06-11", "final_through": "2026-06-10", "seq": 1,
    }
    base.update(kw)
    return _dump(SettlementStatus(**base))


def summary(run_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id, "status": "published", "status_text": "完成", "reason": "nightly",
        "trigger_text": trigger("nightly", "2026-06-10T06:40:00+08:00", started_by=None),
        "started_at": "2026-06-10T06:40:00+08:00",
        "finished_at": "2026-06-10T06:40:09+08:00", "duration_s": 9.2, "eta_initial_s": 10.0,
        "range_from": "2026-06-10", "range_to": "2026-06-10", "days": 1,
        "generation_before": 40, "generation_after": 41,
    }
    base.update(kw)
    return _dump(RunSummary(**base))


def restatement(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i, "kind": "M", "period": "2026-03", "field": "total_market_value",
        "label": "總市值", "unit": "TWD", "old_value": "1200000", "new_value": "1234567.89",
        "run_id": 412, "reason": "2025-06-16 交易更正後重新結算",
        "source": "transactions:123", "at": "2026-06-11T10:33:00+08:00",
    }
    base.update(kw)
    return _dump(Restatement(**base))


def diff_row(run_id: int, day: str, field: str, kind: str, **kw: Any) -> dict[str, Any]:
    """One ``GET /api/settlement/runs/{id}/diff`` row (``settle_run_diff``)."""
    return _dump(DiffRow.model_validate(
        {"run_id": run_id, "day": day, "field": field, "kind": kind, **kw}))


def report(run_id: int, **kw: Any) -> dict[str, Any]:
    """``GET /api/settlement/runs/{id}``: the RunReport plus the route's ``restatements``."""
    sm = kw.pop("summary", None) or summary(run_id)
    restatements = kw.pop("restatements", [])
    base: dict[str, Any] = {
        "summary": sm,
        "inputs": {"scope": "nightly", "reason": "nightly", "through": "2026-06-10"},
        "range": {"from": sm["range_from"], "to": sm["range_to"], "days": sm["days"],
                  "kept_before": 400, "kind": "dirty"},
        "versions": {"generation_before": 40, "generation_after": 41,
                     "settled_through_before": "2026-06-09", "settled_through_after": "2026-06-10",
                     "calc_version_before": "a1b2c3d", "calc_version": "a1b2c3d",
                     "reporting_ccy_before": "TWD", "reporting_ccy": "TWD"},
        "validation": {"status": "done", "findings": []},
        "steps": [{"key": k, "label": label, "status": "done", "elapsed_s": 0.4}
                  for k, label in PHASES],
        "history": {"counts": {}, "note": None},
    }
    base.update(kw)
    body: dict[str, Any] = _dump(RunReport(**base))
    body["restatements"] = restatements
    return body


def progress(run_id: int, **kw: Any) -> dict[str, Any]:
    """The status's ``run``: the progress file as E serves it (trigger rendered; the worker
    never writes ``queued`` — changes made during a run are the STATUS's ``queued``)."""
    phases = []
    for i, (key, label) in enumerate(PHASES):
        st = "done" if i < 3 else ("running" if i == 3 else "pending")
        ph: dict[str, Any] = {"key": key, "label": label, "status": st}
        if st == "done":
            ph.update(started_at="2026-06-11T10:32:02+08:00", elapsed_s=1.8,
                      detail="86 檔 · 價格 74,393 筆" if key == "load" else None)
        if key == "valuate":
            ph.update(done=281, total=489, unit="天", rate="35 天/秒", elapsed_s=7.9,
                      started_at="2026-06-11T10:32:04+08:00",
                      detail="已寫入 2025-06 至 2026-03（10 個月）")
        if key == "close":
            ph.update(total=17, unit="個期間")
        phases.append(ph)
    base: dict[str, Any] = {
        "run_id": run_id, "status": "running", "reason": "prompt", "started_by": GUEST,
        "trigger_text": trigger("prompt", "2026-06-11T10:32:00+08:00", cause="交易變更"),
        "range": {"from": "2025-06-16", "to": "2026-06-10", "days": 360, "kept_before": 1694,
                  "kind": "dirty"},
        "versions": {"generation_before": 41, "settled_through_before": "2026-06-10",
                     "calc_version_before": "a1b2c3d", "calc_version": "a1b2c3d",
                     "reporting_ccy_before": "TWD", "reporting_ccy": "TWD"},
        "started_at": "2026-06-11T10:32:02+08:00", "elapsed_s": 12.4, "percent": 58,
        "eta_s": 8.0, "eta_initial_s": 18.0,
        "eta_basis": "逐日估值實測速度 35 天/秒；其餘階段依上次結算的耗時",
        "priority": "背景低優先序",
        "changes": {"tables": [table("transactions", 2, "2025-06-16", "2025-06-18"),
                               table("prices", 14, "2025-07-01", "2025-07-21", keys=3)],
                    "records_total": 2},
        "validation": {"status": "done", "findings": [
            {"kind": "oversold", "day": "2025-08-01", "account_id": "tw_broker",
             "symbol": "2330",
             "text": "以待釐清處理：{account:tw_broker} 2330 於 2025-08-01 賣超"}]},
        "phases": phases,
        "current": {"day": "2026-03-24", "positions": 41},
        "on_cancel": "保留第 41 版（結算至 2026-06-10）；這 2 筆交易與 14 筆價格變更回到待結算",
    }
    base.update(kw)
    return _dump(RunProgress(**base))


def table(name: str, changes: int, earliest: str, latest: str, *,
          keys: int | None = None) -> dict[str, Any]:
    from portfolio_dash.portfolio.settlement_models import CHANGE_TABLE_ZH

    return _dump(ChangeTableCount.model_validate(
        {"table": name, "label": CHANGE_TABLE_ZH[name], "changes": changes, "keys": keys,
         "earliest": earliest, "latest": latest}))


def change(i: int, *, run_id: int | None = None, viewer: str | None = GUEST,
           **kw: Any) -> dict[str, Any]:
    """A trail record exactly as ``GET /api/settlement/changes`` serves it (E's renderer)."""
    base: dict[str, Any] = {
        "id": i, "seq": 100 + i, "table": "transactions", "op": "update", "row_key": str(500 + i),
        "old_account_id": "tw_broker", "new_account_id": "tw_broker", "old_symbol": "2330",
        "new_symbol": "2330", "old_day": "2025-06-18", "new_day": "2025-06-16",
        "actor": GUEST, "written_at": "2026-06-11T10:31:40+08:00", "state": "claimed",
        "claimed_run_id": run_id,
    }
    base.update(kw)
    return service._record_row(ChangeRecord(**base), viewer=viewer, run_id=run_id)


def counter(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i, "table": "prices", "key": "2330", "changes": 1, "earliest": "2026-06-09",
        "latest": "2026-06-09", "state": "pending",
    }
    base.update(kw)
    return _dump(ChangeCounter(**base))


def changes_page(rows: list[dict[str, Any]], *, counters: list[dict[str, Any]] | None = None,
                 tables: list[dict[str, Any]] | None = None,
                 run_id: int | None = None) -> dict[str, Any]:
    """``GET /api/settlement/changes``: records (paged), counters and tables (whole)."""
    return {"rows": rows, "total_count": len(rows), "counters": counters or [],
            "tables": tables or [],
            "handed_back_text": "本次未生效，已退回待結算" if run_id is not None else None}


def period(period: str, **kw: Any) -> dict[str, Any]:
    """A year close as ``GET /api/settlement/periods?kind=Y`` will serve it (package G)."""
    kpi: dict[str, Any] = {"reporting_ccy": "TWD", "total_market_value": "2000000",
                           "total_return": "300000", "total_return_rate": "0.15", "xirr": None,
                           "xirr_reason": "期間不足一年，XIRR 不計", "period_twr": "0.08"}
    kpi.update(kw.pop("kpi", {}))
    base: dict[str, Any] = {"kind": "Y", "period": period, "end_day": period + "-12-31",
                            "kpi": kpi, "activity": {}, "source": "store"}
    base.update(kw)
    return _dump(PeriodClose(**base))


class SettleStub:
    """The stubbed /api/settlement/* — mutable, and it records every request it answered."""

    def __init__(self, st: dict[str, Any]) -> None:
        self.status = st
        self.reports: dict[int, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.changes: dict[str, dict[str, Any]] = {}   # state -> changes_page(...)
        self.diffs: dict[int, list[dict[str, Any]]] = {}   # run id -> diff_row(...) rows
        self.periods: list[dict[str, Any]] | None = []
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def handle(self, route: Route) -> None:
        req = route.request
        url = urlparse(req.url)
        path = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if req.method == "POST":
            body = json.loads(req.post_data) if req.post_data else None
            self.posts.append((path, body))
            if path.endswith("/run"):
                return self._json(route, {"run_id": 900 + len(self.posts)}, 202)
            return self._json(route, {"run_id": 412, "status": "cancelled", "cancelled": True})
        self.gets.append(req.url)
        if path.endswith("/settlement/status"):
            return self._json(route, self.status)
        if path.endswith("/settlement/runs"):
            rows = [r for r in self.runs
                    if q.get("only_changes") not in ("1", "true") or r["had_changes"]]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
            return self._json(route, {"rows": rows[off:off + lim], "total_count": len(rows)})
        m = re.search(r"/settlement/runs/(\d+)(/diff)?$", path)
        if m:
            if int(m.group(1)) not in self.reports:
                return self._json(route, {"error": {
                    "code": "not_found",
                    "message": f"找不到重新結算紀錄 #{m.group(1)}（可能已超過保留期限）"}}, 404)
            if m.group(2):
                rows = self.diffs.get(int(m.group(1)), [])
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
                return self._json(route, {"rows": rows[off:off + lim], "total_count": len(rows)})
            return self._json(route, self.reports[int(m.group(1))])
        if path.endswith("/settlement/changes"):
            page = self.changes.get(q.get("state", "pending")) or changes_page([])
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
            body = dict(page)
            body["rows"] = page["rows"][off:off + lim]
            return self._json(route, body)
        if path.endswith("/settlement/periods"):
            if self.periods is None:   # the route not mounted yet (before package G)
                return self._json(route, {"error": {"code": "not_found",
                                                    "message": "找不到指定的資料或頁面"}}, 404)
            return self._json(route, {"rows": self.periods, "total_count": len(self.periods)})
        return self._json(route, {"error": {"code": "not_found",
                                            "message": "找不到指定的資料或頁面"}}, 404)

    @staticmethod
    def _json(route: Route, body: Any, status_code: int = 200) -> None:
        route.fulfill(status=status_code, content_type="application/json",
                      body=json.dumps(body, ensure_ascii=False))

    def count(self, needle: str) -> int:
        return sum(1 for u in self.gets if needle in u)


@contextmanager
def stubbed(page: Page, stub: SettleStub,
            extra: list[tuple[str, Callable[[Route], None]]] | None = None) -> Iterator[SettleStub]:
    """Install the stub (and any extra routes) on THIS page only; always removed."""
    page.route(PATTERN, stub.handle)
    for pattern, handler in extra or []:
        page.route(pattern, handler)
    try:
        yield stub
    finally:
        page.unroute(PATTERN, stub.handle)
        for pattern, handler in extra or []:
            page.unroute(pattern, handler)


__all__ = [
    "GUEST", "PATTERN", "SettleStub", "change", "changes_page", "counter", "period",
    "progress", "report", "restatement", "status", "stubbed", "summary", "table", "trigger",
]
