"""Options W4 — every STOCK reader leaves option contracts out (OPT-D10, spec §6.2).

v1 keeps options out of target weights, rebalance, what-if, the ex-dividend calendar and
projection, signals, alerts and their inputs, AI insights, the LLM variables, prompts, the
digest, news, fundamentals and the watchlist list (filterable): each of those readers treats a
held symbol as a stock, and a contract handed to it would be priced, fetched or narrated as
one. Two halves:

* **The scan** (the class guard, ``plan §5``): every function in the package that iterates a
  ``.holdings`` list, calls ``held_among`` / ``holds_position`` / ``held_in_book`` or reads
  ``tracked_instruments`` / ``tracked_symbols`` either applies the option filter — names one
  of :data:`_MARKERS` (``is_option`` / ``is_option_symbol`` / ``stock_instruments`` /
  ``asset_type``, or calls a source that is itself stock-only) — or is a reviewed entry in
  :data:`_ALLOWLIST` with its reason. A new reader nobody listed fails here, which is the
  plan's mitigation for "a stock reader nobody listed sees an option". The scan bites: a
  synthetic offender is caught, and a stale entry fails too (D39: an exception nobody uses is
  an exception nobody notices).
* **The behaviour** of the filters this package owns, through their public doors, on a ledger
  holding a sold put and a long call beside the golden stocks.

``DashboardData.holdings`` readers rely on W2 (subagent A) making that list stock-only
(OPT-D5); their allowlist reason says so, and the main session tests them at the merge.
"""

from __future__ import annotations

import ast
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import portfolio_dash
from portfolio_dash.api import insight_service
from portfolio_dash.api.routers import actions
from portfolio_dash.data_ingestion import fees as fees_mod
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction, list_instruments
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.instrument_scope import stock_instruments
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rebalance import compute_rebalance
from portfolio_dash.strategy.stock_scope import stock_market_value
from tests.conftest import GOLDEN_NOW

_PUT = "AAPL261218P00150000"
_CALL = "AAPL261218C00200000"
_PKG = Path(portfolio_dash.__file__).resolve().parent

# ---------------------------------------------------------------------------- the scan

#: A call to one of these reads the registry's 「持有」, the valuation 「持有」 or the tracked
#: universe — a reader this guard must see.
_TRIGGER_CALLS = frozenset({
    "held_among", "holds_position", "held_in_book", "tracked_instruments", "tracked_symbols",
})
#: A function that names one of these applies the option filter, or reads a source that is
#: stock-only by construction (``TRACKED_SQL`` / ``held_in_book``, both pinned by behaviour
#: tests below and in ``test_options_fetch_scope_and_registration.py``).
_MARKERS = frozenset({
    "is_option", "is_option_symbol", "stock_instruments", "asset_type", "ASSET_STOCK",
    "ASSET_OPTION", "tracked_instruments", "tracked_symbols", "held_in_book",
})

_DASH = ("reads DashboardData.holdings, which W2 makes stock-only (OPT-D5: holdings, weights "
         "and subtotals are stock-only); the main session tests it at the merge")
_EXPORT = ("an export over DashboardData.holdings — stock-only by W2 (OPT-D5); options leave "
           "through the `options` block (W6)")
_LLM_VARS = ("an LLM variable over DashboardData.holdings — stock-only by W2 (OPT-D5); a "
             "per-symbol card never targets a contract (held_in_book / the custom universe "
             "filter it)")

#: ``path::qualname`` → why this reader may see an option, or why it never can. Reviewed.
_ALLOWLIST: dict[str, str] = {
    # -- DashboardData.holdings readers (W2 makes the list stock-only) --------------------
    "api/digest_service.py::_held_symbols_and_names": _DASH,
    "api/digest_service.py::_daily": _DASH,
    "api/digest_service.py::_chores": _DASH,
    "api/insight_service.py::_resolve_markets": _DASH,
    "api/insight_service.py::_price_population": _DASH,
    "api/routers/prompts.py::_resolve_fx_rates": _DASH,
    "strategy/alerts.py::compute_alerts_from": _DASH,
    "strategy/rebalance.py::_excluded_with_target": _DASH,
    "strategy/whatif.py::_new_weight": (
        "reads DashboardData.holdings for the price of a symbol compute_whatif has already "
        "refused if it is a contract; stock-only by W2 besides"),
    "llm_insight/variables.py::_holdings_for_symbol": _LLM_VARS,
    "llm_insight/variables.py::_freshness": _LLM_VARS,
    "llm_insight/variables.py::value_for": _LLM_VARS,
    "export/holdings.py::build_holdings_csv": _EXPORT,
    "export/holdings_report.py::_filter_label": _EXPORT,
    "export/holdings_report.py::_holdings_table_html": _EXPORT,
    "export/rebalance_report.py::build_rebalance_report_html": _EXPORT,
    # -- the replay and the doors that must see every position, options included ----------
    "portfolio/dashboard.py::_build_dashboard": (
        "the valuation itself: W2 splits book.holdings into the stock list and the options "
        "block here"),
    "portfolio/dashboard.py::kpis_as_of": (
        "the valuation's KPI half, extracted from _build_dashboard (PERF §11.2): it values "
        "every position and splits the options out itself (opt_syms / split_holdings)"),
    "api/snapshots.py::period_detail": (
        "serves one close's position rows (settled_period_holding, or period_close.holdings_at "
        "for a close computed live): every position at D, stocks and contracts each tagged "
        "asset_type (OPT-D5 keeps them apart where listed); no stock-only figure is derived"),
    "portfolio/period_close.py::kpi_at": (
        "a close's notes: past_expiry_holdings looks FOR contracts past their expiry in the "
        "book (OPT-D2); the KPI half is kpis_as_of, which splits the options out itself"),
    "portfolio/settle_worker.py::_Run._close": (
        "writes the close's position rows (period_close.holdings_at): every position at D, "
        "stocks and contracts each tagged asset_type (OPT-D5 keeps them apart where listed); "
        "no stock-only figure is derived from them"),
    "portfolio/settle_worker.py::_Run._valuate": (
        "the live progress's position count (RunCurrent.positions, 「N 個部位」): every "
        "position the valuation replays, contracts included — a count, never a stock figure"),
    "api/routers/input_center.py::_holdings_or_none": (
        "the manual door's preview book: option mode closes a contract position (W3)"),
    "api/routers/input_center.py::_position_preview": (
        "the manual preview mirrors the replay for every position, contracts included (W3)"),
    "api/routers/ledgers.py::_by_key": (
        "a correction door's replay guard compares every position, contracts included"),
    # -- the registry 「持有」 of one row, or its definition ---------------------------------
    "data_ingestion/holdings.py::held_among": "the definition (over holds_position)",
    "data_ingestion/store.py::_reactivate_if_held": (
        "write side: a contract gaining a position is re-activated like any instrument; "
        "TRACKED_SQL still keeps it off every fetch scope"),
    "api/routers/instruments.py::_held": (
        "the watchlist badge and the 封存 / 移除 / 永久移除 gates of ONE row the caller chose: "
        "a held contract (listed only with include_options=1) is held (DEF-075)"),
    "api/signals_service.py::is_held": (
        "the held flag of ONE symbol the caller chose; a contract has no price series, so its "
        "signal evaluates empty — the scan universe is tracked (stock-only)"),
    "api/signals_service.py::evaluate_all": (
        "its universe is _registered_symbols → tracked_symbols (stock-only); held_among only "
        "flags that universe"),
}

#: Readers that MUST apply the filter themselves — an allowlist entry cannot excuse them.
_REQUIRED = frozenset({
    "strategy/whatif.py::compute_whatif",
    "strategy/rebalance.py::compute_rebalance",
    "api/insight_service.py::held_in_book",
    "api/fundamentals_service.py::_held_refs",
    "api/routers/actions.py::held_symbols",
    "api/routers/strategy.py::_target_weights_view",
    "api/news_service.py::run_news_daily",
})


@dataclass
class _Site:
    triggers: set[str] = field(default_factory=set)
    filtered: bool = False


def _scan_source(source: str) -> dict[str, _Site]:
    """Per function (qualname): the reader triggers it contains and whether it names a marker."""
    sites: dict[str, _Site] = {}
    markers: set[str] = set()

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack = (*stack, node.name)
        key = ".".join(stack) or "<module>"
        if (isinstance(node, ast.Attribute) and node.attr == "holdings"
                and isinstance(node.ctx, ast.Load)):
            sites.setdefault(key, _Site()).triggers.add(".holdings")
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in _TRIGGER_CALLS:
                sites.setdefault(key, _Site()).triggers.add(f"{name}()")
        if ((isinstance(node, ast.Name) and node.id in _MARKERS)
                or (isinstance(node, ast.Attribute) and node.attr in _MARKERS)
                or (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "asset_type" in node.value)):
            markers.add(key)
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(ast.parse(source), ())
    for key, site in sites.items():
        site.filtered = key in markers
    return sites


def _scan_package() -> dict[str, _Site]:
    out: dict[str, _Site] = {}
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        for qual, site in _scan_source(path.read_text(encoding="utf-8")).items():
            out[f"{rel}::{qual}"] = site
    return out


def _offenders(sites: dict[str, _Site], allow: dict[str, str]) -> list[str]:
    return sorted(k for k, s in sites.items() if not s.filtered and k not in allow)


def test_every_stock_reader_filters_options_or_is_a_reviewed_entry() -> None:
    sites = _scan_package()
    offenders = _offenders(sites, _ALLOWLIST)
    assert not offenders, (
        "These functions read holdings / the registry 「持有」 / the tracked universe without "
        "the option filter (OPT-D10). Filter contracts out (`is_option`, "
        "`shared.instrument_scope.is_option_symbol` / `stock_instruments`) or add a reviewed "
        "_ALLOWLIST entry saying why a contract cannot reach it:\n  "
        + "\n  ".join(f"{k}  [{', '.join(sorted(sites[k].triggers))}]" for k in offenders))


def test_every_allowlist_entry_is_still_an_unfiltered_reader() -> None:
    sites = _scan_package()
    stale = sorted(k for k in _ALLOWLIST if k not in sites or sites[k].filtered)
    assert not stale, (
        "Stale _ALLOWLIST entries — the function is gone, renamed, or now applies the filter "
        "itself; delete the entry (an exception nobody uses is an exception nobody notices): "
        + ", ".join(stale))


def test_the_required_readers_apply_the_filter_themselves() -> None:
    sites = _scan_package()
    assert not _REQUIRED & set(_ALLOWLIST)
    missing = sorted(k for k in _REQUIRED if k not in sites or not sites[k].filtered)
    assert not missing, f"must filter options themselves: {missing}"


def test_the_scan_catches_a_synthetic_offender() -> None:
    source = (
        "def leaks(book):\n"
        "    return [h.symbol for h in book.holdings]\n"
        "def registry(conn, symbols):\n"
        "    return held_among(conn, symbols, today=None)\n"
        "def filters(book):\n"
        "    return [h.symbol for h in book.holdings if not is_option_symbol(h.symbol)]\n"
        "def filters_registry(conn):\n"
        "    held = held_among(conn, [i.symbol for i in stock_instruments(x)], today=None)\n"
        "    return held\n"
        "class Reader:\n"
        "    def method(self, data):\n"
        "        return data.holdings\n"
        "def unrelated(rows):\n"
        "    return [r.symbol for r in rows]\n"
    )
    sites = _scan_source(source)
    assert set(sites) == {"leaks", "registry", "filters", "filters_registry", "Reader.method"}
    assert _offenders(sites, {}) == ["Reader.method", "leaks", "registry"]
    assert _offenders(sites, {"leaks": "x", "registry": "y", "Reader.method": "z"}) == []


def test_strategy_fee_callers_never_pass_is_option() -> None:
    """B adds ``is_option`` to ``compute_fees`` (default False); a strategy preview never
    prices a contract — what-if refuses one and rebalance excludes one — so none of its
    calls may pass it."""
    calls = 0
    for path in sorted((_PKG / "strategy").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name != "compute_fees":
                continue
            calls += 1
            assert "is_option" not in {k.arg for k in node.keywords}, path.name
    assert calls >= 3  # whatif ×1, rebalance ×2 — the scan is not vacuous


# ---------------------------------------------------------------------------- behaviour


@pytest.fixture
def options_db(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """The golden ledger (2330 / AAPL held) + a sold put and a long call, both open."""
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


# The element keys of /api/instruments before options (spec §10 invariant 1): the ONLY change
# on a stock ledger is the additive ``asset_type: "stock"``.
_ELEMENT_KEYS_BEFORE = {
    "symbol", "name", "aliases", "market", "board", "sector", "industry", "ccy", "held",
    "last", "chg_pct", "target_low", "target_high", "is_etf", "etf_flag_unknown", "archived",
    "has_history",
}


def test_instrument_elements_gain_only_asset_type_on_a_stock_ledger(
    api_client: TestClient,
) -> None:
    rows = api_client.get("/api/instruments").json()["list"]
    assert {r["symbol"] for r in rows} == {"2330", "AAPL"}
    for r in rows:
        assert set(r) == _ELEMENT_KEYS_BEFORE | {"asset_type"}
        assert r["asset_type"] == "stock"


def test_the_watchlist_hides_contracts_unless_asked(
    api_client: TestClient, options_db: sqlite3.Connection,
) -> None:
    default = api_client.get("/api/instruments").json()["list"]
    assert {r["symbol"] for r in default} == {"2330", "AAPL"}
    shown = api_client.get("/api/instruments", params={"include_options": 1}).json()["list"]
    by_symbol = {r["symbol"]: r for r in shown}
    assert set(by_symbol) == {"2330", "AAPL", _PUT, _CALL}
    assert by_symbol[_PUT]["asset_type"] == "option"
    assert by_symbol[_CALL]["asset_type"] == "option"
    assert by_symbol["AAPL"]["asset_type"] == "stock"
    # A held contract is held (DEF-075's registry 「持有」) and is never quoted.
    assert by_symbol[_PUT]["held"] is True and by_symbol[_PUT]["last"] is None
    assert by_symbol[_PUT]["name"] == "AAPL 2026-12-18 150 Put"


def test_target_weights_list_stocks_only_and_refuse_a_contract(
    api_client: TestClient, options_db: sqlite3.Connection,
) -> None:
    r = api_client.get("/api/target-weights")
    assert r.status_code == 200, r.text
    assert {s["symbol"] for s in r.json()["symbols"]} == {"2330", "AAPL"}
    r = api_client.put("/api/target-weights", json={"weights": {_PUT: "0.1"}})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == f"{_PUT} 是選擇權合約，不設目標權重"
    assert api_client.put("/api/target-weights",
                          json={"weights": {"AAPL": "0.2"}}).status_code == 200


def test_the_quote_job_threshold_leaves_out_contracts(
    options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "app_now", lambda: GOLDEN_NOW, raising=False)
    assert actions.held_symbols(options_db) == {"2330", "AAPL"}


def test_held_in_book_leaves_out_contracts(options_db: sqlite3.Connection) -> None:
    data = build_dashboard(options_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert insight_service.held_in_book(data) == {"2330", "AAPL"}


@pytest.mark.parametrize("universe,expected", [
    ({"mode": "all"}, ["2330", "AAPL"]),
    ({"mode": "custom", "symbols": ["AAPL", _PUT, _CALL]}, ["AAPL"]),
    ({"mode": "all_registered"}, ["2330", "AAPL"]),
    (None, ["2330", "AAPL"]),
])
def test_the_insight_universe_leaves_out_contracts(
    universe: Any, expected: list[str], options_db: sqlite3.Connection,
) -> None:
    data = build_dashboard(options_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert insight_service._resolve_universe_raw(options_db, universe, data) == expected


def test_what_if_refuses_a_contract_before_any_fee(
    api_client: TestClient, options_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("compute_fees reached for an option contract")

    monkeypatch.setattr(fees_mod, "compute_fees", boom)
    r = api_client.post("/api/whatif", json={
        "symbol": _PUT, "side": "buy", "shares": "100", "price": "1.0",
        "account_id": "schwab"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "選擇權不提供試算"
    assert r.json()["error"]["field"] == "symbol"


def test_rebalance_excludes_a_contract_target(options_db: sqlite3.Connection) -> None:
    out = compute_rebalance(options_db, now=GOLDEN_NOW, reporting=Currency.TWD,
                            targets={_PUT: Decimal("0.1"), "AAPL": Decimal("0.2")})
    summary: Any = out["summary"]
    assert _PUT in summary["excluded"]
    assert summary["excluded_reasons"][_PUT] == "no_price"
    assert all(row["symbol"] != _PUT for row in out["rows"])  # type: ignore[attr-defined]


def test_the_stock_total_is_the_kpi_total_on_a_stock_ledger(
    golden_db: sqlite3.Connection,
) -> None:
    """What-if and rebalance size their weights against Σ stock MV (OPT-D5). On a stock ledger
    that is the KPI 總市值 byte for byte, so both previews are unchanged (invariant 1)."""
    data = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert data.kpis.total_market_value is not None
    assert stock_market_value(data) == data.kpis.total_market_value
    assert str(stock_market_value(data)) == str(data.kpis.total_market_value)


@pytest.mark.parametrize("route", ["/api/prompts/preview", "/api/prompts/test"])
def test_a_per_symbol_prompt_on_a_contract_is_refused(
    route: str, api_client: TestClient, options_db: sqlite3.Connection,
) -> None:
    r = api_client.post(route, json={"body": "{{holdings_json}}", "scope": "per_symbol",
                                     "symbol": _PUT})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"] == f"{_PUT} 是選擇權合約，不提供 AI 分析"


def test_stock_instruments_keeps_registry_order(options_db: sqlite3.Connection) -> None:
    every = list_instruments(options_db)
    kept = stock_instruments(every)
    assert [i.symbol for i in kept] == [i.symbol for i in every if not i.is_option]
    assert {i.symbol for i in kept} == {"2330", "AAPL"}
