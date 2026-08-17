"""Tests for the external-snapshot ingest functions (spec 20.4).

Each ingest fn takes ``conn`` + injectable client callables (so tests monkeypatch
without network) and writes ``external_snapshots`` rows for the TW universe. The
universe is read by direct SQL on ``instruments`` (no data_ingestion import). Raw
payloads are stored verbatim (no Decimal coercion at ingest).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.pricing import fundamentals_source, ingest, snapshots_store

_NOW = datetime(2026, 6, 11, 18, 0)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    snapshots_store.ensure_tables(c)
    yield c
    c.close()


def _add_instrument(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'TWD', NULL, NULL, NULL)",
        (symbol, market),
    )
    conn.commit()


def test_tw_universe_direct_sql(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "AAPL", "US")
    _add_instrument(conn, "0050", "TW")
    assert ingest.tw_universe(conn) == ["0050", "2330"]


def test_ingest_chips_writes_rows_per_symbol(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "AAPL", "US")  # non-TW must be skipped

    inst_rows = [
        {"date": "2026-06-10", "name": "Foreign_Investor", "buy": 100, "sell": 40},
        {"date": "2026-06-11", "name": "Foreign_Investor", "buy": 200, "sell": 50},
    ]
    margin_rows = [{"date": "2026-06-11", "MarginPurchaseTodayBalance": 19280}]

    def fake_dataset(c: sqlite3.Connection, *, dataset: str, data_id: str,
                     start_date: str) -> list[dict[str, Any]]:
        assert data_id == "2330"  # only the TW symbol is fetched
        return inst_rows if dataset == "institutional" else margin_rows

    n = ingest.ingest_chips(conn, now=_NOW, fetch_dataset=fake_dataset)
    assert n == 2  # one institutional + one margin row for the single TW symbol

    inst = snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="institutional", symbol="2330"
    )
    assert inst is not None
    assert inst.as_of == date(2026, 6, 11)  # latest date in the data
    assert inst.payload["rows"] == inst_rows
    margin = snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="margin", symbol="2330"
    )
    assert margin is not None and margin.payload["rows"] == margin_rows


def test_ingest_chips_skips_empty(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")

    def empty_dataset(c: sqlite3.Connection, *, dataset: str, data_id: str,
                      start_date: str) -> list[dict[str, Any]]:
        return []

    n = ingest.ingest_chips(conn, now=_NOW, fetch_dataset=empty_dataset)
    assert n == 0
    assert snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="institutional", symbol="2330"
    ) is None


def test_ingest_valuation_writes_rows(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    rows = [{"date": "2026-06-11", "PER": "24.1", "PBR": "6.2", "dividend_yield": "1.8"}]

    def fake_dataset(c: sqlite3.Connection, *, dataset: str, data_id: str,
                     start_date: str) -> list[dict[str, Any]]:
        assert dataset == "valuation"
        return rows

    n = ingest.ingest_valuation(conn, now=_NOW, fetch_dataset=fake_dataset)
    assert n == 1
    snap = snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="valuation", symbol="2330"
    )
    assert snap is not None and snap.payload["rows"] == rows


def test_ingest_fundamentals_writes_both_datasets(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    rev = [{"date": "2026-05-31", "revenue": 250000}]
    fin = [{"date": "2026-03-31", "type": "EPS", "value": 14.2}]

    def fake_dataset(c: sqlite3.Connection, *, dataset: str, data_id: str,
                     start_date: str) -> list[dict[str, Any]]:
        return rev if dataset == "monthly_revenue" else fin

    n = ingest.ingest_fundamentals(conn, now=_NOW, fetch_dataset=fake_dataset)
    assert n == 2
    assert snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="monthly_revenue", symbol="2330"
    ) is not None
    assert snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="financials", symbol="2330"
    ) is not None


def test_ingest_sentiment_writes_vix_and_fng(conn: sqlite3.Connection) -> None:
    from decimal import Decimal

    n = ingest.ingest_sentiment(
        conn, now=_NOW,
        fetch_vix=lambda: Decimal("14.2"),
        fetch_fear_greed=lambda: {"score": Decimal("62"), "rating": "greed"},
    )
    assert n == 2
    vix = snapshots_store.latest_snapshot(conn, source="sentiment", dataset="vix", symbol=None)
    assert vix is not None and vix.symbol is None
    assert vix.payload["close"] == "14.2"  # Decimal stored as canonical string
    assert vix.as_of == date(2026, 6, 11)
    fng = snapshots_store.latest_snapshot(conn, source="sentiment", dataset="fng", symbol=None)
    assert fng is not None
    assert fng.payload["score"] == "62" and fng.payload["rating"] == "greed"


def test_ingest_sentiment_degrades_when_sources_down(conn: sqlite3.Connection) -> None:
    n = ingest.ingest_sentiment(
        conn, now=_NOW, fetch_vix=lambda: None, fetch_fear_greed=lambda: None
    )
    assert n == 0
    assert snapshots_store.latest_snapshot(
        conn, source="sentiment", dataset="vix", symbol=None
    ) is None


def test_ingest_index_writes_quotes(conn: sqlite3.Connection) -> None:
    from decimal import Decimal

    n = ingest.ingest_index(
        conn, now=_NOW,
        fetch_indices=lambda: {"^TWII": Decimal("22150.5"), "^GSPC": Decimal("5980.12")},
    )
    assert n == 1  # one index_quotes snapshot row (all indices in one payload)
    snap = snapshots_store.latest_snapshot(
        conn, source="index", dataset="index_quotes", symbol=None
    )
    assert snap is not None
    assert snap.payload["quotes"]["^TWII"] == "22150.5"
    assert snap.payload["quotes"]["^GSPC"] == "5980.12"


def test_ingest_index_degrades_when_empty(conn: sqlite3.Connection) -> None:
    n = ingest.ingest_index(conn, now=_NOW, fetch_indices=lambda: {})
    assert n == 0
    assert snapshots_store.latest_snapshot(
        conn, source="index", dataset="index_quotes", symbol=None
    ) is None


# --- consensus ingest (P1 batch 2): all-market universe + yf symbol mapping ----


def test_all_universe_all_markets(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "AAPL", "US")
    _add_instrument(conn, "1155", "MY")
    refs = ingest.all_universe(conn)
    assert {r.symbol for r in refs} == {"2330", "AAPL", "1155"}


def test_ingest_consensus_maps_yf_symbols_and_keys_by_portfolio_symbol(
    conn: sqlite3.Connection,
) -> None:
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "AAPL", "US")
    _add_instrument(conn, "1155", "MY")
    seen: list[str] = []

    def fake_fetch(yf_sym: str, *, as_of: date) -> dict[str, Any]:
        seen.append(yf_sym)
        return {"as_of": as_of.isoformat(), "ratings": {"total": 1}, "source": "yfinance"}

    n = ingest.ingest_consensus(conn, now=_NOW, fetch_consensus=fake_fetch)
    assert n == 3
    # yfinance symbol mapping is reused (.TW / .KL / bare US).
    assert set(seen) == {"2330.TW", "AAPL", "1155.KL"}
    # stored keyed by the PORTFOLIO symbol, not the yf symbol.
    snap = snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="2330"
    )
    assert snap is not None and snap.as_of == date(2026, 6, 11)


def test_ingest_consensus_skips_uncovered(conn: sqlite3.Connection) -> None:
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "ZZZ", "US")

    def fake_fetch(yf_sym: str, *, as_of: date) -> dict[str, Any] | None:
        return None if yf_sym == "ZZZ" else {"as_of": as_of.isoformat(), "source": "x"}

    n = ingest.ingest_consensus(conn, now=_NOW, fetch_consensus=fake_fetch)
    assert n == 1
    assert snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="ZZZ"
    ) is None


def test_ingest_consensus_isolates_symbol_exception(conn: sqlite3.Connection) -> None:
    # One symbol raising must not drop the others.
    _add_instrument(conn, "2330", "TW")
    _add_instrument(conn, "AAPL", "US")

    def flaky(yf_sym: str, *, as_of: date) -> dict[str, Any]:
        if yf_sym == "AAPL":
            raise RuntimeError("boom")
        return {"as_of": as_of.isoformat(), "source": "yfinance"}

    n = ingest.ingest_consensus(conn, now=_NOW, fetch_consensus=flaky)
    assert n == 1
    assert snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="2330"
    ) is not None
    assert snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="AAPL"
    ) is None


def test_ingest_consensus_reread_is_idempotent(conn: sqlite3.Connection) -> None:
    # Append-only store: a re-run leaves latest_snapshot returning the newest payload.
    _add_instrument(conn, "2330", "TW")
    calls = {"n": 0}

    def fake_fetch(yf_sym: str, *, as_of: date) -> dict[str, Any]:
        calls["n"] += 1
        return {"as_of": as_of.isoformat(), "rating_score": str(calls["n"]),
                "source": "yfinance"}

    ingest.ingest_consensus(conn, now=_NOW, fetch_consensus=fake_fetch)
    ingest.ingest_consensus(conn, now=_NOW, fetch_consensus=fake_fetch)
    snap = snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="2330"
    )
    assert snap is not None and snap.payload["rating_score"] == "2"  # newest wins on read


# --- fundamentals union ingest (W3, AI-D13/D14): every enabled source writes its own
# row; no fallback chain, no merge. The registry's capable_ids is the capability+key
# gate; a stub registry keeps the tests deterministic (and off the env). ---------------


class _StubRegistry:
    """Only the seam ingest_fundamentals_union uses: capable_ids()."""

    def __init__(self, enabled: dict[str, list[str]]) -> None:
        self._enabled = enabled  # market value -> source ids

    def capable_ids(self, data_type: object, market: Any) -> list[str]:
        return self._enabled.get(str(market), [])


def _stub_registry(monkeypatch: pytest.MonkeyPatch, enabled: dict[str, list[str]]) -> None:
    stub = _StubRegistry(enabled)
    monkeypatch.setattr(ingest, "default_registry", lambda conn=None: stub)


def _fake_block(symbol: str) -> dict[str, Any]:
    return {"as_of": "2026-06-11", "currency": "USD", "pe_ratio": "25", "sym": symbol}


def test_fundamentals_union_writes_one_row_per_enabled_source(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add_instrument(conn, "AAPL", "US")
    _stub_registry(monkeypatch, {"US": ["alphavantage", "finnhub", "yfinance"]})
    n = ingest.ingest_fundamentals_union(
        conn, now=_NOW,
        fetchers={s: lambda ref, *, as_of, token, _s=s: _fake_block(_s) for s in
                  ("yfinance", "finnhub", "alphavantage")},
    )
    assert n == 3  # UNION: all three rows coexist (PK carries source)
    for source in ("yfinance", "finnhub", "alphavantage"):
        snap = snapshots_store.latest_snapshot(
            conn, source=source, dataset="fundamentals", symbol="AAPL"
        )
        assert snap is not None and snap.payload["sym"] == source


def test_fundamentals_union_keyless_sources_write_nothing(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # The gate: only yfinance is capable (keyless finnhub/AV absent from capable_ids).
    _add_instrument(conn, "AAPL", "US")
    _stub_registry(monkeypatch, {"US": ["yfinance"]})
    n = ingest.ingest_fundamentals_union(
        conn, now=_NOW,
        fetchers={"yfinance": lambda ref, *, as_of, token: _fake_block("yfinance")},
    )
    assert n == 1
    assert snapshots_store.latest_snapshot(
        conn, source="finnhub", dataset="fundamentals", symbol="AAPL"
    ) is None


def test_fundamentals_union_sources_and_universe_are_restricted(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # The AV Saturday leg: sources=("alphavantage",) + an injected held-only universe.
    _add_instrument(conn, "AAPL", "US")
    _add_instrument(conn, "MSFT", "US")  # registered but NOT in the injected universe
    _stub_registry(monkeypatch, {"US": ["alphavantage", "yfinance"]})
    held = [r for r in ingest.all_universe(conn) if r.symbol == "AAPL"]
    n = ingest.ingest_fundamentals_union(
        conn, now=_NOW, sources=("alphavantage",), universe=held,
        fetchers={"alphavantage": lambda ref, *, as_of, token: _fake_block("av")},
    )
    assert n == 1
    assert snapshots_store.latest_snapshot(
        conn, source="alphavantage", dataset="fundamentals", symbol="AAPL"
    ) is not None
    assert snapshots_store.latest_snapshot(
        conn, source="alphavantage", dataset="fundamentals", symbol="MSFT"
    ) is None
    assert snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="fundamentals", symbol="AAPL"
    ) is None  # yfinance not in this job's sources


def test_fundamentals_union_isolates_per_source_failure(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add_instrument(conn, "AAPL", "US")
    _stub_registry(monkeypatch, {"US": ["finnhub", "yfinance"]})

    def boom(ref: object, *, as_of: object, token: object) -> None:
        raise RuntimeError("finnhub 500")

    n = ingest.ingest_fundamentals_union(
        conn, now=_NOW,
        fetchers={"finnhub": boom,
                  "yfinance": lambda ref, *, as_of, token: _fake_block("yfinance")},
    )
    assert n == 1  # the yfinance row survives finnhub's failure


def test_fundamentals_union_ledger_only_db_degrades_to_keyless(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # The pricing conn fixture is bootstrap_db-only: NO data_sources table. The keyed
    # providers' token getters raise OperationalError -> only the key-less leg runs.
    _add_instrument(conn, "AAPL", "US")
    monkeypatch.delenv("FINNHUB_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_KEY", raising=False)
    monkeypatch.setitem(
        fundamentals_source.FETCHERS, "yfinance",
        lambda ref, *, as_of, token=None: _fake_block("yfinance"),
    )
    n = ingest.ingest_fundamentals_union(conn, now=_NOW)
    assert n == 1
    assert snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="fundamentals", symbol="AAPL"
    ) is not None
