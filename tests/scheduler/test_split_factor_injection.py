"""``factor_of`` injection: the ledger reaches the price write seam (W6a, D17).

``pricing/`` may not import ``data_ingestion`` (``architecture.md``), so the ratio
lookup is a callable that this layer binds. These tests cover the binding itself
(:func:`split_factor_fn`) and the end-to-end effect through a real refresh job with a
faked provider — the path where a backfilled, provider-re-stated close is turned back
into the as-traded value the ledger's share count for that date is expressed in.
"""

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import backfill_history_all, split_factor_fn
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_SPLIT_DAY = date(2026, 6, 8)
_PRE = date(2026, 6, 5)  # a session BEFORE the split — the provider re-states this one
_POST = date(2026, 6, 9)  # a session after it — already in post-split terms


def _split(conn: sqlite3.Connection, account: str = "schwab", symbol: str = "AAPL",
           to_: str = "20", from_: str = "1", when: date = _SPLIT_DAY) -> None:
    insert_corporate_action(
        conn, account_id=account, action_date=when, kind=CorporateActionKind.SPLIT,
        from_symbol=symbol, to_symbol=symbol, ratio_to=Decimal(to_),
        ratio_from=Decimal(from_),
    )


# --- the binding --------------------------------------------------------------------


def test_no_actions_yields_the_exact_identity(conn: sqlite3.Connection) -> None:
    """An empty ledger gives ``Decimal(1)`` — and specifically the ``exponent == 0``
    form, because a scaled identity like ``Decimal("1.0")`` would repaint stored TEXT."""
    factor_of = split_factor_fn(conn)
    got = factor_of("AAPL", after=_PRE, through=_NOW.date())
    assert got == Decimal(1) and got.as_tuple().exponent == 0


def test_split_in_the_window_is_applied(conn: sqlite3.Connection) -> None:
    _split(conn)
    factor_of = split_factor_fn(conn)
    assert factor_of("AAPL", after=_PRE, through=_NOW.date()) == Decimal(20)


def test_split_outside_the_window_is_not(conn: sqlite3.Connection) -> None:
    """A row fetched AFTER its own session but before the split carries no factor; nor
    does a session on/after the split date (the provider needed no re-statement)."""
    _split(conn)
    factor_of = split_factor_fn(conn)
    assert factor_of("AAPL", after=_PRE, through=date(2026, 6, 7)) == Decimal(1)
    assert factor_of("AAPL", after=_POST, through=_NOW.date()) == Decimal(1)


def test_other_symbols_are_untouched(conn: sqlite3.Connection) -> None:
    _split(conn)
    assert split_factor_fn(conn)("2330", after=_PRE, through=_NOW.date()) == Decimal(1)


def test_the_same_split_in_three_accounts_is_one_factor(conn: sqlite3.Connection) -> None:
    """A split is a MARKET fact; the ledger row is per-ACCOUNT and ``prices`` has no
    account. Three identical rows must give 3, not 27 (spec §5.1 detail 3)."""
    for acct in ("schwab", "moomoo_my", "tw_broker"):
        _split(conn, account=acct, to_="3", from_="1")
    assert split_factor_fn(conn)("AAPL", after=_PRE, through=_NOW.date()) == Decimal(3)


def test_non_split_kinds_are_ignored(conn: sqlite3.Connection) -> None:
    """EXCHANGE is excluded by D22 — it ADDS to its destination rather than
    re-denominating it, so a factor there would corrupt the destination's history."""
    insert_corporate_action(
        conn, account_id="schwab", action_date=_SPLIT_DAY,
        kind=CorporateActionKind.EXCHANGE, from_symbol="OLD", to_symbol="AAPL",
        ratio_to=Decimal(2), ratio_from=Decimal(7),
    )
    assert split_factor_fn(conn)("AAPL", after=_PRE, through=_NOW.date()) == Decimal(1)


# --- end to end through a job -------------------------------------------------------


class _FakeHistory(ProviderBase):
    """A provider that behaves like a real one AFTER a split: every historical close it
    serves is already expressed in post-split terms."""

    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        return [
            PriceRow(instrument=instrument.symbol, market=instrument.market, as_of=_PRE,
                     close=Decimal("6.5"), source=self.name),
            PriceRow(instrument=instrument.symbol, market=instrument.market, as_of=_POST,
                     close=Decimal("6.75"), source=self.name),
        ]


def _prepare(conn: sqlite3.Connection) -> None:
    create_pricing_tables(conn)
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES ('AAPL', 'US', 'USD', NULL, NULL, NULL)"
    )
    conn.commit()


def _fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeHistory()
    reg = Registry(providers={provider.name: provider},
                   order={(DataType.QUOTE_HISTORY, Market.US): [provider.name]})
    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: reg)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history",
                        lambda *a, **kw: jobs_mod.RefreshSummary(fetched_at=kw["now"]))
    monkeypatch.setattr(jobs_mod, "_backfill_benchmarks", lambda *a, **kw: "skipped")


def test_backfill_un_adjusts_a_pre_split_close(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The whole point of W6a, exercised through the real job.

    The 2026-06-05 session traded at 130; after a 20-for-1 the provider serves it as
    6.50. The ledger's share count for that date is in PRE-split terms, so the stored
    price must be too — 130, with the basis recorded. The post-split session needed no
    re-statement and is stored as delivered.
    """
    _prepare(conn)
    _split(conn)
    _fake_registry(monkeypatch)
    backfill_history_all(conn, now=_NOW, days=30)
    rows = {r["as_of_date"]: (r["close"], r["close_raw"], r["split_basis"])
            for r in conn.execute(
                "SELECT as_of_date, close, close_raw, split_basis FROM prices")}
    # 6.5 x 20 == Decimal("130.0"): a genuine product keeps the exponent sum, which is
    # cosmetic and reaches ONLY rows that actually carried an action.
    assert rows[_PRE.isoformat()] == ("130.0", "6.5", "20")
    assert rows[_POST.isoformat()] == ("6.75", "6.75", "1")


def test_backfill_without_actions_stores_the_provider_value(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The same job on a ledger with no corporate action moves nothing, byte for byte."""
    _prepare(conn)
    _fake_registry(monkeypatch)
    backfill_history_all(conn, now=_NOW, days=30)
    rows = {r["as_of_date"]: (r["close"], r["split_basis"])
            for r in conn.execute("SELECT as_of_date, close, split_basis FROM prices")}
    assert rows == {_PRE.isoformat(): ("6.5", "1"), _POST.isoformat(): ("6.75", "1")}
