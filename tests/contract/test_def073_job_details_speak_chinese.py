"""DEF-073: every ``job_runs.detail`` a job writes is a Chinese sentence — the class guard.

The 排程中心 prints ``detail`` verbatim. 3be67db still wrote 「18 alert(s) [...], 7 dispatched」,
「daily digest 2026-09-25：…」, 「news: organized 2, …」, 「backup ok -> …」, 「N snapshot(s)
written」, 「no … runner registered」, 「9 symbol(s), 0 seeded, …」 — and an insight run's
detail was ``ok`` / ``R3_no_live_templates; …`` / the raw litellm English (DEF-066).

Why the existing guard did not catch it: ``tests/contract/test_zh_punctuation_fullwidth.py``
asks whether a half-width mark TOUCHES a CJK character. An all-English sentence has no CJK
to touch, so it is clean by that rule — the file even lists 「3 alert(s) [a, b], 2 dispatched;
notify：無啟用通道」 as a string its detector must NOT flag. It checks punctuation, never
language. This file checks language, on the RUNTIME text: each job is run the way the
scheduler runs it (``run_job_outcome``), with its runner / provider seams stubbed so it
finishes on its success and its failure branches, and the written detail is scanned.

What counts as English: a run of 2+ ASCII letters that is not an IDENTIFIER — a ticker or
pair (all caps: ``AAPL``, ``USDTWD``), a shared acronym (``AI``, ``JSON``, ``HTTP``), a
provider id (``twse``, ``yfinance`` — the sentence names which source answered), an
exception class (``RuntimeError`` — DEF-030 keeps it for log search) or a backup file name.

⚠ Blind spots (stated, not papered over): only the branches driven below are checked — a
job's rarer branches (a digest push that reached a channel, a notify channel failure) are
not; an English word spelled in capitals would pass as an identifier; and a raised
exception's own MESSAGE (``failure_detail``) is provider / Python text by design (DEF-030).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import digest_service, signals_service, snapshots
from portfolio_dash.api import dividend_inbox as inbox
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import JOBS, run_job_outcome

NOW = datetime(2026, 6, 11, 22, 38, tzinfo=ZoneInfo("Asia/Taipei"))
_REPO = Path(__file__).resolve().parents[2]

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_EXC_CLASS = re.compile(r"^[A-Z][A-Za-z]*(Error|Exception)$")
_FILENAME = re.compile(r"[\w-]+(?:\.(?:db|gz|sqlite))+")
_ACRONYMS = {"AI", "JSON", "HTTP", "KPI", "FX", "ETF"}
#: Push channels by their product names (the 通知 settings page uses the same words).
_CHANNELS = {"ntfy", "Telegram"}
#: The sentence names which source answered what (``_summarize``, item 8 2026-07-03).
_PROVIDERS = set(default_registry(None)._providers) | {"derived"}

#: Words a job's detail still carries because the text is produced OUTSIDE this change's
#: file ownership — each entry names the file:line and the proposed zh text is in the R6
#: developer report. ⚠ Delete the entry when that file is fixed; the stale check below
#: fails while the word is no longer seen.
_PENDING: dict[str, set[str]] = {}  # the notify_dispatch entry landed 2026-09-26 (R6)
#: (job, word) pairs of ``_PENDING`` actually seen in this session's scan (stale check).
#: Note: ``evaluate_insights`` / ``generate_calibrations`` belong to another R6 change
#: (DEF-073's evaluate / calibrate half); they are scanned here on the no-runner branch.
_SEEN_PENDING: set[tuple[str, str]] = set()


def english_words(detail: str) -> list[str]:
    text = _FILENAME.sub(" ", detail)
    return [
        w for w in _WORD.findall(text)
        if sum(c.isalpha() for c in w) >= 2
        and not (w.isupper() or w in _ACRONYMS or w in _PROVIDERS or w in _CHANNELS
                 or _EXC_CLASS.match(w))
    ]


def test_the_detector_sees_the_reported_sentences() -> None:
    """Positive control: every sentence DEF-073 quoted is flagged; zh sentences are not."""
    for english in ("18 alert(s) [單一標的集中度], 7 dispatched",
                    "daily digest 2026-09-25：組合 —", "news pass complete",
                    "backup ok -> portfolio_2026-06-16.db.gz", "3 snapshot(s) written",
                    "no digest runner registered", "evaluate pass complete",
                    "llm_unavailable_mid_run: provider error (haiku-4.5): boom", "ok",
                    "R3_no_live_templates; R2_universe_empty"):
        assert english_words(english), english
    for zh in ("3 項已更新（來源 twse：2330；yfinance：AAPL）；失敗：USDMYR",
               "備份完成：portfolio_2026-06-16.db.gz", "執行失敗：RuntimeError",
               "主模型 X：金鑰無效或未授權（HTTP 401）", "寫入 3 筆外部快照"):
        assert not english_words(zh), zh


@pytest.fixture
def db(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
       ) -> Iterator[sqlite3.Connection]:
    for name in ("_DIVIDEND_SCAN_RUNNER", "_NEWS_RUNNER", "_DIGEST_RUNNER", "_INSIGHT_RUNNER",
                 "_SNAPSHOT_RUNNER", "_SIGNAL_SCAN_RUNNER", "_FUNDAMENTALS_RUNNER",
                 "_EVALUATION_RUNNER", "_CALIBRATION_RUNNER", "_ALERT_COMPUTE_RUNNER",
                 "_ALERT_HELD_FN", "_HELD_SYMBOLS_FN"):
        monkeypatch.setattr(jobs, name, None)
    jobs._INFLIGHT_JOBS.clear()
    yield golden_db
    jobs._INFLIGHT_JOBS.clear()


def _no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = Registry(providers={}, order={})
    monkeypatch.setattr(jobs, "default_registry", lambda conn=None: empty)
    monkeypatch.setattr(inbox, "default_registry", lambda conn=None: empty)


def _all_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok_for(conn: Any, registry: Any, refs: list[Any], *a: Any, **kw: Any) -> RefreshSummary:
        keys = {getattr(r, "symbol", None) or f"{r.base.value}{r.quote.value}" for r in refs}
        return RefreshSummary(ok={k: "yfinance" for k in keys}, failed=[], fetched_at=NOW)

    def quotes(conn: Any, registry: Any, refs: list[Any], pairs: list[Any], **kw: Any) -> Any:
        s = ok_for(conn, registry, [*refs, *pairs])
        return s

    monkeypatch.setattr(jobs, "refresh_quotes", quotes)
    monkeypatch.setattr(jobs, "refresh_history", ok_for)
    monkeypatch.setattr(jobs, "refresh_dividends", ok_for)
    monkeypatch.setattr(inbox, "refresh_dividends", ok_for)
    monkeypatch.setattr(inbox, "default_registry", lambda conn=None: None)
    monkeypatch.setattr(jobs, "default_registry", lambda conn=None: None)


def _stub_seams(monkeypatch: pytest.MonkeyPatch, *, failing: bool) -> None:
    """The runner seams the app registers, wired to the REAL api-side runners wherever
    they run hermetically on the golden DB; only network-bound steps are stubbed."""
    for fn in ("ingest_chips", "ingest_valuation", "ingest_fundamentals", "ingest_consensus",
               "ingest_fundamentals_union", "ingest_sentiment", "ingest_index"):
        monkeypatch.setattr(jobs.ingest, fn, lambda conn, *, now, **kw: 0 if failing else 2)
    monkeypatch.setattr(jobs, "_SNAPSHOT_RUNNER", snapshots.snapshot_job)
    monkeypatch.setattr(jobs, "_SIGNAL_SCAN_RUNNER", signals_service.scan_signals)
    monkeypatch.setattr(jobs, "_DIGEST_RUNNER", digest_service.run_digest)
    monkeypatch.setattr(jobs, "_DIVIDEND_SCAN_RUNNER", inbox.scan_job)
    monkeypatch.setattr(jobs, "_FUNDAMENTALS_RUNNER", lambda conn, *, now: 0 if failing else 5)
    news = {"organized": 0 if failing else 2, "headline_only": 1, "skipped_existing": 3,
            "refetched": 1, "stopped_budget": failing}
    monkeypatch.setattr(jobs, "_NEWS_RUNNER", lambda conn, *, now: news)
    monkeypatch.setattr(jobs.backup_ops, "check_integrity", lambda: (True, "ok"))
    monkeypatch.setattr(jobs.backup_ops, "backup_database",
                        lambda *, now: Path(f"portfolio_{now.date().isoformat()}.db.gz"))
    if failing:
        def boom(conn: Any, *, now: Any) -> str:
            raise RuntimeError("push path exploded")

        monkeypatch.setattr(jobs.notify_dispatch, "dispatch_notifications", boom)


_ALL_JOBS = [spec.id for spec in JOBS]


@pytest.mark.parametrize("branch", ["success", "failure"])
@pytest.mark.parametrize("job_id", _ALL_JOBS)
def test_every_job_writes_a_chinese_detail(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, job_id: str, branch: str
) -> None:
    failing = branch == "failure"
    (_no_provider if failing else _all_answer)(monkeypatch)
    _stub_seams(monkeypatch, failing=failing)
    run_id, outcome = run_job_outcome(db, job_id, now=NOW)
    detail = db.execute("SELECT detail FROM job_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert detail == outcome.detail and detail
    words = english_words(detail)
    pending = _PENDING.get(job_id, set())
    _SEEN_PENDING.update((job_id, w) for w in words if w in pending)
    stray = [w for w in words if w not in pending]
    assert not stray, f"{job_id} ({branch}) wrote English into job_runs.detail: {detail!r}"


def test_the_no_runner_branch_speaks_chinese_too(db: sqlite3.Connection) -> None:
    """A scheduler-only process (no runner registered) — 「no … runner registered」 before."""
    for job_id in ("digest_daily", "digest_weekly", "snapshot_monthly", "signal_scan",
                   "news_daily", "fundamentals_av_weekly", "evaluate_insights",
                   "generate_calibrations"):
        _, outcome = run_job_outcome(db, job_id, now=NOW)
        assert not english_words(outcome.detail), (job_id, outcome.detail)


def test_the_worker_level_texts_speak_chinese() -> None:
    """The sentences the run WRAPPERS write, not the jobs: the missing insight runner, the
    cron overlap skip and an unknown id (the id itself is an identifier)."""
    assert not english_words(jobs._NO_INSIGHT_RUNNER)
    src = (_REPO / "portfolio_dash/scheduler/jobs.py").read_text(encoding="utf-8")
    assert "already_running：" not in src  # the enum lives in job_runs.reason, not detail
    assert not english_words(jobs.unknown_job_message("quotes_xx").replace("quotes_xx", ""))


def test_the_push_summary_speaks_chinese_on_every_branch(db: sqlite3.Connection) -> None:
    """``ops.notify_dispatch`` is embedded in alert_scan's detail; the scan above only reaches
    its no-channel branch. Drive the rest directly: sent, a channel down, the give-up."""
    from portfolio_dash.llm_insight import alerts_bridge
    from portfolio_dash.ops import notify, notify_dispatch

    noon = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    alerts_bridge.ensure_tables(db)
    notify.ensure_seeded(db)
    assert not english_words(notify_dispatch.dispatch_notifications(db, now=noon))
    cfg = notify.load_config(db)
    cfg.ntfy.enabled = True
    notify.save_config(db, cfg, now=noon)
    alerts_bridge.record_event(db, rule_id="single_weight", symbol="2330", now=noon)

    def down(channels: list[Any], *_a: Any) -> dict[str, str]:
        return {ch.name: "error: down" for ch in channels}

    def up(channels: list[Any], *_a: Any) -> dict[str, str]:
        return {ch.name: "ok" for ch in channels}

    details = [notify_dispatch.dispatch_notifications(db, now=noon, sender=down)
               for _ in range(3)]
    details.append(notify_dispatch.dispatch_notifications(db, now=noon, sender=up))
    alerts_bridge.record_event(db, rule_id="single_weight", symbol="2317", now=noon)
    details.append(notify_dispatch.dispatch_notifications(db, now=noon, sender=up))
    assert "放棄" in details[2]  # the third failed attempt gives up — that branch ran
    for d in details:
        assert not english_words(d), d


def test_the_pending_list_is_not_stale() -> None:
    """A pending word that no job writes any more must be deleted (D39 applied here).

    Runs after the parametrized scan in file order; if run alone it has nothing to check."""
    if _PENDING and not _SEEN_PENDING:
        pytest.skip("the job scan did not run in this session")
    for job_id, words in _PENDING.items():
        for w in words:
            assert (job_id, w) in _SEEN_PENDING, (
                f"_PENDING[{job_id!r}] still lists {w!r}, which no longer appears — delete it")
