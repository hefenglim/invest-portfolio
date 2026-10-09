"""DEF-067: a job that finished WITHOUT raising but lost what it was sent for is not 成功.

3be67db: ``dividends_daily`` returned ``describe_refresh(summary)`` — a bare string — and
``_outcome_of`` wraps every string as ``JobOutcome("ok", …)``. With every provider down the
排程中心 showed 「成功　0 檔事件已更新，10 檔失敗（…）」: the chip and its own sentence
contradicting each other. The quote jobs had returned a ``JobOutcome`` since M10-02; no other
job did. The class is "a job func whose failures are swallowed into its detail string".

Verdict rule for a per-symbol sweep (``jobs._sweep_outcome``): every symbol failed →
``error`` (the vocabulary a raised run already writes; the frontend maps it to 失敗),
some failed → ``partial`` (部分), none → ``ok``. A symbol whose source answered with no
dividend records is NOT a failure (DEF-047). The detail sentence is unchanged.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests

from portfolio_dash.pricing import consensus_source, fundamentals_source, ingest
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import JobOutcome, run_job_outcome
from portfolio_dash.shared.enums import Market

NOW = datetime(2026, 9, 25, 22, 38, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No provider at all: every symbol fails, no socket is opened. Runner seams cleared."""
    monkeypatch.setattr(
        jobs, "default_registry", lambda conn=None: Registry(providers={}, order={})
    )
    for name in ("_DIVIDEND_SCAN_RUNNER", "_NEWS_RUNNER", "_DIGEST_RUNNER",
                 "_INSIGHT_RUNNER", "_SNAPSHOT_RUNNER", "_SIGNAL_SCAN_RUNNER",
                 "_FUNDAMENTALS_RUNNER"):
        monkeypatch.setattr(jobs, name, None)
    jobs._INFLIGHT_JOBS.clear()
    yield
    jobs._INFLIGHT_JOBS.clear()


def _status(conn: sqlite3.Connection, job_id: str) -> tuple[str, str, JobOutcome]:
    run_id, outcome = run_job_outcome(conn, job_id, now=NOW)
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["status"], row["detail"]) == (outcome.status, outcome.detail)
    return row["status"], row["detail"], outcome


def _summary(ok: dict[str, str], failed: list[str], **kw: Any) -> RefreshSummary:
    return RefreshSummary(ok=ok, failed=failed, fetched_at=NOW, **kw)


# --- dividends_daily: the defect as reported -----------------------------------------


def test_dividends_daily_every_symbol_failed_is_error(golden_db: sqlite3.Connection) -> None:
    status, detail, _ = _status(golden_db, "dividends_daily")
    assert status == "error"
    assert detail.startswith("0 檔事件已更新，2 檔失敗（")  # the sentence is unchanged


def test_dividends_daily_some_failed_is_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "refresh_dividends", lambda *a, **k: _summary(
        {"2330": "finmind"}, ["AAPL"], failed_reasons={"AAPL": "yfinance 回應錯誤"}))
    status, detail, _ = _status(golden_db, "dividends_daily")
    assert (status, detail) == ("partial", "1 檔事件已更新，1 檔失敗（AAPL：yfinance 回應錯誤）")


def test_dividends_daily_no_records_is_not_a_failure(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "refresh_dividends", lambda *a, **k: _summary(
        {"2330": "finmind"}, [], empty=["AAPL"]))
    assert _status(golden_db, "dividends_daily")[0] == "ok"


# --- the class: every other sweep that swallowed its failures ------------------------


def test_history_daily_verdicts(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _status(golden_db, "history_daily")[0] == "error"  # nothing answered

    def one_ok(conn: Any, registry: Any, refs: list[Any], start: Any, **kw: Any) -> Any:
        return _summary({r.symbol: "twse" for r in refs if r.symbol == "2330"},
                        [r.symbol for r in refs if r.symbol != "2330"])

    monkeypatch.setattr(jobs, "refresh_history", one_ok)
    status, detail, _ = _status(golden_db, "history_daily")
    assert status == "partial"
    assert "1 項已更新，1 項失敗" in detail


def test_history_daily_benchmark_failure_alone_stays_ok(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FU-D27 still holds: a benchmark fetch never fails the instrument sweep."""

    def instruments_ok(conn: Any, registry: Any, refs: list[Any], start: Any, **kw: Any) -> Any:
        is_bench = any(r.symbol.startswith("^") or r.symbol == "0050" for r in refs)
        if is_bench:
            return _summary({}, [r.symbol for r in refs])
        return _summary({r.symbol: "twse" for r in refs}, [])

    monkeypatch.setattr(jobs, "refresh_history", instruments_ok)
    assert _status(golden_db, "history_daily")[0] == "ok"


def test_dividend_inbox_scan_fallback_path_verdicts(golden_db: sqlite3.Connection) -> None:
    """The scheduler-only fallback (no runner registered) sweeps the acquired symbols."""
    status, detail, _ = _status(golden_db, "dividend_inbox_scan")
    assert status == "error"
    assert "2 檔失敗" in detail


def test_dividend_inbox_scan_runner_outcome_passes_through(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from portfolio_dash.api import dividend_inbox

    monkeypatch.setattr(dividend_inbox, "refresh_dividends", lambda *a, **k: _summary(
        {"2330": "finmind"}, ["AAPL"], failed_reasons={"AAPL": "yfinance 回應錯誤"}))
    monkeypatch.setattr(dividend_inbox, "default_registry", lambda conn=None: None)
    monkeypatch.setattr(jobs, "_DIVIDEND_SCAN_RUNNER", dividend_inbox.scan_job)
    status, detail, _ = _status(golden_db, "dividend_inbox_scan")
    assert status == "partial"
    assert detail.startswith("1 檔事件已更新，1 檔失敗（AAPL：yfinance 回應錯誤）・待確認 ")


@pytest.mark.parametrize(("job_id", "fn", "written", "status"), [
    ("sentiment_daily", "ingest_sentiment", 0, "error"),
    ("sentiment_daily", "ingest_sentiment", 1, "partial"),
    ("sentiment_daily", "ingest_sentiment", 2, "ok"),
    ("index_quotes_daily", "ingest_index", 0, "error"),
    ("index_quotes_daily", "ingest_index", 1, "ok"),
])
def test_fixed_count_ingests_know_what_they_expected(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    job_id: str, fn: str, written: int, status: str,
) -> None:
    """VIX + Fear & Greed is always two snapshots, the index close one — each ``None``
    from its source is a lost fetch, swallowed by ``pricing/ingest.py`` into the count."""
    monkeypatch.setattr(jobs.ingest, fn, lambda conn, *, now: written)
    assert _status(golden_db, job_id)[0] == status


# --- the per-symbol snapshot ingests (owner 2026-09-30, item 12) ----------------------
# ``consensus_daily`` / ``fundamentals_daily`` / ``fundamentals_av_weekly`` wrote 「寫入 N 筆
# 外部快照」 as ``ok`` whatever happened: ``pricing/ingest.py`` counted a raised fetch together
# with "the source has no coverage for this symbol", and the keyed fetch seams below it
# turned a failed request — or Alpha Vantage's quota refusal — into that same ``None``.
# The golden universe is 2330 (TW) + AAPL (US), both held.


class _Capable:
    """The one registry seam ``ingest_fundamentals_union`` reads (``capable_ids``)."""

    def __init__(self, by_market: dict[Market, list[str]]) -> None:
        self._by_market = by_market

    def capable_ids(self, data_type: object, market: Market) -> list[str]:
        return self._by_market.get(market, [])


def _unreachable(*_a: Any, **_k: Any) -> Any:
    raise ConnectionError("provider unreachable")


def _consensus(as_of: date) -> dict[str, str]:
    return {"as_of": as_of.isoformat(), "source": "yfinance"}


@pytest.mark.parametrize(("sweep", "status"), [
    (ingest.SnapshotSweep(written=0, empty=0, failed=3), "error"),
    (ingest.SnapshotSweep(written=2, empty=1, failed=1), "partial"),
    (ingest.SnapshotSweep(written=2, empty=1, failed=0), "ok"),
    (ingest.SnapshotSweep(written=0, empty=4, failed=0), "ok"),  # answered with nothing
    (ingest.SnapshotSweep(), "ok"),  # nothing asked, nothing lost
])
@pytest.mark.parametrize(
    "job_id", ["consensus_daily", "fundamentals_daily", "fundamentals_av_weekly"]
)
def test_snapshot_ingests_judge_their_keys(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    job_id: str, sweep: ingest.SnapshotSweep, status: str,
) -> None:
    """Every key lost → 失敗, some → 部分, none → 成功; the sentence states all three counts."""
    monkeypatch.setattr(jobs.ingest, "ingest_consensus", lambda conn, *, now: sweep)
    monkeypatch.setattr(jobs.ingest, "ingest_fundamentals_union",
                        lambda conn, *, now, **kw: sweep)
    monkeypatch.setattr(jobs, "_FUNDAMENTALS_RUNNER", lambda conn, *, now: sweep)
    assert _status(golden_db, job_id)[:2] == (status, (
        f"寫入 {sweep.written} 筆外部快照，{sweep.empty} 筆來源無資料，"
        f"{sweep.failed} 筆擷取失敗"))


def test_consensus_daily_every_fetch_failed_is_error(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect as reported: every fetch raised, and the run read 成功."""
    monkeypatch.setattr(consensus_source, "fetch_consensus", _unreachable)
    assert _status(golden_db, "consensus_daily")[:2] == (
        "error", "寫入 0 筆外部快照，0 筆來源無資料，2 筆擷取失敗")


def test_consensus_daily_one_lost_fetch_is_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(yf_sym: str, *, as_of: date) -> dict[str, str]:
        if yf_sym == "AAPL":
            raise ConnectionError("provider unreachable")
        return _consensus(as_of)

    monkeypatch.setattr(consensus_source, "fetch_consensus", fetch)
    assert _status(golden_db, "consensus_daily")[:2] == (
        "partial", "寫入 1 筆外部快照，0 筆來源無資料，1 筆擷取失敗")


def test_consensus_daily_no_coverage_is_not_a_lost_fetch(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAPL answered with nothing, and 2330's snapshot proves yfinance answered this run."""
    monkeypatch.setattr(
        consensus_source, "fetch_consensus",
        lambda yf_sym, *, as_of: None if yf_sym == "AAPL" else _consensus(as_of),
    )
    assert _status(golden_db, "consensus_daily")[:2] == (
        "ok", "寫入 1 筆外部快照，1 筆來源無資料，0 筆擷取失敗")


def test_consensus_daily_empty_answers_without_proof_are_lost(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yfinance reports a failed HTTP call as an EMPTY answer, not an exception
    (``YfConfig.debug.hide_exceptions``) — DEF-067 ④'s reason ``history_daily`` trusts an
    empty answer only when the same run proves the source answered. Nothing was written,
    so nothing proves it."""
    monkeypatch.setattr(consensus_source, "fetch_consensus", lambda yf_sym, *, as_of: None)
    assert _status(golden_db, "consensus_daily")[:2] == ("error", (
        "寫入 0 筆外部快照，0 筆來源無資料，2 筆擷取失敗"
        "（其中 2 筆來源回應空白，且本輪沒有任何標的從同一來源取得資料，視為無法連線）"))


def test_fundamentals_daily_every_fetch_failed_is_error(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = ["yfinance", "finnhub"]
    monkeypatch.setattr(
        ingest, "default_registry",
        lambda conn=None: _Capable({Market.TW: ["yfinance"], Market.US: both}),
    )
    for source in both:
        monkeypatch.setitem(fundamentals_source.FETCHERS, source, _unreachable)
    assert _status(golden_db, "fundamentals_daily")[:2] == (
        "error", "寫入 0 筆外部快照，0 筆來源無資料，3 筆擷取失敗")


def test_fundamentals_av_weekly_quota_refusal_is_a_lost_fetch(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpha Vantage refuses over quota with HTTP 200 + ``{"Information": …}``; the seam
    read that as "no data for this symbol", so a Saturday pass that fetched nothing read
    成功. The REAL runner (``api.fundamentals_service``) and fetch seam run here."""
    from portfolio_dash.api import fundamentals_service

    class _Refusal:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"Information": "Our standard API rate limit is 25 requests per day."}

    monkeypatch.setenv("ALPHAVANTAGE_KEY", "demo-key")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Refusal())
    monkeypatch.setattr(
        ingest, "default_registry", lambda conn=None: _Capable({Market.US: ["alphavantage"]})
    )
    monkeypatch.setattr(jobs, "_FUNDAMENTALS_RUNNER", fundamentals_service.run_fundamentals_av)
    assert _status(golden_db, "fundamentals_av_weekly")[:2] == (
        "error", "寫入 0 筆外部快照，0 筆來源無資料，1 筆擷取失敗")


def test_news_budget_stop_is_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = {"organized": 2, "headline_only": 1, "skipped_existing": 3, "refetched": 0,
              "stopped_budget": True}
    monkeypatch.setattr(jobs, "_NEWS_RUNNER", lambda conn, *, now: result)
    status, detail, _ = _status(golden_db, "news_daily")
    assert status == "partial"
    assert detail == "AI 整理 2 則，僅存標題 1 則，已收錄略過 3 則；AI 額度用盡，提前結束"


def test_alert_scan_push_crash_is_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "_compute_alerts_for_scan", lambda conn, *, now: [])

    def boom(conn: Any, *, now: Any) -> str:
        raise RuntimeError("push path exploded")

    monkeypatch.setattr(jobs.notify_dispatch, "dispatch_notifications", boom)
    status, detail, _ = _status(golden_db, "alert_scan")
    assert status == "partial"
    assert "推播失敗" in detail and "exploded" not in detail


def test_digest_push_that_reached_no_channel_is_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from portfolio_dash.api import digest_service

    def runner(conn: Any, kind: str, *, now: Any) -> Any:
        return digest_service.run_digest(conn, kind, now=now)

    monkeypatch.setattr(digest_service, "_push", lambda *a, **k: digest_service.PushResult(
        text="推播 0/2 通道", sent=0, channels=2))
    monkeypatch.setattr(jobs, "_DIGEST_RUNNER", runner)
    status, detail, _ = _status(golden_db, "digest_daily")
    assert status == "partial"
    assert detail.endswith("推播 0/2 通道")


def test_a_job_with_no_runner_says_so_in_chinese(golden_db: sqlite3.Connection) -> None:
    """A scheduler-only process has no runner: a safe no-op (``ok``, as before), in zh."""
    for job_id in ("digest_daily", "digest_weekly", "snapshot_monthly", "signal_scan",
                   "news_daily", "fundamentals_av_weekly"):
        status, detail, _ = _status(golden_db, job_id)
        assert status == "ok", job_id
        assert detail.endswith("執行器未接線，未執行"), detail
