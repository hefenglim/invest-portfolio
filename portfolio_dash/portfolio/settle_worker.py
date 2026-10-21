"""The settlement worker (PERF spec 2026-10-18 §9.2–§9.6): one run of the settlement.

Started by ``api/settlement_service.py`` as ``python -m portfolio_dash.portfolio.settle_worker
--run <id>`` (``shared/child_runner.py``), or called in-process by :func:`run` when
``PD_SETTLE_MODE=inline`` (hermetic tests, Windows development; synchronous, never on a request
thread).

Imports only ``portfolio/``, ``pricing/``, ``forex/``, ``data_ingestion/`` and ``shared/`` —
never ``fastapi``, ``litellm``, ``yfinance``, ``pandas`` or any ``api`` / ``scheduler`` /
``export`` / ``strategy`` / ``news`` / ``ops`` / ``llm_insight`` module (a test inspects
``sys.modules`` after the worker's imports).

The contract of :func:`run` (what the settlement service builds on)
====================================================================

**Before it is called** the parent has created the run's ``job_runs`` row and its ``preparing``
report (``settlement_store.create_run``, the ``RunRequest`` in ``inputs``, the trigger sentence
in ``trigger_text``). :func:`run` then, in this order:

1. takes ``<data>/settlement.lock`` with ``proc_lock.lock(path, timeout=LOCK_TIMEOUT_S)`` —
   never ``try_lock``: a status read probes the lock by taking it for an instant. Held until
   :func:`run` returns, so the OS frees it the moment the process dies however it dies (the
   liveness signal, §9.4). Still held by another process after the timeout: the report is
   ended ``failed`` (「另一個結算程序仍持有結算鎖」) only while it is still ``preparing`` and
   not the active run, and the state is otherwise left alone;
2. writes the progress file ``<data>/settlement-progress.json`` (:data:`PROGRESS_FILE`) with
   ITS OWN ``os.getpid()`` (on Windows the venv ``python.exe`` is a launcher, so ``Popen.pid``
   is not the interpreter) — replaced atomically, at most every 0.5 s and at every phase
   change, until the run ends; the last write carries the end status;
3. reads its ``RunRequest`` from the report and claims (``settlement_store.claim``, which applies
   the run range of plan §6.1); a refused claim ends the report ``failed``;
4. runs the phases of §9.3 — 讀取帳本與行情 · 檢查帳本 · 重播帳本 · 逐日估值 · 月結與年結 ·
   基準比較 · 比對與切換 (預熱 is the web process's, phase 8). The report (``settle_run``) is
   updated at the end of every phase (``update_run``), so an interrupted run keeps what it
   finished;
5. ends the run itself — every end but a kill:

   - ``published`` / ``partial`` / ``superseded`` — from ``settlement_store.swap`` (the swap
     decides which, from the ``dirty_from`` it finds; ``superseded`` publishes nothing);
   - ``failed`` — any exception in a phase, or a refused swap (``SwapRefused``): the end
     transaction of §9.4 (``finalise_run`` → ``end_without_publish``) runs, the report names
     the phase and the zh reason, and its consequence is derived from the state (``on_cancel``);

   then freezes the report (``finalise_run``) and returns the stored end status.

``cancelled`` and ``interrupted`` are never written here: a cancel terminates this process and
the service ends the run; a kill leaves the report ``running`` (or ``preparing``) for the
service's waiter or for boot. The writes carry no actor — the change trail records ledger
changes, not store writes — and touch no ledger table, so no ledger trigger fires.
``job_runs`` is the parent's (P7). The process exits 0 when the report is final, 1 otherwise.

**Byte identity** (plan §0 invariant 1). Every settled day is the v0.1.31 trend point of that
day — ``value_series`` over books from ONE ``iter_books`` pass, ``compose_net_worth`` against
``daily_cash_series(start=)`` — and every close's KPI block is the dashboard's own
``kpis_as_of`` with "on or before D" sources (``period_close.kpi_at``). One pass over
``sorted(event days ∪ period ends)`` feeds both; a book is never rebuilt per month end.

**A series that is unavailable from some day U** (a flow dated U has no on-or-before FX rate in
the trend's pair set — the live build then shows no trend at all. Since PERF-F2's fix the pair
set, ``timeseries.trend_fx_currencies``, holds every converted flow's own currency, so this is
a rate genuinely missing on or before U): the days before U are valued over the ledger cut at
U − 1 (every point there is what the truncated world shows); a day from U on has no value in
any v0.1.31 view, so it is stored as a PLACEHOLDER (``SettledDay.placeholder``:
``total_value = net_invested = 0``, ``incomplete``, ``net_worth`` and ``cash_total`` NULL,
``cash_incomplete``) and the closes from U on carry no trend figure and no 當期報酬
(``notes.trend_unavailable``). A placeholder carries no figure:

- **it never outlives the condition that made it** — every run computes its own U first. When
  the published generation's first placeholder day P lies before the run's start and U is not
  P (the series is available again, or unavailable from another day), the run starts at
  ``min(P, U)`` instead and values every placeholder day again (the change that makes the
  series available may be dated after U and mark the store dirty only from its own date);
  the claim's range is widened accordingly (``settle_run.range_from``). **This widening is
  defense in depth with no known production trigger since PERF-F2's fix.** Before it, a later
  row in a new quote currency was one; now every flow's own currency is in the pair set, so
  every change that can end a missing-rate cause — a rate on or before U, or an edit or
  deletion of the flow itself — is dated on or before U and marks the store dirty from there.
  Its test reaches the branch by handing the worker v0.1.31's pair set (a monkeypatch,
  ``tests/settlement/test_worker_placeholders.py``). With U = P the placeholders stand: a
  nightly run on a ledger unavailable since U settles its one new day, never a re-run from U;
- **it is never a figure in a report**: a day with a value on one side and a placeholder on
  the other is ``value_to_none`` / ``none_to_value``, a placeholder on both is no change, a
  placeholder day is never counted as a valued 「新結算」 / 「移除」 day, and a run that adds
  placeholder days says 「新結算 N 天（沒有走勢資料）」;
- the read path serves a placeholder day live whenever the live series is available
  (``settlement_store.first_placeholder_day``).

**The closes start at the month of the ledger's first row** — any ledger: a deposit or a
conversion may precede the first trade, even by a year — not at the trend's first month, and
every close covers exactly its own calendar period. A month before the series has its cash, its
activity and its KPIs (no holdings), and no trend point (``notes.trend_unavailable``: the series
has not started). So every year is the sum of its months, holds its own cash and ties out with
that year's tax package; the swap checks the closes from there (``swap(closes_from=)``). A full
run and the self-check start there too.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Literal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.portfolio import period_close as pc
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.benchmark_counterfactual import ReportingFlow, accumulate
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, iter_books
from portfolio_dash.portfolio.dashboard import (
    _BENCH_EPOCH,
    _EPOCH,
    OnOrBefore,
    bounded_closes,
    closes_in,
)
from portfolio_dash.portfolio.dashboard_models import TrendPoint
from portfolio_dash.portfolio.ledger_findings import LedgerReview, review_ledger
from portfolio_dash.portfolio.networth import CashDay, compose_net_worth, daily_cash_series
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.returns import booked_dividends
from portfolio_dash.portfolio.settlement_models import (
    CHANGE_TABLE_ZH,
    PHASE_LABELS,
    PHASES,
    SETTLE_TABLE_ZH,
    DayValue,
    DiffRow,
    HistoryResult,
    PeriodCash,
    RunChanges,
    RunCurrent,
    RunFailure,
    RunPhase,
    RunProgress,
    RunReport,
    RunRequest,
    RunStatus,
    RunValidation,
    SelfcheckResult,
    StateResult,
)
from portfolio_dash.portfolio.timeseries import (
    FxHistory,
    PriceHistory,
    UnconvertibleFlow,
    build_reporting_flows,
    first_unconvertible_flow,
    holds_open_option,
    trend_book_days,
    trend_fx_currencies,
    trend_start,
    trend_symbols,
    trend_unavailable_reason,
    value_series,
)
from portfolio_dash.portfolio.twr import convert_closes, twr_index
from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.pricing.store import FxSeriesCache
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.child_runner import apply_test_stubs_from_env, lower_own_priority
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import LedgerBundle, valued_rows_as_of
from portfolio_dash.shared.money import to_db

logger = logging.getLogger(__name__)

#: The progress file in the data folder (§9.5).
PROGRESS_FILE = "settlement-progress.json"
#: How long the child waits for ``settlement.lock`` (a status read holds it for an instant).
LOCK_TIMEOUT_S = 10.0
#: The progress file is rewritten at most this often (and at every phase change).
PROGRESS_INTERVAL_S = 0.5
#: The words of a child that lowered its own priority (§9.2).
PRIORITY_LOW_ZH = "背景低優先序"

_ONE_DAY = timedelta(days=1)
_ZERO = Decimal("0")

#: The 2026-10-18 profile (spec Appendix A, the prod-shaped ledger): seconds per unit of each
#: phase, the ETA basis of a store's first run.
_PROFILE_S_PER_UNIT: dict[str, float] = {
    "load": 0.02, "check": 0.0002, "replay": 0.0002, "valuate": 0.004, "close": 0.08,
    "bench": 0.2, "swap": 0.3,
}
_PROFILE_BASIS_ZH = "依 2026-10-18 實測的各階段比例估算（尚無上次結算紀錄）"


# =============================================================================================
# Small helpers
# =============================================================================================


def _now() -> datetime:
    return app_now()


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """``[start, end]`` cut at calendar month boundaries."""
    out: list[tuple[date, date]] = []
    lo = start
    while lo <= end:
        nxt = date(lo.year + 1, 1, 1) if lo.month == 12 else date(lo.year, lo.month + 1, 1)
        hi = min(end, nxt - _ONE_DAY)
        out.append((lo, hi))
        lo = nxt
    return out


def _prev_end(kind: str, end: date) -> date:
    """The previous period's end day: the month before's last day, or the year before's."""
    if kind == "M":
        return date(end.year, end.month, 1) - _ONE_DAY
    return date(end.year - 1, 12, 31)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else to_db(value)


#: A ratio (period TWR, the rate, XIRR) restates only when it moved by more than this
#: absolute amount — or, for a huge ratio, by more than :data:`RATIO_RESTATE_RELATIVE` of its
#: size. The TWR index is chain-linked over every day at Decimal's 28 digits, so a correction
#: that leaves a period's value unchanged in fact — a price revised on two flowless days whose
#: two daily ratios cancel — still shifts the 27th digit, and a restatement row is never
#: pruned (accountant as-built check, 2026-10-20: rich 2026-02 read +1.58% → +1.58%). 1e-12 is
#: ten orders below the display (0.01%) and far above the arithmetic's noise.
RATIO_RESTATE_TOLERANCE = Decimal("1e-12")
#: A 28-digit value of size X carries noise near X·1e-27, so a huge ratio (a 30× gain in a
#: ledger's first month annualises to ~2e17) is also compared relative to its size.
RATIO_RESTATE_RELATIVE = Decimal("1e-20")


def _moved(old: Decimal | None, new: Decimal | None, unit: str) -> bool:
    """Whether a close's figure changed enough to restate: money exactly (its canonical TEXT),
    a ratio beyond the tolerance above; a value appearing or disappearing always.

    The ratio arithmetic runs in a WIDE local context: under the default 28 digits,
    ``quantize``-ing a ratio of 1e16 or more to 12 places raised ``InvalidOperation`` and failed
    every run that recomputed that close (accountant re-verification, 2026-10-20)."""
    if old is None or new is None:
        return (old is None) != (new is None)
    if unit == "ratio":
        with localcontext() as ctx:
            ctx.prec = 80
            diff = abs(old - new)
            bound = max(RATIO_RESTATE_TOLERANCE,
                        max(abs(old), abs(new)) * RATIO_RESTATE_RELATIVE)
            return diff > bound
    return _text(old) != _text(new)


#: A restated close's reason names at most this many changes, then 「等 N 筆」.
REASON_NAMED_MAX = 3
#: The reason of a restatement no claimed change explains (a 重新結算 that found a moved
#: figure with nothing claimed on or before the close's day).
REASON_RESETTLED_ZH = "重新結算"
#: The reason of a run whose code or calc version changed (unchanged since §14.6).
REASON_CALC_ZH = "計算規則更新"


def correction_reason(changes: Sequence[st.ClaimedChange], through: date, *,
                      calc_changed: bool) -> tuple[str, str | None]:
    """A restated close's ``(reason, source)`` (§14.6; accountant MINOR 2, owner ruling
    2026-10-22): every claimed change whose affected day is on or before the close's end day
    *through*, earliest first — 「2026-02-05 交易、2026-05-08 股利更正後重新結算」. A run
    names, per close, only what can have moved THAT close: one reason per run named its
    earliest change on every close, so a May dividend correction read as a February trade.

    - **An entry** is one ledger or reference record (「2026-02-05 交易」), one price / FX
      counter with its symbol or pair (「2026-06-09 價格（2330）」), or one import batch — the
      inserts and deletes of two or more claimed records sharing ``import_batch_id`` (an import
      or its undo), dated by its earliest record on or before *through*
      (「2026-01-03 匯入批次（交易／股利）」). An UPDATE of an imported row is a correction of
      that row and stays its own entry: it carries the row's batch id, not an import.
    - **At most** :data:`REASON_NAMED_MAX` distinct texts are named; when there are more
      entries than texts named (more entries, or two the same day and table), the total follows
      as 「等 N 筆」 — N counts entries, an import batch and a symbol's counter each one.
    - The vocabulary is ``CHANGE_TABLE_ZH``'s. :data:`REASON_CALC_ZH` when the calc version
      changed; :data:`REASON_RESETTLED_ZH` when no claimed change is on or before *through*.

    ``source`` is a machine reference, never shown: the named entries' ``table:key`` (an import
    batch ``import_batch:<id>``), comma-joined, or ``None``."""
    sizes: dict[str, int] = {}
    for c in changes:
        if c.import_batch_id is not None and c.op in ("insert", "delete"):
            sizes[c.import_batch_id] = sizes.get(c.import_batch_id, 0) + 1
    entries: dict[tuple[str, str, str], tuple[date, list[str]]] = {}
    for c in changes:
        if c.day > through:
            continue
        batch = c.import_batch_id
        if batch is not None and c.op in ("insert", "delete") and sizes[batch] >= 2:
            key = ("batch", batch, "")
        else:
            key = ("counter" if c.is_counter else "record", c.table, c.row_key)
        if key not in entries:
            entries[key] = (c.day, [])
        tables = entries[key][1]
        if c.table not in tables:
            tables.append(c.table)
    order = list(CHANGE_TABLE_ZH)

    def label(table: str) -> str:
        return CHANGE_TABLE_ZH.get(table, table)

    texts: list[str] = []
    refs: list[str] = []
    for (kind, a, b), (day, tables) in entries.items():
        if kind == "batch":
            joined = "／".join(label(t) for t in sorted(
                tables, key=lambda t: (order.index(t) if t in order else len(order), t)))
            text, ref = f"{day.isoformat()} 匯入批次（{joined}）", f"import_batch:{a}"
        elif kind == "counter":
            text, ref = f"{day.isoformat()} {label(a)}（{b}）", f"{a}:{b}"
        else:
            text, ref = f"{day.isoformat()} {label(a)}", f"{a}:{b}"
        if text not in texts and len(texts) < REASON_NAMED_MAX:
            texts.append(text)
            refs.append(ref)
    source = ",".join(refs) if refs else None
    if calc_changed:
        return REASON_CALC_ZH, source
    if not entries:
        return REASON_RESETTLED_ZH, None
    more = f"等 {len(entries)} 筆" if len(entries) > len(texts) else ""
    return f"{'、'.join(texts)}{more}更正後重新結算", source


def _changes_phrase(tables: Sequence[Any]) -> str:
    """「2 筆交易與 14 筆價格變更」 from a change summary's per-table lines."""
    parts = [f"{t.changes} 筆{t.label}" for t in tables if t.changes]
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else "、".join(parts[:-1]) + "與" + parts[-1]
    return f"{joined}變更"


def _write_json_atomic(path: Path, text: str) -> None:
    """Replace *path* with *text* (temp file + ``os.replace``). On Windows a reader holding the
    file open makes the replace fail for an instant: retried briefly, then left to the next
    write — the progress file is a live view, never the record (the report is)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                return
            time.sleep(0.02)


# =============================================================================================
# ETA (§9.5)
# =============================================================================================


#: Phase 8 (預熱) runs in the WEB process after the report is frozen (§9.3 #8), so a frozen
#: report never leaves it 「待執行」: these are its details.
PREWARM_BY_THE_WEB = "今天的結果由網站在背景另行預熱"
PREWARM_SKIPPED_SUPERSEDED = "本次未切換版本，無需預熱"
PREWARM_SKIPPED_UNPUBLISHED = "本次未生效，無需預熱"


class _Eta:
    """Each phase's expected duration: the last successful run's duration for the phase,
    scaled by its unit count; the 2026-10-18 profile on a store's first run; during 逐日估值,
    after ≥ 3 s, its smoothed measured rate."""

    def __init__(self, last: RunReport | None) -> None:
        self.last_at: datetime | None = None
        self.per_unit: dict[str, float] = dict(_PROFILE_S_PER_UNIT)
        self.from_last = False
        if last is not None:
            for step in last.steps:
                if step.status != "done" or step.elapsed_s is None:
                    continue
                units = step.total if step.total else 1
                self.per_unit[step.key] = step.elapsed_s / units
                self.from_last = True
            if self.from_last:
                self.last_at = last.summary.finished_at or last.summary.started_at
        self.rate: float | None = None  # smoothed 逐日估值 days per second

    def expected(self, key: str, units: int | None) -> float:
        if key == "prewarm":
            return 0.0
        if key == "valuate" and self.rate:
            return (units or 0) / self.rate
        return self.per_unit.get(key, 0.1) * max(units or 1, 1)

    def observe_rate(self, days_per_s: float) -> None:
        self.rate = days_per_s if self.rate is None else 0.7 * self.rate + 0.3 * days_per_s

    def basis(self) -> str:
        if self.from_last and self.last_at is not None:
            others = f"上次結算（{self.last_at.astimezone(app_now().tzinfo):%m-%d %H:%M}）的耗時"
        else:
            others = None
        if self.rate is not None:
            rest = others if others else "2026-10-18 實測的各階段比例"
            return f"逐日估值實測速度 {self.rate:.0f} 天/秒；其餘階段依{rest}"
        return f"依{others}" if others else _PROFILE_BASIS_ZH


# =============================================================================================
# Progress file + report (§9.5, §9.6)
# =============================================================================================


class _Tracker:
    """The run's live view (the progress file) and its record (the report), kept together:
    every phase change writes both, a tick writes the file at most every 0.5 s."""

    def __init__(self, conn: sqlite3.Connection, report: RunReport, path: Path,
                 eta: _Eta, priority: str | None) -> None:
        self.conn = conn
        self.report = report
        self.path = path
        self.eta = eta
        s = report.summary
        self.progress = RunProgress(
            run_id=s.run_id, pid=os.getpid(), heartbeat=_now(), status="preparing",
            reason=s.reason, started_by=s.started_by, trigger_text=s.trigger_text,
            started_at=s.started_at, priority=priority, eta_initial_s=s.eta_initial_s,
            phases=[RunPhase(key=k, label=label) for k, label in PHASES])
        self.units: dict[str, int | None] = {k: None for k, _ in PHASES}
        self._last_write = 0.0
        self._phase_t0: dict[str, float] = {}

    # --- the phases -----------------------------------------------------------------------
    def phase(self, key: str) -> RunPhase:
        return next(p for p in self.progress.phases if p.key == key)

    def _set(self, key: str, **update: Any) -> None:
        phases = [p.model_copy(update=update) if p.key == key else p
                  for p in self.progress.phases]
        self.progress = self.progress.model_copy(update={"phases": phases})

    def start(self, key: str, *, total: int | None = None, unit: str | None = None) -> None:
        self._phase_t0[key] = time.monotonic()
        self.units[key] = total
        self._set(key, status="running", started_at=_now(), total=total, unit=unit,
                  done=0 if total is not None else None)
        self.write(force=True)

    def tick(self, key: str, done: int, *, detail: str | None = None,
             rate: str | None = None) -> None:
        update: dict[str, Any] = {"done": done, "elapsed_s": self._elapsed(key)}
        if detail is not None:
            update["detail"] = detail
        if rate is not None:
            update["rate"] = rate
        self._set(key, **update)
        self.write()

    def finish(self, key: str, detail: str, *, done: int | None = None,
               total: int | None = None) -> None:
        update: dict[str, Any] = {"status": "done", "finished_at": _now(),
                                  "elapsed_s": self._elapsed(key), "detail": detail}
        if total is not None:
            update["total"] = total
            self.units[key] = total
        if done is not None:
            update["done"] = done
        self._set(key, **update)
        self.write(force=True)
        self.save()

    def skip(self, key: str, detail: str) -> None:
        self._set(key, status="skipped", detail=detail)
        self.write(force=True)

    def fail(self, key: str, detail: str) -> None:
        self._set(key, status="failed", finished_at=_now(), elapsed_s=self._elapsed(key),
                  detail=detail)

    def _elapsed(self, key: str) -> float | None:
        t0 = self._phase_t0.get(key)
        return None if t0 is None else round(time.monotonic() - t0, 3)

    # --- estimates ----------------------------------------------------------------------
    def _estimate(self) -> tuple[int, float | None]:
        total = done = 0.0
        for p in self.progress.phases:
            if p.key == "prewarm":
                continue
            units = self.units.get(p.key)
            expected = self.eta.expected(p.key, units)
            if p.status in ("done", "skipped"):
                spent = p.elapsed_s or 0.0
                total += spent
                done += spent
            elif p.status == "running":
                fraction = (p.done or 0) / p.total if p.total else 0.0
                elapsed = p.elapsed_s or 0.0
                left = max(expected * (1.0 - fraction), 0.0)
                if p.key == "valuate" and self.eta.rate and p.total:
                    left = max(p.total - (p.done or 0), 0) / self.eta.rate
                total += elapsed + left
                done += elapsed
            else:
                total += expected
        if total <= 0:
            return 0, None
        return min(99, int(100 * done / total)), max(total - done, 0.0)

    def write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_INTERVAL_S:
            return
        self._last_write = now
        percent, eta_s = self._estimate()
        when = _now()
        update: dict[str, Any] = {
            "heartbeat": when, "percent": percent, "eta_s": eta_s,
            "eta_basis": self.eta.basis(),
            "elapsed_s": round(max((when - self.progress.started_at).total_seconds(), 0.0), 3),
        }
        if self.progress.eta_initial_s is None and self.progress.status == "running":
            update["eta_initial_s"] = eta_s
        self.progress = self.progress.model_copy(update=update)
        _write_json_atomic(self.path, self.progress.model_dump_json(by_alias=True))

    def save(self) -> None:
        """The report as it stands (``update_run``): the steps, the range and versions."""
        self.report = self.report.model_copy(update={
            "steps": [p.model_copy() for p in self.progress.phases],
            "range": self.progress.range, "versions": self.progress.versions,
            "changes": self.progress.changes, "validation": self.progress.validation,
            "summary": self.report.summary.model_copy(update={
                "eta_initial_s": self.report.summary.eta_initial_s
                if self.report.summary.eta_initial_s is not None
                else self.progress.eta_initial_s}),
        })
        st.update_run(self.conn, self.report.summary.run_id, report=self.report)


# =============================================================================================
# Phase 1 — what one read transaction loads
# =============================================================================================


@dataclass
class _Loaded:
    """Everything the run reads, from ONE read transaction (§9.3 phase 1). *read* keeps that
    transaction open until the closes and the benchmark state are done, so the closes' price
    reads (``OnOrBefore``) and *cache* see the same snapshot."""

    read: sqlite3.Connection
    cache: FxSeriesCache
    accounts: list[Account]
    seq: int
    raw: LedgerBundle
    registered: LedgerBundle
    cut: LedgerBundle
    movements_all: list[StoredCashMovement]
    movements: list[StoredCashMovement]
    conversions: list[StoredFxConversion]
    transactions: list[StoredTransaction]
    dividends: list[StoredDividend]
    first_day: date | None
    #: The ledger's first row counting by the run's last day (any ledger: trades, dividends by
    #: their effective date, openings, corporate actions, cash movements, FX conversions) —
    #: where the first period's activity starts.
    first_row: date | None = None
    price_history: PriceHistory = field(default_factory=dict)
    fx_history: FxHistory = field(default_factory=dict)
    prices_read: int = 0
    fx_rows_read: int = 0


def _first_row(cut: LedgerBundle, movements: Sequence[StoredCashMovement],
               conversions: Sequence[StoredFxConversion], through: date) -> date | None:
    """The first day any ledger row of the run counts on (the first period's start)."""
    days = ([t.trade_date for t in cut.transactions]
            + [d.effective_date for d in cut.dividends]
            + [o.build_date for o in cut.opening] + [a.date for a in cut.actions]
            + [m.date for m in movements]
            + [c.date for c in conversions if c.date <= through])
    return min(days) if days else None


def _histories(conn: sqlite3.Connection, cache: FxSeriesCache, cut: LedgerBundle,
               movements: Sequence[StoredCashMovement], first_day: date, through: date,
               reporting: Currency) -> tuple[PriceHistory, FxHistory, int, int]:
    """The trend's price and FX histories exactly as ``_build_dashboard`` step 9 reads them:
    every symbol of the ledger and BOTH ends of every corporate action (``trend_symbols``),
    from the last close on or before the first day (never before 1900); FX for the trend's
    pair set (``trend_fx_currencies``: the quote currencies and the currency of every cash
    movement the trend converts — PERF-F2), both directions. *movements* are the run's, cut
    at *through*."""
    symbols = trend_symbols(cut)
    prices: PriceHistory = {sym: bounded_closes(conn, sym, first_day, through, floor=_EPOCH)
                            for sym in symbols}
    fx: FxHistory = {}
    for ccy in trend_fx_currencies(cut, movements):
        if ccy == reporting:
            continue
        for base, quote in ((ccy, reporting), (reporting, ccy)):
            rows = cache.history(base, quote, _EPOCH, through)
            if rows:
                fx[(base, quote)] = [(r.as_of, r.rate) for r in rows]
    return (prices, fx, sum(len(v) for v in prices.values()),
            sum(len(v) for v in fx.values()))


def _cash_fx(cache: FxSeriesCache, ccys: set[Currency], reporting: Currency,
             through: date) -> FxHistory:
    out: FxHistory = {}
    for ccy in ccys:
        if ccy == reporting:
            continue
        for base, quote in ((ccy, reporting), (reporting, ccy)):
            rows = cache.history(base, quote, _EPOCH, through)
            if rows:
                out[(base, quote)] = [(r.as_of, r.rate) for r in rows]
    return out


def _unavailable_from(flow: UnconvertibleFlow | None, first: date) -> date | None:
    """The first day ``U`` whose prefix ledger has a flow without an on-or-before rate: the
    date of the first unconvertible flow (``timeseries.first_unconvertible_flow``, the ONE
    owner), never before the trend's first day — a cost movement may precede the first trade,
    and the series starts there. Every prefix cut on or after it holds that flow; none before
    it holds any unconvertible flow."""
    return None if flow is None else max(flow.on, first)


def _bench_state(conn: sqlite3.Connection, cut: LedgerBundle, flows: Sequence[ReportingFlow],
                 fx: FxHistory, reporting: Currency, through: date) -> dict[str, Any] | None:
    """The benchmark counterfactual's accumulation through *through* (§9.3 phase 6), with
    ``_benchmark_comparison``'s loads: each market's closes from the last one on or before its
    first flow (never before 1990), re-expressed into *through*'s terms, converted at the
    trend's FX. ``None`` when the state has no lossless canonical text."""
    actions = ActionIndex.build(cut.actions)
    first_flow: dict[Market, date] = {}
    for f in flows:
        if f.market is not None and (f.market not in first_flow or f.on < first_flow[f.market]):
            first_flow[f.market] = f.on
    closes_by_market: dict[Market, list[tuple[date, Decimal]]] = {}
    for market in sorted(first_flow, key=lambda m: m.value):
        bench = benchmark_for_market(market)
        if bench is None:
            continue
        raw = closes_in(actions, bench.storage_key,
                        bounded_closes(conn, bench.storage_key, first_flow[market], through,
                                       floor=_BENCH_EPOCH),
                        valued_on=through)
        converted = convert_closes(raw, fx, bench.quote_ccy, reporting)
        if converted:
            closes_by_market[market] = converted
    try:
        return accumulate(flows, closes_by_market).to_json()
    except ValueError:
        return None


# =============================================================================================
# The run
# =============================================================================================


class _Failure(Exception):
    """A phase failed: *reason* is the zh sentence the report shows; *error_class* the class
    of the exception behind it, when one is (a refused swap: ``SwapRefused``)."""

    def __init__(self, phase: str, reason: str, *, error_class: str | None = None) -> None:
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.error_class = error_class


_DIFF_FIELDS: tuple[str, ...] = ("total_value", "net_invested", "net_worth")


def is_placeholder(day: st.SettledDay) -> bool:
    """A stored day of a series unavailable from it on (see the module docstring): the only
    rows written with ``cash_total`` NULL — a valued day always stores its cash (0 without
    any)."""
    return day.cash_total is None


def _flags(day: st.SettledDay) -> dict[str, bool]:
    return {"incomplete": day.incomplete, "cash_incomplete": day.cash_incomplete,
            "options_open": day.options_open}


def _diff_rows(run_id: int, old: dict[date, st.SettledDay], new: dict[date, st.SettledDay]
               ) -> list[DiffRow]:
    """Every changed day of the published range (§9.6 Result — history), compared as stored
    TEXT, so an exponent change is a change and an equal value is never reported."""
    rows: list[DiffRow] = []
    for day in sorted(set(old) | set(new)):
        a, b = old.get(day), new.get(day)
        # a placeholder is "no value": never a figure, never a settled (new / dropped) day
        a_value = a is not None and not a.placeholder
        b_value = b is not None and not b.placeholder
        if a is None and b is not None:
            if b_value:
                rows.append(DiffRow(run_id=run_id, day=day, field="total_value",
                                    kind="new_day", after=b.total_value, flags_after=_flags(b)))
            continue
        if b is None and a is not None:
            if a_value:
                rows.append(DiffRow(run_id=run_id, day=day, field="total_value",
                                    kind="dropped_day", before=a.total_value,
                                    flags_before=_flags(a)))
            continue
        assert a is not None and b is not None
        if not a_value and not b_value:
            continue
        for name in _DIFF_FIELDS:
            before = getattr(a, name) if a_value else None
            after = getattr(b, name) if b_value else None
            if _text(before) == _text(after):
                continue
            kind = ("value_to_none" if after is None else "none_to_value" if before is None
                    else "changed")
            rows.append(DiffRow.model_validate({
                "run_id": run_id, "day": day, "field": name, "kind": kind, "before": before,
                "after": after, "flags_before": _flags(a), "flags_after": _flags(b)}))
        if a_value and b_value and a.incomplete != b.incomplete:
            rows.append(DiffRow(run_id=run_id, day=day, field="incomplete", kind="flag_flip",
                                before=Decimal(int(a.incomplete)),
                                after=Decimal(int(b.incomplete)),
                                flags_before=_flags(a), flags_after=_flags(b)))
    return rows


#: A close's KPI fields a restatement names: (field, zh label, unit — None = reporting).
_RESTATED: tuple[tuple[str, str, str | None], ...] = (
    ("total_market_value", "總市值", None),
    ("total_return", "累計總報酬", None),
    ("total_return_rate", "累計報酬率", "ratio"),
    ("xirr", "累計 XIRR", "ratio"),
    ("period_twr", "當期報酬（TWR）", "ratio"),
    ("trend_total_value", "證券市值", None),
    ("net_invested", "淨投入", None),
    ("net_worth", "淨資產", None),
)


def _kpi_values(p: st.SettledPeriod, reporting: Currency
                ) -> dict[str, tuple[str, str, Decimal | None]]:
    """field → (label, unit, value) of a close's restatable KPI figures."""
    out: dict[str, tuple[str, str, Decimal | None]] = {}
    for name, label, unit in _RESTATED:
        out[name] = (label, unit or reporting.value, getattr(p.kpi, name))
    for ccy, value in p.kpi.by_currency_value.items():
        out[f"by_currency_value.{ccy.value}"] = (f"{ccy.value} 市值", ccy.value, value)
    return out


@dataclass
class _Close:
    period: st.SettledPeriod
    holdings: list[Any]
    cash: list[Any]


class _Run:
    """One run, phase by phase (§9.3). Every write goes through *conn* (the write
    connection); the read snapshot is a second connection on the same file."""

    def __init__(self, conn: sqlite3.Connection, db_path: Path, data: Path, run_id: int,
                 reporting: Currency, priority: str | None) -> None:
        self.conn = conn
        self.db_path = db_path
        self.run_id = run_id
        self.reporting = reporting
        self.calc = st.calc_version(reporting)
        report = st.get_run(conn, run_id)
        if report is None:
            raise LookupError(f"run {run_id} has no report")
        self.request: RunRequest = report.inputs
        last = st.last_successful_run(conn, before=run_id)
        self.tracker = _Tracker(conn, report, data / PROGRESS_FILE, _Eta(last), priority)
        self.claim: st.Claim | None = None
        self.lines: list[str] = []
        self.read: sqlite3.Connection | None = None
        self.run_from: date | None = None
        self.close_first: date | None = None
        #: Placeholder days this run adds that the previous generation did not hold.
        self.new_placeholders: list[date] = []
        #: The swap committed: from here on nothing ends the run as failed.
        self.swapped = False

    # ------------------------------------------------------------------------------------
    def execute(self) -> RunStatus:
        self.tracker.write(force=True)
        current = ""
        try:
            try:
                self._claim()
            except st.ClaimRefused as exc:
                stored = st.get_run(self.conn, self.run_id)
                if stored is not None and stored.summary.status in st.END_STATUSES:
                    return stored.summary.status  # already ended (a cancel before the start)
                # its text is English; the sentence is derived from the state
                return self._end_failed(None, self._refusal_text(),
                                        error_class=type(exc).__name__)
            current = "load"
            loaded = self._load()
            current = "check"
            review = self._check(loaded)
            current = "replay"
            plan = self._plan(loaded, review)
            self.run_from = plan.run_from
            self.close_first = plan.close_first
            current = "valuate"
            series = self._valuate(loaded, review, plan)
            current = "close"
            closes = self._close(loaded, plan, series)
            current = "bench"
            bench = self._bench(loaded, series)
            self._release_snapshot()
            current = "swap"
            return self._swap(loaded, review, series, closes, bench)
        except _Failure as exc:
            if self.swapped:  # pragma: no cover - _after_swap catches its own faults
                return self._stored_status()
            return self._end_failed(exc.phase, exc.reason, error_class=exc.error_class)
        except UnbookableLedgerError as exc:
            if self.swapped:  # pragma: no cover - _after_swap catches its own faults
                return self._stored_status()
            return self._end_failed(current or None, str(exc),
                                    error_class=type(exc).__name__)
        except Exception as exc:  # every other fault ends the run, named, never silently
            if self.swapped:  # pragma: no cover - _after_swap catches its own faults
                return self._stored_status()
            label = PHASE_LABELS.get(current, "開始結算")
            return self._end_failed(current or None,
                                    f"{label}時發生錯誤（{type(exc).__name__}）",
                                    error_class=type(exc).__name__)
        finally:
            self._release_snapshot()

    def _stored_status(self) -> RunStatus:
        stored = st.get_run(self.conn, self.run_id)
        return "published" if stored is None else stored.summary.status

    def _release_snapshot(self) -> None:
        """End the read transaction (the closes and the benchmark state are done)."""
        if self.read is not None:
            with contextlib.suppress(sqlite3.Error):
                self.read.rollback()
            self.read.close()
            self.read = None

    # ------------------------------------------------------------------------------------
    def _claim(self) -> None:
        req = self.request
        claim = st.claim(self.conn, self.run_id, scope=req.scope, run_through=req.through)
        self.claim = claim
        state = st.store_state(self.conn, reporting=self.reporting,
                               expected_calc_version=self.calc)
        page = st.run_changes(self.conn, self.run_id, limit=0)
        attempts = st.claimed_attempts(self.conn, self.run_id)
        changes = RunChanges(tables=page.tables, records_total=page.total_count,
                             records_url=f"/api/settlement/changes?state=run:{self.run_id}",
                             attempted_by=attempts.run_ids)
        report = st.get_run(self.conn, self.run_id)
        assert report is not None
        self.tracker.report = report.model_copy(update={"inputs": req})
        prev = report.summary.previous_run_id
        if prev is not None:
            prev_report = st.get_run(self.conn, prev)
            if (prev_report is not None and prev_report.summary.status == "partial"
                    and prev_report.range.handed_from is not None):
                self.lines.append(f"延續 #{prev} 的變更於 "
                                  f"{prev_report.range.handed_from.isoformat()} 起的影響")
        if attempts.run_ids:
            ids = "、".join(f"#{i}" for i in attempts.run_ids)
            self.lines.append(f"其中 {attempts.changes} 筆變更曾於 {ids} 嘗試")
        on_cancel = self._consequence(state, changes)
        progress = self.tracker.progress.model_copy(update={
            "status": "running", "range": report.range, "changes": changes,
            "versions": report.versions.model_copy(update={
                "calc_version": self.calc, "reporting_ccy": self.reporting}),
            "on_cancel": on_cancel})
        self.tracker.progress = progress
        self.tracker.write(force=True)
        self.tracker.save()

    def _refusal_text(self) -> str:
        """Why the claim was refused, in a zh sentence read off the state (the store's
        ``ClaimRefused`` text is English, and it reaches ``job_runs.detail``, DEF-073)."""
        state = st.store_state(self.conn, reporting=self.reporting)
        if state.run_id is not None and state.run_id != self.run_id:
            return f"另一次重新結算（#{state.run_id}）仍在進行，本次未執行"
        report = st.get_run(self.conn, self.run_id)
        if report is None:
            return "找不到這次重新結算的報告，本次未執行"
        if report.summary.status != "preparing":
            return (f"這次重新結算的狀態已是「{report.summary.status_text}」，"
                    "不在準備中，本次未執行")
        return "結算資料表尚未建立完成，本次未執行"

    def _consequence(self, state: st.StoreState, changes: RunChanges) -> str:
        """What stays if this run does not publish (§9.5 ``on_cancel``), from the state."""
        if state.generation == 0 or state.settled_through is None:
            return "目前尚無結算版本，歷史照常即時計算"
        if not state.calc_ok:
            return "目前沒有可用的結算版本（計算規則已更新），歷史照常即時計算"
        if not state.currency_ok:
            return "目前沒有可用的結算版本（報告幣別已變更），歷史照常即時計算"
        kept = f"保留第 {state.generation} 版（結算至 {state.settled_through.isoformat()}）"
        phrase = _changes_phrase(changes.tables)
        if not phrase:
            return f"{kept}，今天這段照常即時計算"
        return f"{kept}；這 {phrase}回到待結算"

    # ------------------------------------------------------------------------------------
    def _load(self) -> _Loaded:
        t = self.tracker
        t.start("load")
        assert self.claim is not None
        through = self.claim.run_through
        read = connect_path(self.db_path)
        self.read = read
        read.execute("BEGIN")  # one read transaction: every read below sees one snapshot
        seq = int(read.execute("SELECT seq FROM settle_dirty WHERE id = 1").fetchone()[0])
        transactions = list_transactions(read)
        dividends = list_dividends(read)
        raw = load_ledger_bundle(read, transactions=transactions, dividends=dividends)
        registered = raw.without_unregistered() if raw.unregistered_symbols else raw
        cut = registered.valued_as_of(through)
        movements_all = list_cash_movements(read)
        loaded = _Loaded(
            read=read, cache=FxSeriesCache(read), accounts=list_accounts(read),
            seq=seq, raw=raw, registered=registered, cut=cut, movements_all=movements_all,
            movements=valued_rows_as_of(movements_all, through),
            conversions=list_fx_conversions(read), transactions=transactions,
            dividends=dividends, first_day=trend_start(cut))
        loaded.first_row = _first_row(cut, loaded.movements, loaded.conversions, through)
        if loaded.first_day is not None and loaded.first_day <= through:
            (loaded.price_history, loaded.fx_history, loaded.prices_read,
             loaded.fx_rows_read) = _histories(read, loaded.cache, cut, loaded.movements,
                                               loaded.first_day, through, self.reporting)
        symbols = len(loaded.price_history)
        t.finish("load", f"{symbols} 檔 · 價格 {loaded.prices_read:,} 筆 · "
                         f"匯率 {len(loaded.fx_history)} 組 {loaded.fx_rows_read:,} 筆",
                 done=symbols, total=symbols)
        return loaded

    # ------------------------------------------------------------------------------------
    def _check(self, loaded: _Loaded) -> LedgerReview:
        t = self.tracker
        t.start("check")
        assert self.claim is not None
        review = review_ledger(loaded.raw.valued_as_of(self.claim.run_through))
        found = len(review.findings)
        tail = (f"發現 {found} 項待釐清（以待釐清處理，結算照常進行）" if found
                else "未發現待釐清項目")
        t.progress = t.progress.model_copy(update={
            "validation": RunValidation(status="done", findings=review.findings)})
        t.finish("check", f"重播 {review.events:,} 筆事件，{tail}", done=found, total=found)
        return review

    # ------------------------------------------------------------------------------------
    def _plan(self, loaded: _Loaded, review: LedgerReview) -> _Plan:
        t = self.tracker
        t.start("replay")
        assert self.claim is not None
        claim = self.claim
        first = loaded.first_day
        through = claim.run_through
        plan = _Plan()
        if first is not None and first <= through:
            # this run's own U: the first day an unconvertible flow counts (None: available),
            # and the flow itself — the sentence every close from U on reads
            plan.unconvertible = first_unconvertible_flow(
                loaded.cut, loaded.fx_history, self.reporting,
                cash_movements=loaded.movements,
                refused_dividends=review.book.refused_dividends)
            plan.unavailable_from = _unavailable_from(plan.unconvertible, first)
            # the closes start at the month of the ledger's FIRST ROW (a month funded before
            # its first trade has its close: its cash, its activity, no trend point)
            plan.close_first = (first if loaded.first_row is None
                                else min(first, loaded.first_row))
        start = self._run_start(claim, plan)
        plan.run_from = start
        if first is not None and start is not None:
            plan.value_start = max(start, first)
        if plan.value_start is not None and plan.value_start <= through and first is not None:
            assert plan.close_first is not None and start is not None
            plan.periods = [(k, p, e) for k, p, e in st.period_ends(plan.close_first, through)
                            if e >= start]
            book_days = trend_book_days(loaded.cut, start=plan.value_start, end=through)
            plan.days = sorted(set(book_days) | {e for _k, _p, e in plan.periods})
        events = (len(loaded.cut.transactions) + len(loaded.cut.dividends)
                  + len(loaded.cut.opening) + len(loaded.cut.actions)
                  + len(loaded.cut.unreadable_actions))
        t.finish("replay", f"{events:,} 筆事件 · {len(plan.days)} 個帳本日，"
                           "逐日估值時一次重播", done=events, total=events)
        return plan

    def _run_start(self, claim: st.Claim, plan: _Plan) -> date | None:
        """The run's first day: the claim's, widened when the published generation cannot be
        kept up to it (see the module docstring):

        - a full run (and the self-check, and generation 0) starts at the ledger's first row
          when that precedes the claim's day (the closes start there);
        - the published generation's first PLACEHOLDER day P, when the condition that made it
          CHANGED: this run's U is not P (U absent — the series is available — or another
          day): from ``min(P, U)``. With U = P the placeholders stand and the run starts at
          its claimed day (a nightly run on a ledger unavailable since U settles one day);
        - the first close the rule expects, when the published generation lacks it (a store
          built by an earlier rule).

        The range (``settle_run.range_from`` / ``days``, the report's ``range``) says so."""
        start = claim.run_from
        close_first = plan.close_first
        if start is None:
            return None
        if claim.scope in ("full", "selfcheck") or claim.generation == 0:
            if close_first is not None and close_first < start:
                self._widen(claim, close_first, None)
                return close_first
            return start
        widened = start
        line: str | None = None
        first_placeholder = st.first_placeholder_day(self.conn, claim.generation)
        u = plan.unavailable_from
        if first_placeholder is not None and first_placeholder < start and u != first_placeholder:
            widened = first_placeholder if u is None else min(first_placeholder, u)
            line = (f"前一版自 {first_placeholder.isoformat()} 起沒有走勢資料，"
                    "其原因已改變：本次自該日起一併重新估值")
        if close_first is not None and close_first < widened and not self._holds_closes(
                claim.generation, close_first, widened):
            widened = close_first
            line = (f"前一版缺少 {close_first.isoformat()} 起的月結與年結（由較早的規則建立）："
                    "本次自該日起一併重新結算")
        if widened < start:
            self._widen(claim, widened, line)
        return widened

    def _holds_closes(self, generation: int, first: date, before: date) -> bool:
        """The generation holds every close the rule expects ending in ``[first, before)``."""
        expected = {(k, p) for k, p, e in st.period_ends(first, before - _ONE_DAY)}
        held = {(c.kind, c.period) for c in st.read_periods(self.conn, generation,
                                                           end=before - _ONE_DAY)}
        return expected <= held

    def _widen(self, claim: st.Claim, start: date, line: str | None) -> None:
        """Move the run's range to start at *start*: ``days`` is the calendar span (the
        report's one convention, :class:`RunRange`)."""
        days = max((claim.run_through - start).days + 1, 0)
        kept = (len(st.read_days(self.conn, claim.generation, end=start - _ONE_DAY))
                if claim.generation > 0 else 0)
        st.update_run(self.conn, self.run_id, range_from=start, days=days)
        t = self.tracker
        rng = t.progress.range.model_copy(update={
            "from_": start, "days": days, "kept_before": kept})
        t.progress = t.progress.model_copy(update={"range": rng})
        t.report = t.report.model_copy(update={"range": rng})
        if line is not None:
            self.lines.append(line)

    # ------------------------------------------------------------------------------------
    def _valuate(self, loaded: _Loaded, review: LedgerReview, plan: _Plan) -> _Series:
        t = self.tracker
        assert self.claim is not None
        claim = self.claim
        through = claim.run_through
        new_gen = claim.generation + 1
        series = _Series()
        if claim.generation > 0 and plan.run_from is not None:
            st.copy_before(self.conn, claim.generation, new_gen, plan.run_from)
        start = plan.value_start
        days_total = 0 if start is None or start > through else (through - start).days + 1
        rng = t.progress.range.model_copy(update={"valued_days": days_total})
        t.progress = t.progress.model_copy(update={"range": rng})
        t.report = t.report.model_copy(update={"range": rng})
        t.start("valuate", total=days_total, unit="天")
        first = loaded.first_day
        if start is None or start > through or first is None:
            t.finish("valuate", "沒有需要重新估值的日子", done=0, total=days_total)
            return series
        # the stored prefix (G's days before the start, copied into G + 1): for the options
        # flag before the start and for the full-series TWR index
        series.prefix = st.read_days(self.conn, new_gen, end=start - _ONE_DAY)
        options_before = any(d.options_open for d in series.prefix)
        if start == first:
            options_before = False
        refused = review.book.refused_dividends
        flows = build_reporting_flows(loaded.cut, loaded.fx_history, self.reporting,
                                      cash_movements=loaded.movements,
                                      refused_dividends=refused)
        series.flows = flows
        series.unavailable_from = plan.unavailable_from
        series.unavailable_reason = (None if plan.unconvertible is None
                                     else trend_unavailable_reason(plan.unconvertible))
        if (flows is None) != (plan.unavailable_from is not None):
            raise _Failure("valuate", "逐日估值時匯率可用性判斷不一致（內部錯誤）")
        trend_end = through if series.unavailable_from is None else (
            series.unavailable_from - _ONE_DAY)
        trend_bundle = loaded.cut if series.unavailable_from is None else (
            loaded.cut.valued_as_of(trend_end))
        trend_moves = valued_rows_as_of(loaded.movements, trend_end)
        # the cash series (net worth): the dashboard's rows, the BOOKED dividends only
        cash_divs = booked_dividends(loaded.dividends, refused)
        ccys = {ccy for _acct, ccy in cash_balances(
            loaded.movements, loaded.conversions, loaded.transactions, cash_divs,
            loaded.cut.instruments)}
        cash_fx = _cash_fx(loaded.cache, ccys, self.reporting, through)
        cash_by_date: dict[date, CashDay] = daily_cash_series(
            loaded.movements, loaded.conversions, loaded.transactions, cash_divs,
            loaded.cut.instruments, cash_fx, self.reporting, end=through, start=start)
        instruments = loaded.cut.instruments
        books = iter(zip(plan.days, iter_books(loaded.cut, plan.days), strict=True))
        pending = next(books, None)
        last: tuple[date, Book] | None = None
        period_ends = {e for _k, _p, e in plan.periods}
        done = 0
        t_start = time.monotonic()
        for ms, me in _month_chunks(start, through):
            chunk: list[tuple[date, Book]] = []
            while pending is not None and pending[0] <= me:
                if pending[0] in period_ends:
                    series.month_end_books[pending[0]] = pending[1]
                if pending[0] <= ms:
                    last = pending
                else:
                    chunk.append(pending)
                pending = next(books, None)
            pairs = ([last] if last is not None else []) + chunk
            open_on = self._options_open(pairs, ms, me, instruments)
            rows: list[st.SettledDay] = []
            hi = min(me, trend_end)
            if ms <= hi:
                trend = value_series(
                    trend_bundle, loaded.price_history, loaded.fx_history, self.reporting,
                    start=ms, end=hi, cash_movements=trend_moves, refused_dividends=refused,
                    books=pairs, options_open_before=options_before if ms > first else False)
                if not trend.available:
                    raise _Failure("valuate",
                                   "逐日估值時趨勢資料不可用（部分投入流量缺少當日匯率）")
                for p in compose_net_worth(trend, cash_by_date).points:
                    cash = cash_by_date.get(p.date)
                    rows.append(st.SettledDay(
                        day=p.date, total_value=p.total_value, net_invested=p.net_invested,
                        net_worth=p.net_worth,
                        cash_total=_ZERO if cash is None else cash.reporting_total,
                        cash_incomplete=False if cash is None else cash.incomplete,
                        incomplete=p.incomplete, options_open=open_on[p.date]))
                    series.points[p.date] = p
            day = max(ms, hi + _ONE_DAY)
            while day <= me:  # the unavailable tail: placeholders (see the module docstring)
                rows.append(st.SettledDay(
                    day=day, total_value=_ZERO, net_invested=_ZERO, net_worth=None,
                    cash_total=None, cash_incomplete=True, incomplete=True,
                    options_open=open_on[day]))
                day += _ONE_DAY
            for r in rows:
                series.options_open[r.day] = r.options_open
            options_before = options_before or any(r.options_open for r in rows)
            series.days.extend(rows)
            st.write_days(self.conn, new_gen, rows)
            if chunk or pairs:
                last = pairs[-1]
            done += len(rows)
            elapsed = time.monotonic() - t_start
            rate_text = None
            if elapsed >= 3.0 and done:
                self.tracker.eta.observe_rate(done / elapsed)
                rate_text = f"{done / elapsed:.0f} 天/秒"
            current = pairs[-1][1] if pairs else None
            if current is not None:
                t.progress = t.progress.model_copy(update={"current": RunCurrent(
                    day=me, positions=sum(1 for h in current.holdings if h.shares))})
            t.tick("valuate", done, rate=rate_text,
                   detail=f"已寫入 {start:%Y-%m} 至 {me:%Y-%m}")
        if pending is not None:
            raise _Failure("valuate", "逐日估值後仍有未使用的帳本日（內部錯誤）")
        t.progress = t.progress.model_copy(update={"current": None})
        months = len(_month_chunks(start, through))
        tail = ("" if series.unavailable_reason is None else
                f"；{series.unavailable_reason}")
        t.finish("valuate", f"估值 {done:,} 天，寫入 {start:%Y-%m} 至 {through:%Y-%m}"
                            f"（{months} 個月）{tail}", done=done, total=days_total)
        return series

    @staticmethod
    def _options_open(pairs: Sequence[tuple[date, Book]], lo: date, hi: date,
                      instruments: dict[str, Instrument]) -> dict[date, bool]:
        """Each day's ``options_open`` (``holds_open_option`` on that day's book)."""
        out: dict[date, bool] = {}
        flags = [(d, holds_open_option(b, instruments)) for d, b in pairs]
        i = -1
        day = lo
        while day <= hi:
            while i + 1 < len(flags) and flags[i + 1][0] <= day:
                i += 1
            out[day] = flags[i][1] if i >= 0 else False
            day += _ONE_DAY
        return out

    # ------------------------------------------------------------------------------------
    def _close(self, loaded: _Loaded, plan: _Plan, series: _Series) -> list[_Close]:
        t = self.tracker
        assert self.claim is not None
        claim = self.claim
        new_gen = claim.generation + 1
        t.start("close", total=len(plan.periods), unit="個期間")
        if not plan.periods:
            t.finish("close", "此範圍內沒有月底或年底", done=0, total=0)
            return []
        # the full-series TWR index: the stored prefix and this run's days
        composed: list[TrendPoint] = [
            TrendPoint(date=d.day, total_value=d.total_value, net_invested=d.net_invested,
                       incomplete=d.incomplete, net_worth=d.net_worth)
            for d in series.prefix if not is_placeholder(d)]
        composed += [series.points[d] for d in sorted(series.points)]
        points = {p.date: p for p in composed}
        index_points = twr_index(composed)
        index = pc.TwrIndex(values={p.date: p.value for p in index_points},
                            anchor=index_points[0].date if index_points else None)
        options_open = {d.day: d.options_open for d in series.prefix}
        options_open.update(series.options_open)
        ledger = pc.CloseLedger(
            bundle=loaded.registered, movements=loaded.movements_all,
            conversions=loaded.conversions, transactions=loaded.transactions,
            dividends=loaded.dividends, accounts=loaded.accounts)
        rates = pc.own_date_rates(loaded.cache, self.reporting)
        first = loaded.first_day
        assert first is not None
        closes: list[_Close] = []
        kpi_cache: dict[date, pc.CloseKpi] = {}
        cash_cache: dict[date, list[PeriodCash]] = {}
        previous = ({(p.kind, p.period): p for p in st.read_periods(
            self.conn, claim.generation, start=plan.periods[0][2], end=plan.periods[-1][2])}
            if claim.generation > 0 else {})
        for n, (kind, period, end) in enumerate(plan.periods, start=1):
            book = series.month_end_books[end]
            prev = _prev_end(kind, end)
            start_end = prev if prev >= first else None
            lo = (start_end + _ONE_DAY) if start_end is not None else first
            excluded = any(options_open.get(lo + timedelta(days=i), False)
                           for i in range((end - lo).days + 1))
            if end not in kpi_cache:
                before = previous.get(("M", f"{end:%Y-%m}"))
                kpi_cache[end] = pc.kpi_at(
                    end, book=book, ledger=ledger,
                    conn_source=OnOrBefore(loaded.read, end, fx_cache=loaded.cache),
                    reporting=self.reporting, options_value_excluded=excluded,
                    unconfirmed=self.request.unconfirmed_closes,
                    carried=() if before is None else before.kpi.notes.unconfirmed_closes)
                cash_cache[end] = pc.cash_at(end, book=book, ledger=ledger,
                                             fx_cache=loaded.cache, reporting=self.reporting)
            ck = kpi_cache[end]
            cash = cash_cache[end]
            kpi = ck.kpi.model_copy(update={"notes": ck.kpi.notes.model_copy(update={
                "options_value_excluded": excluded,
                "missing_cash_fx": pc.missing_cash_fx(cash, self.reporting)})})
            if end < first:  # a close before the series: no trend point, no 當期報酬
                kpi = pc.with_trend(kpi, point=None, twr=None, twr_reason=pc.TWR_NOT_STARTED_ZH,
                                    trend_reason=pc.TREND_NOT_STARTED_ZH)
                if kpi.xirr is None:  # the close's own words (the engine's reason is unchanged)
                    kpi = kpi.model_copy(update={"xirr_reason": pc.XIRR_NO_POSITION_ZH})
            else:
                twr, why = pc.period_twr(index, points, start_end=start_end, end=end,
                                         unavailable=series.unavailable_reason)
                kpi = pc.with_trend(kpi, point=series.points.get(end), twr=twr,
                                    twr_reason=why, trend_reason=series.unavailable_reason)
            # every period is its own calendar period: the closes start at the month of the
            # ledger's first row, so no row is ever outside the period it is listed in
            act = pc.activity(start=pc.period_start(kind, end), end=end, book=book,
                              ledger=ledger, reporting=self.reporting, rate_at=rates)
            closes.append(_Close(
                period=st.SettledPeriod(kind=kind, period=period, end_day=end, kpi=kpi,
                                        activity=act),
                holdings=pc.holdings_at(ck.block), cash=cash))
            t.tick("close", n)
        st.write_periods(self.conn, new_gen, [c.period for c in closes])
        for c in closes:
            st.write_period_holdings(self.conn, new_gen, c.period.kind, c.period.period,
                                     c.holdings)
            st.write_period_cash(self.conn, new_gen, c.period.kind, c.period.period, c.cash)
        months = sum(1 for c in closes if c.period.kind == "M")
        years = len(closes) - months
        t.finish("close", f"月結 {months} 個、年結 {years} 個", done=len(closes),
                 total=len(closes))
        return closes

    # ------------------------------------------------------------------------------------
    def _bench(self, loaded: _Loaded, series: _Series) -> dict[str, Any] | None:
        t = self.tracker
        t.start("bench")
        assert self.claim is not None
        if series.flows is None or loaded.first_day is None:
            t.finish("bench", "沒有完整的投入流量（缺少當日匯率或尚無帳本），基準比較改為即時計算")
            return None
        state = _bench_state(loaded.read, loaded.cut, series.flows, loaded.fx_history,
                             self.reporting, self.claim.run_through)
        if state is None:
            t.finish("bench", "基準狀態無法無損保存，基準比較改為即時計算")
        else:
            t.finish("bench", f"已累計至 {self.claim.run_through.isoformat()}"
                              "（僅在完整發布時採用）")
        return state

    # ------------------------------------------------------------------------------------
    def _swap(self, loaded: _Loaded, review: LedgerReview, series: _Series,
              closes: list[_Close], bench: dict[str, Any] | None) -> RunStatus:
        t = self.tracker
        t.start("swap")
        assert self.claim is not None
        claim = self.claim
        old_gen = claim.generation
        new_gen = old_gen + 1
        state = st.store_state(self.conn, reporting=self.reporting,
                               expected_calc_version=self.calc)
        same_ccy = state.reporting_ccy == self.reporting
        lo = self.run_from
        hi = claim.run_through
        history = HistoryResult()
        restatement_ids: list[int] = []
        selfcheck: SelfcheckResult | None = None
        new_days = {d.day: d for d in series.days}
        if lo is not None and lo <= hi:
            if old_gen > 0 and not same_ccy:
                history = HistoryResult(
                    currency_changed=True,
                    note=(f"報告幣別由 {state.reporting_ccy.value if state.reporting_ccy else '—'}"
                          f" 改為 {self.reporting.value}：全部重算，不逐日比較前後值"))
            else:
                old_days = ({d.day: d for d in st.read_days(self.conn, old_gen, start=lo,
                                                            end=hi)}
                            if old_gen > 0 else {})
                st.write_diff(self.conn, self.run_id, _diff_rows(self.run_id, old_days,
                                                                 new_days))
                self.new_placeholders = sorted(
                    d for d, v in new_days.items() if v.placeholder and d not in old_days)
                note = "計算規則更新" if old_gen > 0 and not state.calc_ok else None
                common = sorted(set(old_days) & set(new_days))
                last_common = (None if not common else DayValue(
                    day=common[-1], before=old_days[common[-1]].net_worth,
                    after=new_days[common[-1]].net_worth))
                history = st.history_from_diff(
                    self.conn, self.run_id, base=HistoryResult(note=note),
                    last_common_day=last_common)
                drafts = self._restatements(old_gen, closes, lo, hi, state)
                restatement_ids = st.write_restatements(self.conn, self.run_id, drafts)
            if claim.scope == "selfcheck" and old_gen > 0:
                selfcheck = self._selfcheck(old_gen, new_gen, lo, hi)
        if loaded.first_day is None:
            self.lines.append("帳本尚無資料，無需結算")
        report = t.report.model_copy(update={
            "history": history, "restatement_ids": restatement_ids, "selfcheck": selfcheck,
            "summary_lines": list(self.lines),
            "summary": t.report.summary.model_copy(update={
                "selfcheck_diff": bool(selfcheck and selfcheck.differences)})})
        t.report = report
        t.save()
        try:
            result = st.swap(self.conn, self.run_id, calc_version=self.calc, bench_state=bench,
                             through=claim.run_through, first_day=loaded.first_day,
                             closes_from=self.close_first,
                             reporting=self.reporting)
        except st.SwapRefused as exc:
            if exc.reason == "incomplete":
                raise _Failure("swap", "切換前的完整性檢查未通過（新版本的逐日或月結列數不符）",
                               error_class=type(exc).__name__) from exc
            raise _Failure("swap", f"無法切換版本（{exc.reason}）",
                           error_class=type(exc).__name__) from exc
        self.swapped = True
        try:
            return self._after_swap(result, review)
        except Exception as exc:  # the swap committed: the run published, whatever follows
            return self._after_swap_failed(result, exc)

    def _after_swap(self, result: st.SwapResult, review: LedgerReview) -> RunStatus:
        """The report of a run whose swap committed: its lines from the PUBLISHED result (the
        swap trims a partial publish's history), its state, then ``finalise_run``."""
        t = self.tracker
        stored = st.get_run(self.conn, self.run_id)
        assert stored is not None
        lines = list(self.lines)
        history = stored.history if result.status != "superseded" else None
        valued = history.new_days if history is not None else 0
        blank = (0 if result.status == "superseded" else
                 sum(1 for d in self.new_placeholders
                     if result.settled_through is None or d <= result.settled_through))
        if valued:
            lines.append(f"新結算 {valued} 天")
        if blank:
            lines.append(f"新結算 {blank} 天（沒有走勢資料）" if not valued
                         else f"另有 {blank} 天沒有走勢資料")
        if history is not None and history.dropped_days:
            lines.append(f"移除 {history.dropped_days} 天")
        if result.status == "partial" and result.handed_from is not None:
            lines.append(f"結算期間有新的變更：已發布至 "
                         f"{(result.handed_from - _ONE_DAY).isoformat()}，"
                         f"{result.handed_from.isoformat()} 起由下一輪接續")
        if result.status == "superseded":
            lines.append("結算期間有更早的變更，本次併入下一輪")
            lines.append("本次未生效，已退回待結算")
            st.discard_run_results(self.conn, self.run_id)
            stored = stored.model_copy(update={"history": None, "restatement_ids": []})
        after = st.store_state(self.conn, reporting=self.reporting,
                               expected_calc_version=self.calc)
        today = _now().date()
        stale = st.stale_range(after, today)
        pending = [f for f in review.findings if f.kind != "unregistered_symbol"]
        state_result = StateResult(
            stale_cleared=stale is None,
            stale_reason=None if stale is None else
            f"歷史走勢與月度成績尚未包含 {stale[0].isoformat()} 起的變更",
            pending_positions=pending, chained_from=result.handed_from)
        changes = stored.changes.model_copy(update={
            "handed_back": result.status == "superseded"})
        final = stored.model_copy(update={
            "summary_lines": lines, "state": state_result, "changes": changes,
            "summary": self._final_summary(stored)})
        t.finish("swap", self._swap_detail(result))
        # phase 8 runs in the web process after this report is frozen: never left 「待執行」
        t.skip("prewarm", PREWARM_SKIPPED_SUPERSEDED if result.status == "superseded"
               else PREWARM_BY_THE_WEB)
        t.report = final.model_copy(update={"steps": [p.model_copy()
                                                      for p in t.progress.phases]})
        status = st.finalise_run(self.conn, self.run_id, result.status, t.report)
        self._final_progress(status)
        return status

    def _after_swap_failed(self, result: st.SwapResult, exc: Exception) -> RunStatus:
        """A fault after the swap committed: the generation is published (or the run already
        superseded) — the report says so with the fault noted, and nothing is raised."""
        status: RunStatus = result.status
        note = f"版本已切換，整理報告時發生錯誤（{type(exc).__name__}）"
        with contextlib.suppress(Exception):
            self.tracker.skip("prewarm", PREWARM_BY_THE_WEB)
        try:
            stored = st.get_run(self.conn, self.run_id) or self.tracker.report
            report = stored.model_copy(update={
                "summary_lines": [*self.lines, note],
                "steps": [p.model_copy() for p in self.tracker.progress.phases],
                "summary": self._final_summary(stored)})
            status = st.finalise_run(self.conn, self.run_id, result.status, report)
        except Exception:  # noqa: BLE001 — the published state stands; the record is best-effort
            logger.exception("settlement run %s: the report after the swap", self.run_id)
        with contextlib.suppress(Exception):
            self._final_progress(status)
        return status

    @staticmethod
    def _swap_detail(result: st.SwapResult) -> str:
        if result.status == "superseded":
            return "結算期間有更早的變更：本次未切換版本"
        through = result.settled_through.isoformat() if result.settled_through else "—"
        text = f"已切換至第 {result.generation} 版（結算至 {through}）"
        if result.status == "partial":
            text += "，其餘交由下一輪接續"
        return text

    def _restatements(self, old_gen: int, closes: list[_Close], lo: date, hi: date,
                      state: st.StoreState) -> list[st.RestatementDraft]:
        """The closes this run moved (§14.6): compared with the previous generation's close of
        the same period when both hold it (same reporting currency, inside the range), and —
        on a period's FIRST publish — with its retired month-end snapshot (``cutover``). Each
        close's reason names the claimed changes on or before ITS end day
        (:func:`correction_reason`)."""
        at = datetime.now(UTC)
        drafts: list[st.RestatementDraft] = []
        old = ({(p.kind, p.period): p for p in st.read_periods(self.conn, old_gen, start=lo,
                                                               end=hi)}
               if old_gen > 0 else {})
        claimed = st.claimed_changes(self.conn, self.run_id)
        calc_changed = old_gen > 0 and not state.calc_ok
        reasons: dict[date, tuple[str, str | None]] = {}
        snapshots: dict[str, st.SnapshotRow] | None = None
        for c in closes:
            new = c.period
            before = old.get((new.kind, new.period))
            if before is not None:
                if new.end_day not in reasons:
                    reasons[new.end_day] = correction_reason(claimed, new.end_day,
                                                             calc_changed=calc_changed)
                reason, source = reasons[new.end_day]
                a, b = _kpi_values(before, self.reporting), _kpi_values(new, self.reporting)
                for name in [*a, *(k for k in b if k not in a)]:
                    label, unit, _value = b[name] if name in b else a[name]
                    old_v = a[name][2] if name in a else None
                    new_v = b[name][2] if name in b else None
                    if not _moved(old_v, new_v, unit):
                        continue
                    drafts.append(st.RestatementDraft(
                        kind=new.kind, period=new.period, field=name, label=label, unit=unit,
                        old_value=old_v, new_value=new_v, reason=reason, at=at,
                        notes_before=before.kpi.notes.model_dump(mode="json"),
                        notes_after=new.kpi.notes.model_dump(mode="json"), source=source))
                continue
            if new.kind != "M":
                continue
            if snapshots is None:
                snapshots = st.cutover_snapshots(self.conn, self.reporting)
            snap = snapshots.get(new.period)
            # only a MONTH-END freeze: the nightly job rewrote the current month every
            # evening, so a row last written before the month's last day (the go-live month)
            # was never that month's close and restates nothing
            if snap is None or snap.as_of is None or snap.as_of.date() != new.end_day:
                continue
            k = new.kpi
            pairs: list[tuple[str, str, str, Decimal | None, Decimal | None]] = [
                ("total_market_value", "總市值", self.reporting.value, snap.total_value,
                 k.total_market_value),
                ("total_return", "累計總報酬", self.reporting.value, snap.total_return,
                 k.total_return),
                ("total_return_rate", "累計報酬率", "ratio", snap.total_return_rate,
                 k.total_return_rate),
                ("xirr", "累計 XIRR", "ratio", snap.xirr, k.xirr),
            ]
            new_ccy = {c_.value: v for c_, v in k.by_currency_value.items()}
            for ccy in sorted(set(snap.by_currency) | set(new_ccy)):
                pairs.append((f"by_currency_value.{ccy}", f"{ccy} 市值", ccy,
                              snap.by_currency.get(ccy), new_ccy.get(ccy)))
            for name, label, unit, old_v, new_v in pairs:
                # money by VALUE (the snapshot's text is ``decimal_str``'s); a ratio at
                # tolerance (``_moved``), as every other restatement
                if (not _moved(old_v, new_v, unit)) if unit == "ratio" else old_v == new_v:
                    continue
                drafts.append(st.RestatementDraft(
                    kind="M", period=new.period, field=name, label=label, unit=unit,
                    old_value=old_v, new_value=new_v, reason="上線重述：月底定格值改為月底結算值",
                    at=at, notes_before=None, notes_after=k.notes.model_dump(mode="json"),
                    source="portfolio_snapshots", cutover=True))
        return drafts

    def _selfcheck(self, old_gen: int, new_gen: int, lo: date, hi: date) -> SelfcheckResult:
        """Row by row, the published generation against this recompute over ``[lo, hi]``
        (the pending range is outside it by the claim): every settle table, the first
        difference by day."""
        old = st.generation_rows(self.conn, old_gen, start=lo, end=hi)
        new = st.generation_rows(self.conn, new_gen, start=lo, end=hi)
        differences = 0
        first: tuple[str, str] | None = None
        for table in ("settled_day", "settled_period", "settled_period_holding",
                      "settled_period_cash"):
            a, b = old[table], new[table]
            for key in set(a) | set(b):
                if a.get(key) == b.get(key):
                    continue
                differences += 1
                day = key.split("|", 1)[0]
                if first is None or day < first[0]:
                    first = (day, table)
        if differences:
            assert first is not None
            table = SETTLE_TABLE_ZH.get(first[1], "結算資料")
            self.lines.append(f"自檢：與第 {old_gen} 版逐列比對，發現 {differences} 處差異，"
                              f"最早於 {first[0]}（{table}）")
        else:
            self.lines.append(f"自檢：與第 {old_gen} 版逐列比對一致")
        return SelfcheckResult(
            compared_through=hi, differences=differences,
            first_day=None if first is None else date.fromisoformat(first[0]),
            first_table=None if first is None else first[1])

    # ------------------------------------------------------------------------------------
    def _end_failed(self, phase: str | None, reason: str, *,
                    error_class: str | None = None) -> RunStatus:
        """Every failure: the end transaction of §9.4 through ``finalise_run`` (put back,
        ``dirty_from`` restored, the generation above the published one deleted), the report
        naming the phase, the reason and the consequence derived from the state."""
        t = self.tracker
        if phase is not None:
            t.fail(phase, reason)
        state = st.store_state(self.conn, reporting=self.reporting,
                               expected_calc_version=self.calc)
        consequence = t.progress.on_cancel or self._consequence(state, t.progress.changes)
        st.discard_run_results(self.conn, self.run_id)
        report = t.report.model_copy(update={"history": None, "restatement_ids": []})
        failure = RunFailure(phase=PHASE_LABELS.get(phase, phase) if phase else None,
                             reason_text=reason, consequence=consequence,
                             error_class=error_class)
        lines = [*self.lines, "本次未生效，已退回待結算"]
        t.skip("prewarm", PREWARM_SKIPPED_UNPUBLISHED)
        report = report.model_copy(update={
            "failure": failure, "summary_lines": lines,
            "steps": [p.model_copy() for p in t.progress.phases],
            "changes": report.changes.model_copy(update={"handed_back": True}),
            "summary": self._final_summary(report)})
        t.report = report
        status = st.finalise_run(self.conn, self.run_id, "failed", report)
        self._final_progress(status)
        return status

    def _final_summary(self, report: RunReport) -> Any:
        """The summary at the end: when it finished, and the first estimate beside it."""
        summary = report.summary
        eta = (summary.eta_initial_s if summary.eta_initial_s is not None
               else self.tracker.progress.eta_initial_s)
        return summary.model_copy(update={"finished_at": _now(), "eta_initial_s": eta})

    def _final_progress(self, status: RunStatus) -> None:
        t = self.tracker
        t.progress = t.progress.model_copy(update={"status": status, "current": None,
                                                   "eta_s": 0.0})
        _write_json_atomic(t.path, t.progress.model_copy(update={
            "percent": 100 if status in ("published", "partial") else t.progress.percent,
            "heartbeat": _now()}).model_dump_json(by_alias=True))


@dataclass
class _Plan:
    """The days the run values and the periods it closes."""

    #: The run's first day (the claim's, widened — :meth:`_Run._run_start`).
    run_from: date | None = None
    #: This run's U: the first day an unconvertible flow counts (``None``: available).
    unavailable_from: date | None = None
    #: The first flow with no rate on or before its date (``None``: available).
    unconvertible: UnconvertibleFlow | None = None
    #: The first day the closes cover: the month of the ledger's first row.
    close_first: date | None = None
    value_start: date | None = None
    days: list[date] = field(default_factory=list)
    periods: list[tuple[Literal["M", "Y"], str, date]] = field(default_factory=list)


@dataclass
class _Series:
    """What 逐日估值 produced: the run's days (as written), its available trend points, the
    books of its period ends, the stored prefix, the flows and the unavailable tail."""

    days: list[st.SettledDay] = field(default_factory=list)
    points: dict[date, TrendPoint] = field(default_factory=dict)
    options_open: dict[date, bool] = field(default_factory=dict)
    month_end_books: dict[date, Book] = field(default_factory=dict)
    prefix: list[st.SettledDay] = field(default_factory=list)
    flows: list[ReportingFlow] | None = None
    unavailable_from: date | None = None
    #: Why a day from ``unavailable_from`` has no trend point (``None``: available) — the
    #: series' ONE sentence, ``timeseries.trend_unavailable_reason``.
    unavailable_reason: str | None = None


# =============================================================================================
# Entry points
# =============================================================================================


def _refuse_locked(conn: sqlite3.Connection, run_id: int) -> RunStatus | None:
    """The lock is held by another process: end THIS run's report only while it is still
    ``preparing`` and not the active run (never another child's run)."""
    report = st.get_run(conn, run_id)
    if report is None:
        return None
    state = st.store_state(conn, reporting=report.versions.reporting_ccy_before
                           or get_settings().reporting_currency)
    if report.summary.status != "preparing" or state.run_id == run_id:
        return None
    failure = RunFailure(reason_text="另一個結算程序仍持有結算鎖，本次未執行",
                         consequence="目前的結算版本不變，歷史照常計算")
    return st.finalise_run(conn, run_id, "failed", report.model_copy(update={
        "failure": failure, "summary_lines": ["本次未生效，已退回待結算"]}))


def run(run_id: int, *, db_path: Path, reporting: Currency | None = None,
        priority: str | None = None) -> RunStatus | None:
    """Execute run *run_id* against *db_path* — see the module docstring for the contract.

    Never renices (it may run in the web process: ``PD_SETTLE_MODE=inline``). *reporting*
    defaults to the configured reporting currency; *priority* is the words of a child that
    lowered its own priority (shown in the panel). Returns the stored end status, or ``None``
    when the run could not be touched (no report; the lock held by another live run).
    """
    db_path = Path(db_path)
    data = db_path.resolve().parent
    rep = reporting if reporting is not None else get_settings().reporting_currency
    try:
        handle = proc_lock.lock(proc_lock.settlement_lock_path(data), timeout=LOCK_TIMEOUT_S)
    except TimeoutError:
        conn = connect_path(db_path)
        try:
            return _refuse_locked(conn, run_id)
        finally:
            conn.close()
    with handle:
        conn = connect_path(db_path)
        try:
            try:
                job = _Run(conn, db_path, data, run_id, rep, priority)
            except (LookupError, sqlite3.OperationalError):
                return None  # no report for this run (or no store at all): nothing to end
            return job.execute()
        finally:
            conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    """The child's entry: parse ``--run``, load test stubs (``PD_TEST_STUBS``), lower its own
    priority, then :func:`run`. Exit 0 when the run's report is final, 1 otherwise."""
    parser = argparse.ArgumentParser(prog="settle_worker")
    parser.add_argument("--run", type=int, required=True, dest="run_id")
    args = parser.parse_args(argv)
    apply_test_stubs_from_env()
    applied = lower_own_priority()
    status = run(args.run_id, db_path=get_settings().db_path,
                 priority=PRIORITY_LOW_ZH if applied else None)
    return 0 if status is not None and status in st.END_STATUSES else 1


if __name__ == "__main__":  # pragma: no cover - the child process entry
    raise SystemExit(main())
