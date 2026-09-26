"""DEF-047 (functional test manual C-01, owner ruling 2026-09-24): a source that answered with NO
dividend records has not failed.

收件匣 › 重新偵測 toasted 「! 偵測完成（部分標的失敗）14 檔事件已更新，1 檔失敗（TSLA：yfinance
無配息資料）」 in the warning colour — TSLA has never paid a dividend, and yfinance said so.
Ruling: 「來源回應成功但沒有配息紀錄」 is normal (「1 檔無配息紀錄」); only a real fetch failure is
a failure and gets the warning face.

The misclassification lived at the SOURCE — ``Registry.fetch_dividends_explained`` put every
symbol that ended without events into ``failed``, whether its providers raised or answered
empty — so every surface built on it (the inbox toast, ``dividend_inbox_scan`` and
``dividends_daily`` in 排程中心) inherited it. The page's warn face is keyed on
``refreshed.failed`` and is left as it is: it is now told the truth.
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
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


class _Script(ProviderBase):
    """A dividend provider whose answer per symbol is scripted: a list (possibly empty) or an
    exception to raise."""

    def __init__(self, name: str, script: dict[str, object]) -> None:
        self.name = name
        self._script = script

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        answer = self._script.get(instruments[0].symbol, [])
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, list)
        return answer


def _event(sym: str) -> DividendEvent:
    return DividendEvent(instrument=sym, market=Market.US, ex_date=date(2026, 3, 1),
                         cash_amount=None, currency=None, source="x")


def _reg(*chain: _Script) -> Registry:
    return Registry(providers={p.name: p for p in chain},
                    order={(DataType.DIVIDEND, Market.US): [p.name for p in chain]})


_TSLA = InstrumentRef(symbol="TSLA", market=Market.US)
_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)


def test_an_answer_with_no_records_is_empty_not_failed() -> None:
    reg = _reg(_Script("yfinance", {"AAPL": [_event("AAPL")], "TSLA": []}))
    events, sources, failed, reasons, empty = reg.fetch_dividends_explained([_AAPL, _TSLA])
    assert sources == {"AAPL": "yfinance"} and len(events) == 1
    assert failed == [] and reasons == {}
    assert empty == ["TSLA"]


def test_only_a_real_fetch_failure_is_a_failure() -> None:
    reg = _reg(_Script("yfinance", {"TSLA": TimeoutError("read timed out")}))
    _e, _s, failed, reasons, empty = reg.fetch_dividends_explained([_TSLA])
    assert failed == ["TSLA"] and reasons == {"TSLA": "yfinance 逾時"} and empty == []


@pytest.mark.parametrize("first,second", [
    (TimeoutError("t"), []),        # the first source is down, the second answers: none
    ([], ValueError("bad")),        # the first answers none, the fallback is broken
])
def test_one_clean_answer_in_the_chain_is_enough(first: object, second: object) -> None:
    reg = _reg(_Script("yfinance", {"TSLA": first}), _Script("stooq", {"TSLA": second}))
    _e, _s, failed, _r, empty = reg.fetch_dividends_explained([_TSLA])
    assert failed == [] and empty == ["TSLA"]


def test_the_sentence_says_no_records_and_never_failed() -> None:
    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    reg = _reg(_Script("yfinance", {"AAPL": [_event("AAPL")], "TSLA": []}))
    summary = refresh_dividends(conn, reg, [_AAPL, _TSLA], now=_NOW)
    conn.close()
    assert summary.failed == [] and summary.empty == ["TSLA"]
    assert describe_refresh(summary) == "1 檔事件已更新，1 檔無配息紀錄（TSLA）"


@pytest.fixture
def aapl_pays_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The golden ledger's US holding (AAPL) answered by a source with no dividend series;
    the TW holding (2330) gets its events from FinMind as usual."""
    class _Finmind(ProviderBase):
        name = "finmind"

        def supports(self, data_type: DataType, market: Market | None) -> bool:
            return data_type is DataType.DIVIDEND and market is Market.TW

        def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
            return [DividendEvent(instrument=r.symbol, market=r.market,
                                  ex_date=date(2026, 6, 1), cash_amount=None, currency=None,
                                  source=self.name) for r in instruments]

    def reg(conn: sqlite3.Connection) -> Registry:
        return Registry(
            providers={"finmind": _Finmind(), "yfinance": _Script("yfinance", {})},
            order={(DataType.DIVIDEND, Market.TW): ["finmind"],
                   (DataType.DIVIDEND, Market.US): ["yfinance"]})

    monkeypatch.setattr(inbox, "default_registry", reg)
    monkeypatch.setattr(jobs, "default_registry", reg)


@pytest.mark.usefixtures("aapl_pays_nothing")
def test_the_inbox_refresh_reports_no_records_as_normal(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    body = api_client.get("/api/dividend-inbox", params={"refresh": 1}).json()
    refreshed = body["refreshed"]
    # `failed` is what the page's warn face keys on — empty now: 「偵測完成」, not 「部分標的失敗」
    assert refreshed["failed"] == []
    assert refreshed["empty"] == ["AAPL"]
    assert refreshed["text"] == (
        f"1 檔事件已更新，1 檔無配息紀錄（AAPL）・待確認 {body['total_count']} 筆")
    assert "失敗" not in refreshed["text"]
    # …and 排程中心's two dividend jobs say the same, with no 失敗 either.
    # DEF-067: both jobs return their verdict with the sentence — and "no records" is ok.
    scan = inbox.scan_job(golden_db, now=_NOW)
    assert (scan.status, scan.detail) == ("ok", refreshed["text"])
    daily = jobs.dividends_daily(golden_db, now=_NOW)
    assert daily.status == "ok"
    assert "無配息紀錄（AAPL）" in daily.detail and "失敗" not in daily.detail
