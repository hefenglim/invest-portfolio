"""A share-adding dividend with no share count must be refused at the DOOR (AI-D71).

The replay already refuses it -- `cost_basis.py` raises "DRIP/STOCK dividend ... requires
reinvest_shares" rather than coercing to zero, and that raise is deliberate and tested.
The import door did not: `apply_dividend_model` passes the field straight through for
STOCK and only derives it for DRIP when a reinvest_price is present, while the M5
conservation gate `check_amounts(gross, 0, 0)` passes for any non-negative gross. So the
row landed with ZERO issues and every later rebuild of the whole book raised --
`tests/portfolio/test_cost_basis.py` records this class as having once "crashed every
rebuild that held a MY dividend". The replay side of that contract is already pinned
by `tests/portfolio/test_cost_basis.py::test_drip_without_reinvest_shares_raises`, so
it is referenced here rather than duplicated: this file guards the DOOR.

`.claude/rules/architecture.md`: data_ingestion "rejects bad input loudly; never silently
coerces". This path did not.

Found 2026-08-28 while tracing what a wrong `gross` on a stock dividend could actually
cost. The AI door can produce exactly this row.
"""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview

_HDR = ("account,symbol,date,type,gross,withholding,net,"
        "reinvest_shares,reinvest_price,ex_date")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    yield c
    c.close()


def _issues(conn: sqlite3.Connection, row: str) -> list[tuple[str, bool]]:
    preview = build_dividend_preview(conn, _HDR + "\n" + row)
    return [(i.kind, i.needs_confirm) for i in preview.rows[0].issues]


def test_stock_dividend_without_share_count_is_refused_at_the_door(
    conn: sqlite3.Connection,
) -> None:
    """RED before the guard: this row previously produced no issue at all."""
    kinds = [k for k, _ in _issues(conn, "tw_broker,2330,2026-06-01,STOCK,0,,,,,")]
    assert "reinvest_shares_required" in kinds


def test_that_refusal_is_HARD_not_a_confirmation_prompt(
    conn: sqlite3.Connection,
) -> None:
    """A needs-confirm issue can be ticked through; this row must never be writable.

    There is no 'yes I meant it' reading of a share dividend that adds no shares -- the
    replay cannot represent it, so confirming would only move the failure later.
    """
    found = [(k, nc) for k, nc in
             _issues(conn, "tw_broker,2330,2026-06-01,STOCK,0,,,,,")
             if k == "reinvest_shares_required"]
    assert found == [("reinvest_shares_required", False)]


def test_drip_with_neither_shares_nor_price_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """DRIP falls through the same hole -- the replay raises for it identically."""
    kinds = [k for k, _ in _issues(conn, "schwab,AAPL,2026-06-01,DRIP,105,,,,,")]
    assert "reinvest_shares_required" in kinds


def test_drip_that_can_DERIVE_its_shares_is_untouched(
    conn: sqlite3.Connection,
) -> None:
    """The guard must not break the legitimate path.

    `apply_dividend_model` computes DRIP shares from net / reinvest_price. That row is
    complete by the time the guard runs, so it must pass -- otherwise the guard would
    reject the ordinary US dividend flow, which is most of the real ledger.
    """
    kinds = [k for k, _ in _issues(conn, "schwab,AAPL,2026-06-01,DRIP,105,,,,210,")]
    assert "reinvest_shares_required" not in kinds


def test_an_explicit_share_count_passes(conn: sqlite3.Connection) -> None:
    kinds = [k for k, _ in _issues(conn, "tw_broker,2330,2026-06-01,STOCK,0,,,50,,")]
    assert "reinvest_shares_required" not in kinds


def test_cash_dividends_are_not_affected(conn: sqlite3.Connection) -> None:
    """CASH / NET add no shares, so the field is meaningless for them."""
    for row in ("tw_broker,2330,2026-06-01,CASH,1000,,,,,",
                "moomoo_my,1155,2026-08-05,NET,88.50,,,,,"):
        kinds = [k for k, _ in _issues(conn, row)]
        assert "reinvest_shares_required" not in kinds, row
