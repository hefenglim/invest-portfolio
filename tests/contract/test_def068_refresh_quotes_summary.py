"""DEF-068: the refresh-quotes door hands the toast the COUNTS it needs — the toast only formats.

3be67db: ``web/shell.js`` read ``results[job].held_failed`` alone and always went on
「其餘已更新，重新整理頁面即可看到新價」 — with every provider down, that sentence sat under
「9 檔持倉未更新」 while 0 instruments had been updated, and the two lost FX pairs were never
named. The API carried the lists but no ``updated`` count, so an honest 「N 檔已更新」 would
have had to be computed in the browser.

The fix adds one ``summary`` block over all the jobs of the request (built from each job's
structured ``results``, never parsed from a detail sentence): ``instruments`` /
``instruments_updated`` / ``instruments_failed`` / ``held_failed`` / ``fx_failed`` /
``all_failed``. An FX pair counts as failed only if EVERY job that asked for it failed —
the three market jobs all ask for the same two USD legs, and one success updates the rate.
"""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers.actions import held_symbols
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import (
    JobOutcome,
    combine_quote_results,
    register_held_symbols_fn,
)


@pytest.fixture(autouse=True)
def _held_seam() -> Iterator[None]:
    register_held_symbols_fn(held_symbols)
    yield
    register_held_symbols_fn(None)


def test_every_provider_down_is_all_failed_and_names_the_fx_pairs(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The E-11 evidence shape: nothing answered, in any market."""
    monkeypatch.setattr(
        jobs, "default_registry", lambda conn=None: Registry(providers={}, order={})
    )
    b = api_client.post("/api/actions/refresh-quotes", json={}).json()
    s = b["summary"]
    assert s["instruments"] == 2 and s["instruments_updated"] == 0
    assert s["instruments_not_updated"] == 2
    assert s["instruments_failed"] == ["2330", "AAPL"]
    assert s["held_failed"] == ["2330", "AAPL"]
    assert s["fx_failed"] == ["USDMYR", "USDTWD"]
    assert s["all_failed"] is True
    # Per-job blocks keep their old shape and gain the count the summary is built from.
    assert b["results"]["quotes_us"]["instruments_updated"] == 0


def test_a_partial_refresh_counts_what_was_updated(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refresh(conn: Any, registry: Any, instruments: list[Any], fx_pairs: list[Any],
                **kw: Any) -> RefreshSummary:
        ok = {r.symbol: "twse" for r in instruments if r.symbol == "2330"}
        ok["USDTWD"] = "yfinance"  # one USD leg answered, the other did not
        return RefreshSummary(ok=ok, failed=["AAPL", "USDMYR"], fetched_at=kw["now"])

    monkeypatch.setattr(jobs, "refresh_quotes", refresh)
    s = api_client.post("/api/actions/refresh-quotes", json={}).json()["summary"]
    assert (s["instruments"], s["instruments_updated"], s["instruments_not_updated"]) == (2, 1, 1)
    assert s["instruments_failed"] == ["AAPL"] and s["held_failed"] == ["AAPL"]
    assert s["fx_failed"] == ["USDMYR"]
    assert s["all_failed"] is False


def test_a_pair_updated_by_any_job_is_not_failed() -> None:
    """The three market jobs each ask for USDTWD + USDMYR; one success updates the rate."""
    a = JobOutcome("partial", "", {"instruments": 1, "instruments_updated": 0,
                                    "instruments_failed": ["X"], "held_failed": ["X"],
                                    "fx_pairs": ["USDMYR", "USDTWD"],
                                    "fx_failed": ["USDMYR", "USDTWD"]})
    b = JobOutcome("ok", "", {"instruments": 1, "instruments_updated": 1,
                               "instruments_failed": [], "held_failed": [],
                               "fx_pairs": ["USDMYR", "USDTWD"], "fx_failed": ["USDMYR"]})
    s = combine_quote_results([a, b])
    assert s["fx_failed"] == ["USDMYR"]
    assert (s["instruments"], s["instruments_updated"], s["all_failed"]) == (2, 1, False)


def test_an_empty_worklist_is_not_all_failed() -> None:
    """A fresh ledger has nothing to quote: that is not a failed refresh."""
    empty = JobOutcome("ok", "", {"instruments": 0, "instruments_updated": 0,
                                   "instruments_failed": [], "held_failed": [],
                                   "fx_pairs": [], "fx_failed": []})
    assert combine_quote_results([empty])["all_failed"] is False
