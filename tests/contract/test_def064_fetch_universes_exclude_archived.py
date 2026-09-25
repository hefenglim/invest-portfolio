"""DEF-064 (owner ruling ⑦, 2026-09-25): every fetch / compute universe leaves ARCHIVED
instruments out — through ONE predicate — and a HELD symbol is never among them.

R4 developer decision ⑦: ``pricing/ingest.py::tw_universe`` / ``all_universe`` read every
``instruments`` row, so the five scheduled snapshot jobs (FinMind chips / valuation /
fundamentals, consensus, the daily fundamentals union) kept spending external quota on symbols
the owner had stopped tracking, while the quote worklist and the insight universe (DEF-059)
already dropped them — each with its own spelling of the filter.

Three things are pinned here, each through the real door:

1. **Each universe loses exactly the archived symbol** — the two ingest universes, the quote
   worklist, the insight ``all_registered`` universe, the signal scan, the news "all" scope
   and the alert inputs.
2. **The five scheduled jobs never REQUEST it** — the provider seam is faked and what the job
   actually asked for is captured (``run_job``, the scheduler's own entry point).
3. **「持有 ⇒ 未封存」 holds at every door that can create a position**, so the flag alone is a
   safe filter for layers that cannot replay the book. Measured on 1ee7771 before this fix:
   a declared short archived (200), a position closed only by a FUTURE-dated sale archived
   (200), and sell-all → 封存 → delete-the-sell left 1,000 shares on an archived symbol that
   had dropped out of the quote worklist.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import alert_inputs, insight_service, news_service, signals_service
from portfolio_dash.data_ingestion.holdings import current_shares, holds_position
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    list_instruments,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing import consensus_source, fundamentals_source, ingest
from portfolio_dash.pricing.providers.yfinance_provider import yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_ARCHIVED_TW = "2454"
_ARCHIVED_US = "TSLA"


@pytest.fixture
def watch(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """The golden ledger (2330 / AAPL held) + four WATCH-ONLY symbols, two per market."""
    for sym, mkt, ccy, board in (
        ("2317", Market.TW, Currency.TWD, "TWSE"),
        (_ARCHIVED_TW, Market.TW, Currency.TWD, "TWSE"),
        ("NFLX", Market.US, Currency.USD, ""),
        (_ARCHIVED_US, Market.US, Currency.USD, ""),
    ):
        upsert_instrument(golden_db, Instrument(
            symbol=sym, market=mkt, quote_ccy=ccy, sector="Tech", name=sym, board=board))
    golden_db.commit()
    return golden_db


def _archive(api_client: TestClient, symbol: str) -> None:
    r = api_client.put(f"/api/instruments/{symbol}/archive", json={"archived": True})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 1. the universes


def _alert_universe(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The alert inputs' registered universe, as handed to its consensus seam."""
    seen: list[list[str]] = []
    real = alert_inputs._consensus_deltas

    def spy(c: sqlite3.Connection, registered: list[str], **kw: Any) -> Any:
        seen.append(list(registered))
        return real(c, registered, **kw)

    monkeypatch.setattr(alert_inputs, "_consensus_deltas", spy)
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    alert_inputs.assemble(conn, data, now=GOLDEN_NOW)
    assert len(seen) == 1
    return seen[0]


def _insight_universe(conn: sqlite3.Connection) -> list[str]:
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    return insight_service._all_registered_symbols(conn, insight_service.held_in_book(data))


_UNIVERSES: dict[str, Callable[[sqlite3.Connection, pytest.MonkeyPatch], list[str]]] = {
    "ingest.tw_universe": lambda c, _m: ingest.tw_universe(c),
    "ingest.all_universe": lambda c, _m: [r.symbol for r in ingest.all_universe(c)],
    "jobs.build_worklist": lambda c, _m: [r.symbol for r in jobs_mod.build_worklist(c, None)[0]],
    "insight all_registered": lambda c, _m: _insight_universe(c),
    "signals": lambda c, _m: signals_service._registered_symbols(c),
    "news all": lambda c, _m: [s for s, _ in news_service.resolve_news_scope(c, "all") or []],
    "alert inputs": _alert_universe,
}


@pytest.mark.parametrize("name", sorted(_UNIVERSES))
def test_each_universe_loses_exactly_the_archived_symbols(
    name: str, api_client: TestClient, watch: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = _UNIVERSES[name]
    before = set(universe(watch, monkeypatch))
    assert {_ARCHIVED_TW} <= before, f"{name}: the fixture symbol must start inside"
    _archive(api_client, _ARCHIVED_TW)
    _archive(api_client, _ARCHIVED_US)
    after = set(universe(watch, monkeypatch))
    assert after == before - {_ARCHIVED_TW, _ARCHIVED_US}, (
        f"{name}: archiving must remove exactly the archived symbols "
        f"(before={sorted(before)}, after={sorted(after)})")
    assert {"2330", "2317"} <= after, f"{name}: held / active TW symbols stay"


def test_restoring_puts_the_symbol_back_on_the_next_read(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """Ruling item 4: 還原 needs no extra step — the next scheduled run reads it again."""
    _archive(api_client, _ARCHIVED_TW)
    assert _ARCHIVED_TW not in ingest.tw_universe(watch)
    r = api_client.put(f"/api/instruments/{_ARCHIVED_TW}/archive", json={"archived": False})
    assert r.status_code == 200, r.text
    assert _ARCHIVED_TW in ingest.tw_universe(watch)
    assert _ARCHIVED_TW in [x.symbol for x in ingest.all_universe(watch)]


def test_the_universes_share_one_predicate(watch: sqlite3.Connection) -> None:
    """No fourth definition: with a hand-archived row the SQL scopes and the registry's own
    ``archived`` flag name the same set (a drifted spelling — e.g. ``archived = 0`` without
    the ``COALESCE`` — would disagree on a NULL-era row; this pins the full agreement)."""
    set_instrument_archived(watch, _ARCHIVED_TW, True)
    registry_view = sorted(i.symbol for i in list_instruments(watch) if not i.archived)
    assert [r.symbol for r in ingest.all_universe(watch)] == registry_view
    assert sorted(r.symbol for r in jobs_mod.build_worklist(watch, None)[0]) == registry_view
    assert signals_service._registered_symbols(watch) == registry_view


# ------------------------------------------------- 2. what the five jobs actually request


_TW_JOBS = ("finmind_chips_daily", "finmind_valuation_daily", "finmind_fundamentals_monthly")


@pytest.mark.parametrize("job_id", _TW_JOBS)
def test_finmind_jobs_never_request_an_archived_symbol(
    job_id: str, api_client: TestClient, watch: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def fake_dataset(conn: sqlite3.Connection, *, dataset: str, data_id: str,
                     start_date: str) -> list[dict[str, Any]]:
        asked.append(data_id)
        return []

    monkeypatch.setattr(ingest, "fetch_dataset", fake_dataset)
    _archive(api_client, _ARCHIVED_TW)
    rid = jobs_mod.run_job(watch, job_id, now=GOLDEN_NOW)
    status = watch.execute("SELECT status FROM job_runs WHERE id=?", (rid,)).fetchone()[0]
    assert status == "ok"
    assert asked, "the job must have requested something"
    assert _ARCHIVED_TW not in asked
    assert set(asked) == {"2330", "2317"}


def test_consensus_job_never_requests_an_archived_symbol(
    api_client: TestClient, watch: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def fake(yf_sym: str, *, as_of: date) -> dict[str, Any] | None:
        asked.append(yf_sym)
        return None

    monkeypatch.setattr(consensus_source, "fetch_consensus", fake)
    _archive(api_client, _ARCHIVED_TW)
    _archive(api_client, _ARCHIVED_US)
    jobs_mod.run_job(watch, "consensus_daily", now=GOLDEN_NOW)
    archived_yf = {yf_symbol(InstrumentRef(symbol=_ARCHIVED_TW, market=Market.TW,
                                           board="TWSE")), _ARCHIVED_US}
    assert asked
    assert not archived_yf & set(asked), asked
    assert set(asked) == {yf_symbol(r) for r in ingest.all_universe(watch)}


def test_fundamentals_job_never_requests_an_archived_symbol(
    api_client: TestClient, watch: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def fake(ref: InstrumentRef, *, as_of: date, token: str | None) -> dict[str, Any] | None:
        asked.append(ref.symbol)
        return None

    monkeypatch.setitem(fundamentals_source.FETCHERS, "yfinance", fake)
    monkeypatch.setitem(fundamentals_source.FETCHERS, "finnhub", fake)
    _archive(api_client, _ARCHIVED_TW)
    _archive(api_client, _ARCHIVED_US)
    jobs_mod.run_job(watch, "fundamentals_daily", now=GOLDEN_NOW)
    assert asked
    assert _ARCHIVED_TW not in asked and _ARCHIVED_US not in asked
    assert {"2330", "AAPL", "2317", "NFLX"} <= set(asked)


# ------------------------------------------------- 3. 「持有 ⇒ 未封存」 at every door


def _in_every_fetch_universe(conn: sqlite3.Connection, symbol: str) -> bool:
    return (symbol in [r.symbol for r in ingest.all_universe(conn)]
            and symbol in [r.symbol for r in jobs_mod.build_worklist(conn, None)[0]])


def _sell_all_2330(conn: sqlite3.Connection, *, on: date = date(2026, 2, 1),
                   short: bool = False, qty: str = "1000") -> int:
    return insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.SELL, quantity=Decimal(qty),
        price=Decimal("600"), fees=Decimal("0"), tax=Decimal("0"), trade_date=on,
        short_sale=short)


def test_a_held_long_cannot_be_archived(api_client: TestClient, watch: sqlite3.Connection
                                       ) -> None:
    for path, method in (("/api/instruments/2330/archive", "put"),
                         ("/api/instruments/2330", "delete")):
        r = (api_client.put(path, json={"archived": True}) if method == "put"
             else api_client.delete(path))
        assert r.status_code == 422 and r.json()["error"]["code"] == "held", r.text
    assert _in_every_fetch_universe(watch, "2330")


def test_a_declared_short_cannot_be_archived(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """A short is a live, priced position; ``current_shares > 0`` did not see it."""
    _sell_all_2330(watch, qty="1500", short=True)  # 1,000 long → 500 short
    assert current_shares(watch, "tw_broker", "2330") == Decimal("-500")
    r = api_client.put("/api/instruments/2330/archive", json={"archived": True})
    assert r.status_code == 422, r.text
    r = api_client.delete("/api/instruments/2330")
    assert r.status_code == 422, r.text
    assert _in_every_fetch_universe(watch, "2330")


def test_a_position_closed_only_by_a_future_sale_cannot_be_archived(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """DEF-056 allows a row ahead of its date; the all-dates net then reads 0 while the
    position is still held today (GOLDEN_NOW = 2026-06-11)."""
    _sell_all_2330(watch, on=date(2026, 12, 1))
    assert current_shares(watch, "tw_broker", "2330") == 0
    r = api_client.put("/api/instruments/2330/archive", json={"archived": True})
    assert r.status_code == 422, r.text
    r = api_client.delete("/api/instruments/2330")
    assert r.status_code == 422, r.text


def test_a_future_dated_position_cannot_be_archived(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    insert_transaction(watch, account_id="tw_broker", symbol="2317", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 12, 1))
    r = api_client.put("/api/instruments/2317/archive", json={"archived": True})
    assert r.status_code == 422, r.text


def test_a_flat_symbol_can_still_be_archived(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """The guard did not become a wall: a closed position archives as before."""
    _sell_all_2330(watch)
    _archive(api_client, "2330")
    assert not _in_every_fetch_universe(watch, "2330")


def test_deleting_the_closing_sale_reactivates_the_symbol(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    sid = _sell_all_2330(watch)
    _archive(api_client, "2330")
    r = api_client.delete(f"/api/ledgers/transactions/{sid}")
    assert r.status_code == 200, r.text
    inst = get_instrument(watch, "2330")
    assert inst is not None and not inst.archived
    assert _in_every_fetch_universe(watch, "2330")


def test_editing_the_closing_sale_down_reactivates_the_symbol(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    sid = _sell_all_2330(watch)
    _archive(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{sid}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "SELL", "shares": "400",
        "price": "600", "date": "2026-02-01", "fee": "0", "tax": "0"})
    assert r.status_code == 200, r.text
    inst = get_instrument(watch, "2330")
    assert inst is not None and not inst.archived
    assert _in_every_fetch_universe(watch, "2330")


def test_an_edit_that_leaves_the_symbol_flat_keeps_it_archived(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """Re-activation is the held test, not "any edit": a price correction on the closing
    sale of a stopped-tracking symbol must not bring it back."""
    sid = _sell_all_2330(watch)
    _archive(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{sid}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "SELL", "shares": "1000",
        "price": "610", "date": "2026-02-01", "fee": "0", "tax": "0"})
    assert r.status_code == 200, r.text
    inst = get_instrument(watch, "2330")
    assert inst is not None and inst.archived


def test_deleting_an_exchange_hands_the_source_back_and_reactivates_it(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """A renamed-away ticker is the natural thing to archive; deleting the EXCHANGE gives
    its shares back."""
    aid = insert_corporate_action(
        watch, account_id="tw_broker", action_date=date(2026, 3, 2),
        kind=CorporateActionKind.EXCHANGE, from_symbol="2330", to_symbol=_ARCHIVED_TW,
        ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _archive(api_client, "2330")
    r = api_client.delete(f"/api/ledgers/corporate-actions/{aid}")
    assert r.status_code == 200, r.text
    inst = get_instrument(watch, "2330")
    assert inst is not None and not inst.archived
    assert _in_every_fetch_universe(watch, "2330")


def test_an_exchange_into_an_archived_symbol_reactivates_it(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    _archive(api_client, _ARCHIVED_TW)
    insert_corporate_action(
        watch, account_id="tw_broker", action_date=date(2026, 3, 2),
        kind=CorporateActionKind.EXCHANGE, from_symbol="2330", to_symbol=_ARCHIVED_TW,
        ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    inst = get_instrument(watch, _ARCHIVED_TW)
    assert inst is not None and not inst.archived


def test_a_share_dividend_on_an_archived_symbol_reactivates_it(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    _sell_all_2330(watch)
    _archive(api_client, "2330")
    insert_dividend(watch, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 1),
                    div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                    net=Decimal("0"), reinvest_shares=Decimal("10"))
    inst = get_instrument(watch, "2330")
    assert inst is not None and not inst.archived


def test_no_archived_symbol_holds_a_position_after_the_doors(
    api_client: TestClient, watch: sqlite3.Connection
) -> None:
    """The invariant itself, over every instrument, after a mixed sequence of the doors."""
    sid = _sell_all_2330(watch)
    _archive(api_client, "2330")
    _archive(api_client, _ARCHIVED_TW)
    api_client.delete(f"/api/ledgers/transactions/{sid}")
    today = app_now().date()
    offenders = [i.symbol for i in list_instruments(watch)
                 if i.archived and holds_position(watch, i.symbol, today=today)]
    assert offenders == []
