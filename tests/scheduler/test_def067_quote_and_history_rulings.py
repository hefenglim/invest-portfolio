"""DEF-067 owner rulings of 2026-09-26 (items ① and ④).

① The quote jobs join the other sweeps: EVERY instrument of the market lost → ``error``
  (失敗). A held instrument lost while others updated stays ``partial`` (M10-02), and a
  watchlist-only loss keeps today's rule (``ok``, named in the detail). 3be67db — and the
  first R6 pass — recorded 部分 even when nothing at all was updated.

④ ``history_daily`` counts only a PROVIDER failure as lost. A provider that answered with no
  bars for the 7-day window (a long holiday closure, a delisted watchlist symbol) is not a
  failure: 「區間內無 K 棒（休市或已下市）」. ``Registry.fetch_quote_history`` treated an
  exception and an empty answer the same; ``fetch_quote_history_explained`` tells them apart
  (DEF-047's ``empty``, for dividends, is the model).

  ⚠ yfinance — first in every history chain — does NOT raise on a network failure: it logs
  and returns an empty frame (``YfConfig.debug.hide_exceptions`` defaults to True). An empty
  answer is therefore trusted only when the SAME run proves one of the market's providers
  was reachable (it returned bars for another instrument or a benchmark). With every answer
  empty, the likelier story is an outage, and the symbols count as lost — otherwise the
  all-providers-down environment of E-05 would read 成功 again, the very defect.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api.routers.actions import held_symbols
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_history
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import PriceRow, RefreshSummary
from portfolio_dash.pricing.store import _no_factor
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import register_held_symbols_fn, run_job_outcome
from portfolio_dash.shared.enums import Market

NOW = datetime(2026, 9, 25, 2, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class _Scripted(ProviderBase):
    """A history provider scripted per symbol: ``bars`` / ``empty`` / ``raise``."""

    def __init__(self, name: str, script: dict[str, str], default: str = "bars") -> None:
        self.name = name
        self.script = script
        self.default = default

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_HISTORY

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        what = self.script.get(instrument.symbol, self.default)
        if what == "raise":
            raise ConnectionError("provider unreachable")
        if what == "empty":
            return []
        return [PriceRow(instrument=instrument.symbol, market=instrument.market,
                         as_of=start + timedelta(days=1), close=Decimal("10"),
                         source=self.name)]


def _registry(*providers: _Scripted) -> Registry:
    names = [p.name for p in providers]
    return Registry(providers={p.name: p for p in providers},
                    order={(DataType.QUOTE_HISTORY, m): names for m in Market})


@pytest.fixture(autouse=True)
def _seams() -> Iterator[None]:
    register_held_symbols_fn(held_symbols)
    jobs._INFLIGHT_JOBS.clear()
    yield
    register_held_symbols_fn(None)
    jobs._INFLIGHT_JOBS.clear()


# --- ① quote jobs ----------------------------------------------------------------------


def _watch(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'USD', 'Tech', ?, NULL)", (symbol, market, symbol))
    conn.commit()


def _quotes_answering(monkeypatch: pytest.MonkeyPatch, answered: set[str]) -> None:
    def refresh(conn: Any, registry: Any, refs: list[Any], pairs: list[Any],
                **kw: Any) -> RefreshSummary:
        ok = {r.symbol: "yfinance" for r in refs if r.symbol in answered}
        return RefreshSummary(ok=ok, failed=[r.symbol for r in refs if r.symbol not in ok],
                              fetched_at=kw["now"])

    monkeypatch.setattr(jobs, "refresh_quotes", refresh)


def test_every_instrument_lost_is_error(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "default_registry",
                        lambda conn=None: Registry(providers={}, order={}))
    for job_id in ("quotes_tw", "quotes_us"):
        _, outcome = run_job_outcome(golden_db, job_id, now=NOW)
        assert outcome.status == "error", job_id
        assert outcome.results["instruments_updated"] == 0  # the counts still travel


def test_a_held_symbol_lost_while_others_updated_stays_partial(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _watch(golden_db, "MSFT", "US")
    _quotes_answering(monkeypatch, {"MSFT"})            # AAPL (held) lost, MSFT updated
    _, outcome = run_job_outcome(golden_db, "quotes_us", now=NOW)
    assert (outcome.status, outcome.results["held_failed"]) == ("partial", ["AAPL"])


def test_a_watchlist_only_loss_keeps_todays_rule(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _watch(golden_db, "MSFT", "US")
    _quotes_answering(monkeypatch, {"AAPL"})            # only the watch symbol lost
    _, outcome = run_job_outcome(golden_db, "quotes_us", now=NOW)
    assert outcome.status == "ok"
    assert "MSFT" in outcome.detail


def test_a_market_with_nothing_to_quote_is_not_an_error(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden ledger has no MY instrument: the job only asks for FX, and a lost pair
    alone never changed the verdict (M10-02)."""
    monkeypatch.setattr(jobs, "default_registry",
                        lambda conn=None: Registry(providers={}, order={}))
    _, outcome = run_job_outcome(golden_db, "quotes_my", now=NOW)
    assert outcome.status == "ok"


# --- ④ history: an empty answer is not a provider failure ------------------------------


def test_registry_tells_an_empty_answer_from_a_failure() -> None:
    reg = _registry(_Scripted("a", {"X": "raise", "Y": "empty", "Z": "empty"}),
                    _Scripted("b", {"X": "raise", "Y": "empty", "Z": "bars"}))
    refs = [InstrumentRef(symbol=s, market=Market.US) for s in ("X", "Y", "Z")]
    rows, sources, failed, empty = reg.fetch_quote_history_explained(refs, date(2026, 9, 1))
    assert failed == ["X"]                  # every provider raised
    assert empty == ["Y"]                   # a provider answered, with nothing
    assert sources == {"Z": "b"} and [r.instrument for r in rows] == ["Z"]
    # The legacy three-tuple is unchanged: an empty answer still reads as "no data".
    assert reg.fetch_quote_history(refs, date(2026, 9, 1))[2] == ["X", "Y"]


def test_refresh_history_carries_empty_out_of_failed(golden_db: sqlite3.Connection) -> None:
    reg = _registry(_Scripted("a", {"Y": "empty", "X": "raise"}))
    refs = [InstrumentRef(symbol=s, market=Market.US) for s in ("X", "Y")]
    s = refresh_history(golden_db, reg, refs, date(2026, 9, 1), now=NOW, factor_of=_no_factor)
    assert (s.failed, s.empty) == (["X"], ["Y"])


def _history(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
             script: dict[str, str], default: str = "bars") -> tuple[str, str]:
    reg = _registry(_Scripted("yf", script, default))
    monkeypatch.setattr(jobs, "default_registry", lambda conn=None: reg)
    _, outcome = run_job_outcome(conn, "history_daily", now=NOW)
    return outcome.status, outcome.detail


def test_a_holiday_window_is_not_a_failure(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TW closed for the whole window (2330 and 0050 answer empty), the US traded."""
    status, detail = _history(golden_db, monkeypatch, {"2330": "empty", "0050": "empty"})
    assert status == "ok", detail
    assert "1 項區間內無 K 棒（休市或已下市）：2330" in detail
    assert "失敗" not in detail.split("・基準指數")[0]


def test_a_tw_only_holiday_is_proven_by_a_benchmark(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every INSTRUMENT answered empty, but ^GSPC came back: the provider is reachable."""
    status, _ = _history(golden_db, monkeypatch,
                         {"2330": "empty", "AAPL": "empty", "0050": "empty"})
    assert status == "ok"


def test_an_outage_that_looks_empty_is_still_lost(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yfinance reports a network failure as an empty answer: nothing proves it answered."""
    status, detail = _history(golden_db, monkeypatch, {}, default="empty")
    assert status == "error", detail
    assert "無 K 棒" not in detail
    assert "來源回應空白" in detail


def test_a_provider_exception_is_still_lost(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, detail = _history(golden_db, monkeypatch, {"2330": "raise"})
    assert status == "partial", detail


def test_the_deep_backfill_still_counts_an_empty_symbol_as_failed(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over a multi-year window an empty answer means the provider has no series for the
    symbol at all — the backfill keeps reporting it as a failure, exactly as before."""
    reg = _registry(_Scripted("yf", {"2330": "empty"}))
    monkeypatch.setattr(jobs, "default_registry", lambda conn=None: reg)
    detail = jobs.backfill_history_all(golden_db, now=NOW, days=30)
    prices = detail.split("・匯率")[0]
    assert "失敗：2330" in prices and "無 K 棒" not in detail
