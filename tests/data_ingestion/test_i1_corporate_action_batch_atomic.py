"""I-1 (F-2): a corporate-action CSV batch is ALL-OR-NOTHING, like the five other kinds.

``write_corporate_action_row`` deferred its band move and its SPINOFF child registration to
the batch's single commit — and then called ``insert_corporate_action`` WITHOUT forwarding
``commit``, so the insert's own default (``commit=True``) committed every row the moment it
was written. ``commit_preview``'s rollback therefore had nothing left to roll back: a
three-row file whose third row failed kept the first two, and a band that row one had moved
stayed moved (measured: ``write_corporate_action_row(commit=False)`` then ``rollback()`` left
one row standing).

The class guard is ``tests/architecture/test_commit_forwarding.py`` (every function that takes
``commit`` forwards it to every commit-capable callee); these tests pin the observable
contract the guard protects.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import corporate_action_import as cai
from portfolio_dash.data_ingestion.corporate_action_import import (
    corporate_action_writer,
    write_corporate_action_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview
from portfolio_dash.data_ingestion.store import (
    MovedWeight,
    get_instrument,
    insert_corporate_action,
    list_corporate_actions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

D = Decimal
DAY = date(2026, 6, 15)


def _no_weight(_frm: str, _to: str) -> MovedWeight | None:
    """A ledger with no target weights: the injected mover finds nothing to move."""
    return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES "
        "('schwab','Schwab','Schwab','USD','TWD','schwab_us','drip_us')")
    for sym in ("AAA", "BBB", "CCC", "DDD"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    # The owner's alert band on AAA — the EXCHANGE in row 0 moves it onto BBB.
    c.execute("UPDATE instruments SET target_low='40', target_high='55', "
              "target_set_at='2026-01-02' WHERE symbol='AAA'")
    c.commit()
    return c


def _row(index: int, **over: str) -> PreviewRow:
    payload = {"account_id": "schwab", "date": DAY.isoformat(), "kind": "SPLIT",
               "from_symbol": "CCC", "to_symbol": "CCC", "ratio_to": "2", "ratio_from": "1"}
    payload.update(over)
    return PreviewRow(index=index, raw={}, payload=payload)


def test_a_deferred_row_is_rolled_back_with_its_transaction(
    conn: sqlite3.Connection
) -> None:
    """The probe's own shape: ``commit=False`` means the caller's rollback takes the row."""
    write_corporate_action_row(conn, _row(0), move_weight=_no_weight, commit=False)
    conn.rollback()
    assert list_corporate_actions(conn) == []


def test_a_batch_whose_third_row_fails_writes_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ One batch, three rows, the third fails → the first two are NOT in the ledger, and
    the band row one moved is back on its source (the move rode the same transaction)."""
    rows = [
        _row(0, kind="EXCHANGE", from_symbol="AAA", to_symbol="BBB", ratio_to="1",
             ratio_from="1"),
        _row(1),
        _row(2, from_symbol="DDD", to_symbol="DDD"),
    ]
    real = insert_corporate_action
    calls = {"n": 0}

    def _third_fails(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(cai, "insert_corporate_action", _third_fails)
    with pytest.raises(RuntimeError, match="disk full"):
        commit_preview(conn, ImportPreview(rows=rows), accept={0, 1, 2},
                       writer=corporate_action_writer(move_weight=_no_weight))
    assert list_corporate_actions(conn) == []
    aaa, bbb = get_instrument(conn, "AAA"), get_instrument(conn, "BBB")
    assert aaa is not None and bbb is not None
    assert (aaa.target_low, aaa.target_high) == (D("40"), D("55"))
    assert (bbb.target_low, bbb.target_high) == (None, None)


def test_the_weight_mover_is_a_required_injection() -> None:
    """architecture.md's injection obligation (1): no default, so a door that forgets to
    bind it is a TypeError, not an EXCHANGE whose target weight silently stays behind."""
    import inspect

    for fn in (write_corporate_action_row, corporate_action_writer):
        param = inspect.signature(fn).parameters["move_weight"]
        assert param.default is inspect.Parameter.empty, fn.__name__
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
