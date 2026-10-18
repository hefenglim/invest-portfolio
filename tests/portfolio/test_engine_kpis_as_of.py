"""``dashboard.kpis_as_of`` — the KPI block of a period close (PERF spec §11.2).

Called exactly as the settlement worker will call it — the whole ledger replayed ONCE by
``iter_books`` over every event day and month end, the cash movements cut at D, and the
:class:`OnOrBefore` sources over the FULL database — it must reproduce every
``month_end/<D>.json`` reference of every corpus. Those references are v0.1.31's own
``GET /api/dashboard`` in the "truncated world" (a database copy with every price and FX row
after D deleted, at D 23:59 Taipei — ``tests/reference/payloads/README.md``), so this is the
claim that "as at D" on the full database is that world.

Compared: the KPI fields (``total_market_value``, ``total_return``, ``total_return_rate``, the
realized / unrealized split, ``xirr`` and its window and reason), ``by_currency_value``, and
the missing / stale facts — every held price read, and every FX pair the KPIs read. The
reference's ``freshness.fx`` also lists the pairs only the 換匯損益 summary reads (step 5,
which a period close leaves out — spec §11.2 "FX P&L at D"); those are named, not compared.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.data_ingestion.store import list_cash_movements, load_ledger_bundle
from portfolio_dash.portfolio.cost_basis import iter_books
from portfolio_dash.portfolio.dashboard import (
    KpiBlock,
    LatestStored,
    OnOrBefore,
    kpis_as_of,
)
from portfolio_dash.pricing.store import FxSeriesCache
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import LedgerBundle, valued_rows_as_of
from portfolio_dash.shared.wire import to_wire
from tests.reference.corpora import (
    CORPORA,
    CORPUS_NAMES,
    TEMPLATE_DIR_NAME,
    copy_corpus,
    open_db,
)

_PAYLOADS = Path(__file__).resolve().parents[1] / "reference" / "payloads"
_KPI_FIELDS = ("total_market_value", "total_return", "total_return_rate", "realized_total",
               "unrealized_total", "xirr", "xirr_window_days")


def _manifest() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((_PAYLOADS / "manifest.json").read_bytes())
    return doc


def _registered(conn: sqlite3.Connection) -> LedgerBundle:
    bundle = load_ledger_bundle(conn)
    return bundle.without_unregistered() if bundle.unregistered_symbols else bundle


@pytest.fixture(scope="module", params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
           ) -> Iterator[tuple[str, sqlite3.Connection]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"kpi-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, conn
    finally:
        conn.close()


def _kpi_wire(k: KpiBlock) -> dict[str, Any]:
    return {f: to_wire(getattr(k, f)) for f in _KPI_FIELDS}


def _price_facts(k: KpiBlock) -> list[dict[str, Any]]:
    return [{"symbol": s, "as_of": None if pr is None else pr.as_of.isoformat(),
             "stale": True if pr is None else pr.stale} for s, pr in k.price_reads.items()]


def _fx_facts(k: KpiBlock) -> dict[str, dict[str, Any]]:
    return {f"{b.value}/{q.value}": {"as_of": None if r is None else r.as_of.isoformat(),
                                     "stale": True if r is None else r.stale,
                                     "source": None if r is None else r.source}
            for (b, q), r in k.fx_reads.items()}


def _assert_matches(label: str, k: KpiBlock, ref: dict[str, Any]) -> None:
    assert _kpi_wire(k) == {f: ref["kpis"][f] for f in _KPI_FIELDS}, label
    assert k.xirr_reason == ref["freshness"]["xirr_unavailable_reason"], label
    view = ref["currency_view"]
    assert to_wire(k.by_currency_value) == (None if view is None
                                            else view["by_currency_value"]), label
    assert _price_facts(k) == ref["freshness"]["prices"], label
    assert k.missing_prices == ref["freshness"]["missing_prices"], label
    ref_fx = {f"{e['base']}/{e['quote']}": {"as_of": e["as_of"], "stale": e["stale"],
                                            "source": e["source"]}
              for e in ref["freshness"]["fx"]}
    mine = _fx_facts(k)
    assert set(mine) <= set(ref_fx), f"{label}: the KPIs read a pair the build did not"
    assert mine == {pair: ref_fx[pair] for pair in mine}, label
    assert k.missing_fx == [p for p in ref["freshness"]["missing_fx"] if p in mine], label


def test_every_month_end_close_is_the_truncated_world(corpus: tuple[str, sqlite3.Connection]
                                                      ) -> None:
    name, conn = corpus
    entry = _manifest()["corpora"][name]
    reporting = Currency(entry["reporting"])
    ends = [date.fromisoformat(d) for d in entry["month_ends"]]
    assert ends, f"{name}: no month end to pin"
    bundle = _registered(conn)
    # The worker's ONE replay: every day a row counts from, and every month end.
    days = sorted(bundle.counts_on_dates() | set(ends))
    books = dict(zip(days, iter_books(bundle, days), strict=True))
    movements = list_cash_movements(conn)
    cache = FxSeriesCache(conn)  # one cache for the whole run
    for d in ends:
        source = OnOrBefore(conn, d, fx_cache=cache)
        k = kpis_as_of(bundle, book=books[d],
                       cash_movements=valued_rows_as_of(movements, d),
                       prices=source, fx=source, as_of=d, reporting=reporting)
        ref = json.loads((_PAYLOADS / name / "month_end" / f"{d:%Y-%m}.json").read_bytes())
        _assert_matches(f"{name} {d}", k, ref)


def test_the_pairs_only_the_fx_summary_reads_are_named(corpus: tuple[str, sqlite3.Connection]
                                                       ) -> None:
    """Every pair the reference lists that a period close's KPIs did not read is one the
    換匯損益 summary reads: a funding / settlement currency of an FX-exposed account."""
    name, conn = corpus
    entry = _manifest()["corpora"][name]
    reporting = Currency(entry["reporting"])
    bundle = _registered(conn)
    exposed = {c.value for row in conn.execute(
        "SELECT settlement_ccy, funding_ccy FROM accounts WHERE settlement_ccy != funding_ccy")
        for c in (Currency(row[0]), Currency(row[1]))} | {reporting.value}
    movements = list_cash_movements(conn)
    for d_text in entry["month_ends"]:
        d = date.fromisoformat(d_text)
        source = OnOrBefore(conn, d)
        k = kpis_as_of(bundle, book=None, cash_movements=valued_rows_as_of(movements, d),
                       prices=source, fx=source, as_of=d, reporting=reporting)
        ref = json.loads((_PAYLOADS / name / "month_end" / f"{d:%Y-%m}.json").read_bytes())
        for e in ref["freshness"]["fx"]:
            pair = f"{e['base']}/{e['quote']}"
            if pair not in _fx_facts(k):
                assert {e["base"], e["quote"]} <= exposed, f"{name} {d}: {pair}"


def test_the_live_sources_give_the_dashboards_kpis(corpus: tuple[str, sqlite3.Connection]
                                                   ) -> None:
    """With :class:`LatestStored` at the corpus clock, the same function gives the committed
    v0.1.31 dashboard's KPI fields."""
    name, conn = corpus
    corpus_def = CORPORA[name]
    now = corpus_def.now
    bundle = _registered(conn)
    source = LatestStored(conn, now=now)
    k = kpis_as_of(bundle, book=None,
                   cash_movements=valued_rows_as_of(list_cash_movements(conn), now.date()),
                   prices=source, fx=source, as_of=now.date(), reporting=corpus_def.reporting)
    ref = json.loads((_PAYLOADS / name / "dashboard.json").read_bytes())
    _assert_matches(name, k, ref)


def test_a_precomputed_book_is_used_not_rebuilt(corpus: tuple[str, sqlite3.Connection],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from portfolio_dash.portfolio import dashboard

    name, conn = corpus
    entry = _manifest()["corpora"][name]
    d = date.fromisoformat(entry["month_ends"][-1])
    bundle = _registered(conn)
    [book] = list(iter_books(bundle, [d]))

    def no_replay(*_a: object, **_k: object) -> None:
        raise AssertionError("kpis_as_of replayed although it was handed a book")

    monkeypatch.setattr(dashboard, "build_book", no_replay)
    source = OnOrBefore(conn, d)
    k = kpis_as_of(bundle, book=book, cash_movements=[], prices=source, fx=source,
                   as_of=d, reporting=Currency(entry["reporting"]))
    assert k.book is book
