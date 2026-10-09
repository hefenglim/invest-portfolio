"""DEF-014 (functional test B-17, 2026-09-22): a trade dated before the position's
opening inventory, or before the account's first record, is no longer accepted in silence.

**Measured defect.** 8299 had opening inventory of 500 股 built 2026-07-21. A buy of
100@45.20 dated 2026-07-01 previewed 「✓ 草稿檢核通過」 and wrote with 201 — yet the opening
already contains the position as of its build date, so the buy is most likely a duplicate
of what the opening describes. Nothing in ``validate_transaction`` looked at the opening's
date at all; only the FUTURE side of the date had a check.

Two findings, two tiers, one shared validator so every door gets both:

* ``trade_before_opening`` — SOFT (``needs_confirm``), the same mechanism as
  ``future_trade_date``: the row is written only after the owner confirms it is not a
  duplicate. The account is a ``{account:<id>}`` token the fetch layer resolves (DEF-023).
* ``trade_before_ledger_start`` — ADVISORY (``info``): the trade predates every record of
  the account. Shown, never gating: the first row of a back-filled history is dated before
  everything by definition.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.data_ingestion.validate import (
    Issue,
    TxnInput,
    advisory_issue,
    validate_transaction,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="8299", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name="群聯"))
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金"))
    upsert_opening(conn, account_id="tw_broker", symbol="8299", shares=D("500"),
                   original_cost_total=D("200000"), build_date=date(2026, 7, 21))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=D("100"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    conn.commit()


def _buy(symbol: str, day: date, qty: str = "100") -> TxnInput:
    return TxnInput(account_id="tw_broker", symbol=symbol, side=Side.BUY, quantity=D(qty),
                    price=D("45.20"), trade_date=day)


def _by_kind(issues: list[Issue]) -> dict[str, Issue]:
    return {i.kind: i for i in issues}


def test_a_trade_before_the_openings_build_date_needs_confirmation(
    conn: sqlite3.Connection,
) -> None:
    _seed(conn)
    found = _by_kind(validate_transaction(conn, _buy("8299", date(2026, 7, 1))))
    issue = found["trade_before_opening"]
    assert issue.needs_confirm is True and issue.info is False
    assert "2026-07-01" in issue.message and "2026-07-21" in issue.message
    assert "{account:tw_broker}" in issue.message          # the token, never 「tw_broker」
    assert "期初庫存建檔日" in issue.message and "重複登錄" in issue.message
    # The advisory is NOT stacked on top of it: one sentence per cause.
    assert "trade_before_ledger_start" not in found


def test_a_trade_on_or_after_the_build_date_is_silent(conn: sqlite3.Connection) -> None:
    _seed(conn)
    for day in (date(2026, 7, 21), date(2026, 7, 22)):
        found = _by_kind(validate_transaction(conn, _buy("8299", day)))
        assert "trade_before_opening" not in found
        assert "trade_before_ledger_start" not in found


def test_a_trade_before_the_accounts_first_record_is_an_advisory(
    conn: sqlite3.Connection,
) -> None:
    """2884 has no opening; the account's earliest record is 2026-01-05."""
    _seed(conn)
    found = _by_kind(validate_transaction(conn, _buy("2884", date(2025, 1, 2))))
    issue = found["trade_before_ledger_start"]
    assert issue.info is True
    assert issue.needs_confirm is True, "an advisory must never read as HARD"
    assert "2026-01-05" in issue.message and "請確認日期" in issue.message
    assert "trade_before_opening" not in found
    # ...and a trade on the first day, or after it, says nothing.
    assert "trade_before_ledger_start" not in _by_kind(
        validate_transaction(conn, _buy("2884", date(2026, 1, 5))))


def test_the_earliest_record_is_read_across_all_five_ledgers(conn: sqlite3.Connection) -> None:
    """A cash deposit older than every trade moves the account's start back."""
    _seed(conn)
    conn.execute(
        "INSERT INTO cash_movements (account_id, date, kind, ccy, amount) "
        "VALUES ('tw_broker', '2025-06-30', 'DEPOSIT', 'TWD', '100000')")
    conn.commit()
    found = _by_kind(validate_transaction(conn, _buy("2884", date(2025, 1, 2))))
    assert "2025-06-30" in found["trade_before_ledger_start"].message


def test_an_account_with_no_record_at_all_gets_no_advisory(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    inp = TxnInput(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=D("1"),
                   price=D("100"), trade_date=date(2020, 1, 1))
    assert "trade_before_ledger_start" not in _by_kind(validate_transaction(conn, inp))


def test_an_advisory_can_never_be_built_as_a_hard_issue() -> None:
    """The tier invariant: ``info=True`` forces ``needs_confirm=True``, so every existing
    ``any(not i.needs_confirm)`` hard-test keeps its answer for an advisory."""
    assert advisory_issue("x", "y").needs_confirm is True
    assert Issue(kind="x", message="y", info=True).needs_confirm is True
    assert Issue(kind="x", message="y", needs_confirm=False, info=True).needs_confirm is True
    assert Issue(kind="x", message="y").info is False
