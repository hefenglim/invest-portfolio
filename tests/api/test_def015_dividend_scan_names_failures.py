"""DEF-015 (functional test manual C-01, 2026-09-23): a failed detection names WHAT failed and WHY.

收件匣 → 重新偵測 answered 「14 檔事件已更新，1 檔失敗・待確認 7 筆」, ``GET /api/dividend-inbox
?refresh=1`` carried only that string, and the 排程中心 row of ``dividend_inbox_scan`` said the
same — while ``dividends_daily``, refreshing the very same events, wrote 「failed: SPCX, TSLA」.
Two detail formats for one operation, and the one the owner reads named nothing.

Three things were missing, each at its own layer:

* the REASON never existed — ``Registry.fetch_dividends`` swallowed every provider exception
  and every empty answer into one bare list of symbols;
* the inbox reduced the summary to two counts;
* the two scheduler jobs and the inbox each formatted the summary their own way.

Now the registry records a zh reason per failed symbol (``RefreshSummary.failed_reasons``),
ONE formatter (``pricing.refresh.describe_refresh``) renders every dividend refresh, and the
inbox answers ``refreshed: {updated, failed: [{symbol, reason}], text}`` with ``text`` the SAME
sentence ``scan_job`` writes into ``job_runs``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import dividend_inbox as inbox
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import describe_refresh, refresh_dividends
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import DividendEvent, RefreshSummary
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


class _Finmind(ProviderBase):
    name = "finmind"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND and market is Market.TW

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        return [DividendEvent(instrument=r.symbol, market=r.market, ex_date=date(2026, 6, 1),
                              cash_amount=None, currency=None, source=self.name)
                for r in instruments]


class _SlowYf(ProviderBase):
    """yfinance timing out for AAPL and answering NOTHING for a never-paid symbol."""

    name = "yfinance"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        if instruments[0].symbol == "AAPL":
            raise TimeoutError("read timed out")
        return []


class _Broken(ProviderBase):
    name = "stooq"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND and market is Market.US

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        raise ValueError("unparseable payload")


def _reg(*, us: list[str] | None = None) -> Registry:
    return Registry(
        providers={"finmind": _Finmind(), "yfinance": _SlowYf(), "stooq": _Broken()},
        order={(DataType.DIVIDEND, Market.TW): ["finmind"],
               (DataType.DIVIDEND, Market.US): us if us is not None else ["yfinance"]},
    )


_2330 = InstrumentRef(symbol="2330", market=Market.TW)
_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)
_TSLA = InstrumentRef(symbol="TSLA", market=Market.US)
_MAYBK = InstrumentRef(symbol="1155", market=Market.MY)


# --- the registry now says WHY ------------------------------------------------------------


def test_the_registry_records_a_reason_per_failed_symbol() -> None:
    """DEF-047 (owner ruling 2026-09-24) re-cut this pin: TSLA — yfinance ANSWERED with no
    series — used to be listed here as a failure (「yfinance 無配息資料」). A source that
    answered is not a failed fetch; TSLA is now ``empty``, and only real failures remain."""
    events, sources, failed, reasons, empty = _reg(
        us=["yfinance", "stooq"]).fetch_dividends_explained([_2330, _AAPL, _TSLA, _MAYBK])
    assert sources == {"2330": "finmind"} and len(events) == 1
    assert failed == ["AAPL", "1155"]
    assert reasons == {
        "AAPL": "yfinance 逾時、stooq 回應錯誤",
        "1155": "無可用的配息資料來源",
    }
    assert empty == ["TSLA"]
    # The unexplained entry point keeps its three-tuple (every existing caller).
    assert _reg().fetch_dividends([_AAPL])[2] == ["AAPL"]


def test_refresh_dividends_carries_the_reasons() -> None:
    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    summary = refresh_dividends(conn, _reg(), [_2330, _AAPL, _TSLA], now=_NOW)
    conn.close()
    assert summary.failed_reasons == {"AAPL": "yfinance 逾時"}
    assert summary.failed == ["AAPL"] and summary.empty == ["TSLA"]   # DEF-047


# --- ONE formatter ------------------------------------------------------------------------


def test_one_sentence_names_every_failure_with_its_reason() -> None:
    summary = RefreshSummary(ok={"2330": "finmind"}, failed=["NVDA", "AAPL"],
                             failed_reasons={"AAPL": "yfinance 逾時",
                                             "NVDA": "yfinance 連線失敗"},
                             empty=["TSLA"], fetched_at=_NOW)
    # DEF-047: the no-dividend symbol is counted apart, before — and never inside — 失敗.
    assert describe_refresh(summary) == (
        "1 檔事件已更新，1 檔無配息紀錄（TSLA），"
        "2 檔失敗（AAPL：yfinance 逾時；NVDA：yfinance 連線失敗）")
    clean = RefreshSummary(ok={"2330": "finmind"}, failed=[], fetched_at=_NOW)
    assert describe_refresh(clean) == "1 檔事件已更新"
    # A summary built before reasons existed still names the symbol.
    legacy = RefreshSummary(ok={}, failed=["8299"], fetched_at=_NOW)
    assert describe_refresh(legacy) == "0 檔事件已更新，1 檔失敗（8299：來源未說明原因）"


# --- the inbox, the scan job and the fallback job say the same thing ----------------------


@pytest.fixture
def slow_yf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox, "default_registry", lambda conn: _reg())
    monkeypatch.setattr(jobs, "default_registry", lambda conn: _reg())


@pytest.mark.usefixtures("slow_yf")
def test_the_inbox_refresh_names_the_failed_symbol_and_its_reason(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    body = api_client.get("/api/dividend-inbox", params={"refresh": 1}).json()
    refreshed = body["refreshed"]
    assert refreshed["updated"] == 1
    assert refreshed["failed"] == [{"symbol": "AAPL", "reason": "yfinance 逾時"}]
    assert refreshed["text"] == (
        f"1 檔事件已更新，1 檔失敗（AAPL：yfinance 逾時）・待確認 {body['total_count']} 筆")
    # …and it is the SAME sentence the scheduled scan writes into job_runs (DEF-067: the
    # runner now returns the verdict WITH the sentence — one of two symbols lost → partial).
    scan = inbox.scan_job(golden_db, now=_NOW)
    assert (scan.status, scan.detail) == ("partial", refreshed["text"])


def test_a_read_without_refresh_carries_no_refresh_block(api_client: TestClient) -> None:
    assert api_client.get("/api/dividend-inbox").json()["refreshed"] is None


@pytest.mark.usefixtures("slow_yf")
def test_both_scheduler_jobs_use_the_one_formatter(golden_db: sqlite3.Connection) -> None:
    previous = jobs._DIVIDEND_SCAN_RUNNER
    jobs.register_dividend_scan_runner(None)   # the scheduler-only fallback branch
    try:
        fallback = jobs.dividend_inbox_scan(golden_db, now=_NOW)
    finally:
        jobs.register_dividend_scan_runner(previous)
    # DEF-067: both jobs return the verdict WITH the one sentence (1 of 2 lost → partial).
    assert (fallback.status, fallback.detail) == (
        "partial", "1 檔事件已更新，1 檔失敗（AAPL：yfinance 逾時）")
    assert "AAPL：yfinance 逾時" in jobs.dividends_daily(golden_db, now=_NOW).detail


def test_the_inbox_page_renders_the_structured_refresh() -> None:
    """The page reads ``refreshed.text`` / ``refreshed.failed`` — the old string concatenation
    (``(resp.refreshed || '') + '・待確認 …'``) would now print 「[object Object]」."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "web" / "inbox.js").read_text(encoding="utf-8")
    assert "(resp.refreshed || '')" not in src
    assert "r.text" in src and "r.failed" in src
    assert "prog.warn(" in src, "a partial refresh must not wear the success face"
