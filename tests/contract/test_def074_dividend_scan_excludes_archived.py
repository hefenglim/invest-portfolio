"""DEF-074 (owner ruling ① B, 2026-09-26): the dividend-inbox scan universe leaves ARCHIVED
instruments out — through the ONE shared predicate the job's fallback path already reads.

The scheduled ``dividend_inbox_scan`` job has two paths. The registered runner
(``api/dividend_inbox.py::scan_job`` → ``refresh_events_for_acquired``) built its universe from
``list_instruments(conn)`` filtered only by "has an earliest acquisition", so an archived symbol
with a buy history kept spending dividend-event fetches every weekday; the fallback path
(``scheduler/jobs.py::dividend_inbox_scan`` → ``build_worklist``) already excluded it (DEF-064).
One job, two answers — the class DEF-064 set out to close.

Pinned through the provider seam (what each path actually REQUESTS), not the list it builds:

1. a symbol with a buy history, fully sold and archived, is requested by neither path;
2. restoring it puts it back on the next scan;
3. a held symbol is unaffected;
4. both paths of the job request exactly the same set, before and after archiving.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import dividend_inbox as inbox
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_CLOSED = "2454"   # bought, fully sold, then archived


class _Recorder(ProviderBase):
    """A dividend provider that records every symbol it is asked for and answers empty."""

    name = "recorder"

    def __init__(self) -> None:
        self.asked: list[str] = []

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        self.asked.extend(i.symbol for i in instruments)
        return []


@pytest.fixture
def ledger(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """The golden ledger (2330 / AAPL held) + a closed TW position that can be archived."""
    upsert_instrument(golden_db, Instrument(
        symbol=_CLOSED, market=Market.TW, quote_ccy=Currency.TWD, sector="Tech",
        name="MediaTek", board="TWSE"))
    for side, day in ((Side.BUY, date(2026, 1, 5)), (Side.SELL, date(2026, 2, 2))):
        insert_transaction(golden_db, account_id="tw_broker", symbol=_CLOSED, side=side,
                           quantity=Decimal("1000"), price=Decimal("1000"),
                           fees=Decimal("0"), tax=Decimal("0"), trade_date=day)
    golden_db.commit()
    return golden_db


def _requested(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, path: str
               ) -> set[str]:
    """The symbols one path of the ``dividend_inbox_scan`` job asks the provider for."""
    rec = _Recorder()
    reg = Registry(providers={rec.name: rec},
                   order={(DataType.DIVIDEND, m): [rec.name] for m in Market})
    monkeypatch.setattr(inbox, "default_registry", lambda _c: reg)
    monkeypatch.setattr(jobs, "default_registry", lambda _c: reg)
    if path == "runner":            # the registered runner: api/dividend_inbox.py
        inbox.refresh_events_for_acquired(conn, now=GOLDEN_NOW)
    else:                           # the scheduler's own fallback: build_worklist
        monkeypatch.setattr(jobs, "_DIVIDEND_SCAN_RUNNER", None)
        jobs.dividend_inbox_scan(conn, now=GOLDEN_NOW)
    return set(rec.asked)


def _archive(api_client: TestClient, symbol: str, archived: bool) -> None:
    r = api_client.put(f"/api/instruments/{symbol}/archive", json={"archived": archived})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["runner", "fallback"])
def test_an_archived_closed_symbol_is_not_scanned_and_comes_back_on_restore(
    path: str, api_client: TestClient, ledger: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _requested(ledger, monkeypatch, path)
    assert {_CLOSED, "2330"} <= before, f"{path}: the fixture symbols must start inside"

    _archive(api_client, _CLOSED, True)
    after = _requested(ledger, monkeypatch, path)
    assert after == before - {_CLOSED}, (
        f"{path}: archiving must remove exactly the archived symbol "
        f"(before={sorted(before)}, after={sorted(after)})")
    assert "2330" in after, f"{path}: a held symbol is never dropped"

    _archive(api_client, _CLOSED, False)
    assert _requested(ledger, monkeypatch, path) == before, (
        f"{path}: 還原 must put the symbol back on the next scan")


def test_both_paths_of_the_job_scan_the_same_universe(
    api_client: TestClient, ledger: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One job, one answer — the runner and the fallback agree before and after archiving."""
    assert (_requested(ledger, monkeypatch, "runner")
            == _requested(ledger, monkeypatch, "fallback"))
    _archive(api_client, _CLOSED, True)
    runner = _requested(ledger, monkeypatch, "runner")
    fallback = _requested(ledger, monkeypatch, "fallback")
    assert runner == fallback, f"runner={sorted(runner)} fallback={sorted(fallback)}"
