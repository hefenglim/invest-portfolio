"""Options W4 — an option contract is never fetched and never registered through a stock door.

Spec ``docs/spec/2026-10-09-options-ledger.md`` §6.2 (Quote jobs row) and OPT-D7: an open
contract is valued at its last trade price (R2), so no provider is ever asked about it.

1. **The tracked universe is stocks only** — ``shared/instrument_scope.py::TRACKED_SQL`` filters
   ``asset_type = 'stock'``, so every scope that reads it (the quote / history / dividend
   worklist, the two snapshot-ingest universes, the insight ``all_registered`` universe, the
   signal scan, the news "all" scope, the alert inputs) loses the contract and nothing else.
2. **Every bypass refuses a contract** — ``quick_register`` and ``restore_archived`` at entry,
   regardless of ``force`` (「選擇權合約由交易登錄建立」); the gap backfill;
   ``refresh_instrument_quote``; the Alpha Vantage held universe; the held-only news run — each
   with the provider seam replaced by a recorder that proves no call is made.
3. **The register-first path meets the refusal** — ``resolve.py`` routes an unregistered OCC
   symbol to the register-first flow (code-shaped, ``NEEDS_AI`` with no candidates), and that
   flow's ``quick_register`` refuses it: a contract is created by the trade door
   (``register_option_contract``), never by the stock quick-add.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import (
    alert_inputs,
    fundamentals_service,
    insight_service,
    instrument_service,
    news_service,
    signals_service,
)
from portfolio_dash.api.instrument_service import (
    QuickRegisterError,
    gap_backfill,
    lookup_instrument,
    quick_register,
    restore_archived,
)
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_instruments,
    set_instrument_archived,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing import ingest
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.instrument_scope import (
    OPTION_REGISTRATION_REFUSAL,
    TRACKED_SQL,
    is_option_symbol,
    tracked_instruments,
    tracked_symbols,
)
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_PUT = "AAPL261218P00150000"    # a sold put — a declared short of one contract
_CALL = "AAPL261218C00200000"   # a long call — two contracts
_UNREGISTERED = "MSFT270115C00400000"


def _recorder(calls: list[str], name: str) -> Callable[..., Any]:
    def record(*_a: Any, **_k: Any) -> Any:
        calls.append(name)
        raise AssertionError(f"provider seam {name} was called for an option contract")
    return record


@pytest.fixture
def options_db(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """The golden ledger (2330 / AAPL held) + two HELD contracts in the Schwab account."""
    register_option_contract(golden_db, _PUT)
    register_option_contract(golden_db, _CALL)
    insert_transaction(golden_db, account_id="schwab", symbol=_PUT, side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("2.5"), fees=Decimal("0.65"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 2), short_sale=True)
    insert_transaction(golden_db, account_id="schwab", symbol=_CALL, side=Side.BUY,
                       quantity=Decimal("200"), price=Decimal("1.2"), fees=Decimal("1.3"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 3))
    golden_db.commit()
    return golden_db


# ---------------------------------------------------------------- 1. the tracked universe


def test_tracked_sql_is_stocks_only() -> None:
    assert "asset_type = 'stock'" in TRACKED_SQL
    assert "COALESCE(archived, 0) = 0" in TRACKED_SQL


def test_is_option_symbol_is_the_occ_shape() -> None:
    assert is_option_symbol(_PUT)
    assert is_option_symbol(" aapl261218p00150000 ")
    for stock in ("AAPL", "2330", "0050", "BRK.B", "5347.KL", "AAPL261218"):
        assert not is_option_symbol(stock), stock


def test_tracked_instruments_leave_out_every_contract(options_db: sqlite3.Connection) -> None:
    every = {i.symbol for i in list_instruments(options_db)}
    assert {_PUT, _CALL} <= every
    assert set(tracked_symbols(options_db)) == every - {_PUT, _CALL}
    assert {t.symbol for t in tracked_instruments(options_db, market=Market.US)} == {"AAPL"}


def _alert_universe(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[list[str]] = []
    real = alert_inputs._consensus_deltas

    def spy(c: sqlite3.Connection, registered: list[str], **kw: Any) -> Any:
        seen.append(list(registered))
        return real(c, registered, **kw)

    monkeypatch.setattr(alert_inputs, "_consensus_deltas", spy)
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    inputs = alert_inputs.assemble(conn, data, now=GOLDEN_NOW)
    assert set(inputs.symbol_metrics) == set(seen[0])
    return seen[0]


_UNIVERSES: dict[str, Callable[[sqlite3.Connection, pytest.MonkeyPatch], list[str]]] = {
    "ingest.tw_universe": lambda c, _m: ingest.tw_universe(c),
    "ingest.all_universe": lambda c, _m: [r.symbol for r in ingest.all_universe(c)],
    "jobs.build_worklist": lambda c, _m: [r.symbol for r in jobs_mod.build_worklist(c, None)[0]],
    "jobs.build_worklist(US)": lambda c, _m: [
        r.symbol for r in jobs_mod.build_worklist(c, Market.US)[0]],
    "insight all_registered": lambda c, _m: insight_service._all_registered_symbols(
        c, insight_service.held_in_book(
            build_dashboard(c, now=GOLDEN_NOW, reporting=Currency.TWD))),
    "signals": lambda c, _m: signals_service._registered_symbols(c),
    "news all": lambda c, _m: [s for s, _ in news_service.resolve_news_scope(c, "all") or []],
    "alert inputs": _alert_universe,
}


@pytest.mark.parametrize("name", sorted(_UNIVERSES))
def test_every_fetch_universe_leaves_out_the_contracts(
    name: str, options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = _UNIVERSES[name](options_db, monkeypatch)
    assert universe, f"{name}: the universe is empty — the test proves nothing"
    assert not {_PUT, _CALL} & set(universe), f"{name}: {universe}"
    assert "AAPL" in universe or "2330" in universe


# ---------------------------------------------------------------- 2. the bypasses


@pytest.fixture
def no_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every provider seam ``instrument_service`` and ``scheduler.jobs`` reach, recording."""
    calls: list[str] = []
    for name in ("default_registry", "refresh_quotes", "refresh_history", "refresh_dividends",
                 "lookup_name", "probe_tw_board", "refresh_instrument_quote"):
        monkeypatch.setattr(instrument_service, name, _recorder(calls, f"service.{name}"))
    for name in ("default_registry", "refresh_quotes"):
        monkeypatch.setattr(jobs_mod, name, _recorder(calls, f"jobs.{name}"))
    return calls


@pytest.mark.parametrize("force", [False, True])
def test_quick_register_refuses_a_contract_at_entry(
    force: bool, golden_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    with pytest.raises(QuickRegisterError) as exc:
        quick_register(golden_db, symbol=_UNREGISTERED.lower(), market=Market.US,
                       now=GOLDEN_NOW, force=force)
    assert exc.value.code == "option_contract"
    assert exc.value.status == 422
    assert exc.value.message == OPTION_REGISTRATION_REFUSAL == "選擇權合約由交易登錄建立"
    assert get_instrument(golden_db, _UNREGISTERED) is None
    assert no_provider == []


def test_quick_register_refuses_an_archived_contract_instead_of_restoring_it(
    options_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    set_instrument_archived(options_db, _PUT, True)
    with pytest.raises(QuickRegisterError) as exc:
        quick_register(options_db, symbol=_PUT, market=Market.US, now=GOLDEN_NOW, force=True)
    assert exc.value.code == "option_contract"
    stored = get_instrument(options_db, _PUT)
    assert stored is not None and stored.archived  # nothing was restored
    assert no_provider == []


def test_restore_archived_refuses_a_contract(
    options_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    set_instrument_archived(options_db, _CALL, True)
    existing = get_instrument(options_db, _CALL)
    assert existing is not None
    with pytest.raises(QuickRegisterError) as exc:
        restore_archived(options_db, existing, name="x", sector="Tech")
    assert exc.value.code == "option_contract"
    stored = get_instrument(options_db, _CALL)
    assert stored is not None and stored.archived and stored.name == existing.name
    assert no_provider == []


@pytest.mark.parametrize("route,body", [
    ("/api/instruments", {"symbol": _UNREGISTERED, "market": "US", "name": "x"}),
    ("/api/instruments/quick", {"symbol": _UNREGISTERED, "market": "US", "force": True}),
])
def test_both_register_doors_answer_422_for_a_contract(
    route: str, body: dict[str, Any], api_client: TestClient, golden_db: sqlite3.Connection,
    no_provider: list[str],
) -> None:
    r = api_client.post(route, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "option_contract"
    assert r.json()["error"]["message"] == "選擇權合約由交易登錄建立"
    assert get_instrument(golden_db, _UNREGISTERED) is None
    assert no_provider == []


def test_the_classic_register_door_refuses_to_restore_an_archived_contract(
    api_client: TestClient, options_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    set_instrument_archived(options_db, _PUT, True)
    r = api_client.post("/api/instruments", json={"symbol": _PUT, "market": "US"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "option_contract"
    stored = get_instrument(options_db, _PUT)
    assert stored is not None and stored.archived
    assert no_provider == []


def test_the_gap_backfill_skips_a_contract(
    options_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    gap_backfill(_PUT, now=GOLDEN_NOW, conn=options_db)  # swallows errors: the recorder proves it
    assert no_provider == []


def test_the_archive_door_restores_a_contract_without_a_backfill_fetch(
    api_client: TestClient, options_db: sqlite3.Connection, no_provider: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """還原 is a flag flip; its background gap backfill opens its OWN session, pointed here at
    the test ledger so the backfill really meets the contract (and records any provider call)."""
    @contextmanager
    def same_db() -> Iterator[sqlite3.Connection]:
        yield options_db

    monkeypatch.setattr(instrument_service, "session", same_db)
    reached: list[str] = []
    real_inner = instrument_service._gap_backfill_inner

    def spy(conn: sqlite3.Connection, symbol: str, now: datetime) -> None:
        reached.append(symbol)
        real_inner(conn, symbol, now)

    monkeypatch.setattr(instrument_service, "_gap_backfill_inner", spy)
    set_instrument_archived(options_db, _PUT, True)
    r = api_client.put(f"/api/instruments/{_PUT}/archive", json={"archived": False})
    assert r.status_code == 200, r.text  # the background gap backfill ran inside TestClient
    assert reached == [_PUT]
    assert no_provider == []


def test_refresh_instrument_quote_skips_a_contract(
    options_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    detail = jobs_mod.refresh_instrument_quote(
        options_db, symbol=_PUT, market=Market.US, board=None, now=GOLDEN_NOW)
    assert no_provider == []
    assert detail == f"{_PUT} 是選擇權合約，不抓報價（以最後成交價估值）"


def test_the_quick_add_lookup_answers_not_found_without_a_provider_call(
    golden_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    found = lookup_instrument(golden_db, symbol=_UNREGISTERED, market=Market.US, now=GOLDEN_NOW)
    assert found.found is False
    assert no_provider == []


def test_ai_resolve_answers_a_contract_code_without_an_llm_call(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portfolio_dash.api.routers import instruments as instruments_router

    monkeypatch.setattr(instruments_router, "complete_structured",
                        _recorder([], "complete_structured"))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": _UNREGISTERED, "market": "US"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "not_found"
    assert r.json()["message"] == "選擇權合約由交易登錄建立"


def test_the_alpha_vantage_held_universe_leaves_out_the_contracts(
    options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[list[str]] = []

    def fake(c: sqlite3.Connection, *, now: datetime, sources: tuple[str, ...],
             universe: list[InstrumentRef]) -> ingest.SnapshotSweep:
        asked.append([r.symbol for r in universe])
        return ingest.SnapshotSweep()

    monkeypatch.setattr(ingest, "ingest_fundamentals_union", fake)
    refs = fundamentals_service._held_refs(options_db, today=GOLDEN_NOW.date())
    assert {r.symbol for r in refs} == {"2330", "AAPL"}
    fundamentals_service.run_fundamentals_av(options_db, now=GOLDEN_NOW)
    assert len(asked) == 1 and set(asked[0]) == {"2330", "AAPL"}


def test_alpha_vantage_is_not_called_when_only_contracts_are_held(
    options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for sym in ("2330", "AAPL"):
        options_db.execute("DELETE FROM transactions WHERE symbol=?", (sym,))
        options_db.execute("DELETE FROM dividends WHERE symbol=?", (sym,))
    options_db.commit()
    monkeypatch.setattr(ingest, "ingest_fundamentals_union",
                        _recorder([], "ingest_fundamentals_union"))
    assert fundamentals_service.run_fundamentals_av(options_db, now=GOLDEN_NOW) \
        == ingest.SnapshotSweep()


def test_the_held_only_news_run_leaves_out_the_contracts(
    options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[list[tuple[str, str]]] = []

    def fake(c: sqlite3.Connection, holdings: list[tuple[str, str]], *,
             now: datetime) -> dict[str, int | bool]:
        asked.append(list(holdings))
        return {}

    monkeypatch.setattr(news_service, "run_news_for", fake)
    news_service.run_news_daily(options_db, now=GOLDEN_NOW)
    assert asked == [[("2330", "TW"), ("AAPL", "US")]]


def test_an_explicit_news_scope_naming_a_contract_is_refused(
    options_db: sqlite3.Connection,
) -> None:
    assert news_service.resolve_news_scope(options_db, _PUT) is None
    assert news_service.resolve_news_scope(options_db, "AAPL") == [("AAPL", "US")]


# ---------------------------------------------------------------- 3. register-first


def test_the_register_first_path_meets_the_entry_refusal(
    golden_db: sqlite3.Connection, no_provider: list[str],
) -> None:
    resolution = resolve(golden_db, _UNREGISTERED)
    assert resolution.status is ResolutionStatus.NEEDS_AI
    assert resolution.instrument is None and resolution.candidates == []
    with pytest.raises(QuickRegisterError) as exc:
        quick_register(golden_db, symbol=_UNREGISTERED, market=Market.US, now=GOLDEN_NOW,
                       force=True)
    assert exc.value.message == "選擇權合約由交易登錄建立"
    assert no_provider == []
    # The trade door's own registration is the one that creates the contract (OPT-D7).
    created = register_option_contract(golden_db, _UNREGISTERED)
    assert created.is_option
    assert resolve(golden_db, _UNREGISTERED).status is ResolutionStatus.EXACT
