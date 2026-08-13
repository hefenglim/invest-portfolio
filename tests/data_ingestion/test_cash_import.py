"""The 6th CSV kind: cash movements — parsing, the shared guard, and the two soft advisories.

The kind exists only because ``validate_cash_movement`` was extracted first, and its withdraw
guard only works because ``portfolio/cash.py``'s arithmetic is INJECTED (``data_ingestion``
may not import ``portfolio``). Both of those are load-bearing and both are tested here:

* the guard fires on the CSV path with the same verdict the manual door gives, and
* it demonstrably gets that verdict FROM the injected arithmetic — a stub probe that reports
  a rich pool lets the same overdraft straight through, which is what "unbound" would look
  like if the probe had a default. It does not have one; see ``test_the_probe_is_required``.
"""

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
    write_cash_movement_row,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    list_cash_movements,
)
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPool,
    CashPoolFn,
)
from portfolio_dash.portfolio.cash import cash_balances, pool_lines, running_min
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"


def _pool_fn(conn: sqlite3.Connection) -> CashPoolFn:
    """The REAL arithmetic, bound the way ``api`` binds it — but built here, from
    ``portfolio/cash.py`` directly, so this unit test does not depend on the router."""
    from portfolio_dash.data_ingestion.store import (
        StoredCashMovement,
        list_dividends,
        list_fx_conversions,
        list_instruments,
        list_transactions,
    )

    movements = list_cash_movements(conn)
    fx = list_fx_conversions(conn)
    txns = list_transactions(conn)
    divs = list_dividends(conn)
    insts = {i.symbol: i for i in list_instruments(conn)}

    def probe(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        rows = [m for m in movements if m.id != exclude_id]
        rows.extend(
            StoredCashMovement(id=0, account_id=m.account_id, date=m.date, kind=m.kind,
                               ccy=m.ccy, amount=m.amount, note=m.note)
            for m in include
        )
        return CashPool(
            balance=cash_balances(rows, fx, txns, divs, insts).get(
                (account_id, ccy), Decimal("0")),
            low=running_min(pool_lines(account_id, ccy, rows, fx, txns, divs, insts)),
        )

    return probe


def _rich_pool_fn(_conn: sqlite3.Connection) -> CashPoolFn:
    """A stub that reports an inexhaustible pool — what "the arithmetic was never wired"
    would look like, if the probe had a default instead of being required."""

    def probe(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        return CashPool(balance=Decimal("1e12"), low=Decimal("0"))

    return probe


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    conn.commit()
    return conn


def _built(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    return build_cash_movement_preview(conn, csv_text, pool=_pool_fn(conn))


def _kinds(preview: ImportPreview) -> list[str]:
    return [i.kind for row in preview.rows for i in row.issues]


def _row_kinds(row: PreviewRow) -> set[str]:
    return {i.kind for i in row.issues}


# --- parsing -------------------------------------------------------------------------


def test_clean_rows_parse_and_write(seeded: sqlite3.Connection) -> None:
    preview = _built(seeded, _HEADER + (
        "tw_broker,2026-07-01,DEPOSIT,TWD,600000,,初始入金\n"
        "schwab,2026-07-02,OPENING,USD,100000,3135870,期初外幣\n"))
    assert _kinds(preview) == []
    summary = commit_preview(seeded, preview, accept={0, 1}, writer=write_cash_movement_row)
    assert len(summary.written) == 2
    stored = list_cash_movements(seeded)
    assert [(m.account_id, m.kind, m.ccy, m.amount, m.acq_home_amount) for m in stored] == [
        ("tw_broker", "DEPOSIT", Currency.TWD, Decimal("600000"), None),
        ("schwab", "OPENING", Currency.USD, Decimal("100000"), Decimal("3135870")),
    ]


def test_zh_kind_labels_are_accepted(seeded: sqlite3.Connection) -> None:
    """The owner reads 入金 / 出金 on a statement; the file should not need translating."""
    preview = _built(seeded, _HEADER + (
        "tw_broker,2026-07-01,入金,TWD,1000,,\n"
        "tw_broker,2026-07-02,出金,TWD,400,,\n"
        "tw_broker,2026-07-03,期初資金,TWD,1,,\n"
        "tw_broker,2026-07-04,折讓款,TWD,50,,\n"))
    assert _kinds(preview) == []
    assert [r.payload["kind"] for r in preview.rows] == [
        "DEPOSIT", "WITHDRAW", "OPENING", "REBATE"]


def test_the_broker_statement_zh_labels_are_accepted(seeded: sqlite3.Connection) -> None:
    """The three kinds added for the broker importer, in the owner's own words.

    Their aliases went in with the kinds and nothing covered them, so the table above could
    have been half-populated and every English-spelling test would still have passed. Both
    spellings of each are here because both are in ``_KIND_ALIASES``, and an alias nobody
    tests is an alias that quietly stops resolving.
    """
    preview = _built(seeded, _HEADER + (
        "schwab,2026-07-01,利息,USD,12,,\n"
        "schwab,2026-07-02,利息收入,USD,3,,\n"
        "schwab,2026-07-03,融資利息,USD,5,,\n"
        "schwab,2026-07-04,利息支出,USD,2,,\n"
        "schwab,2026-07-05,券商費用,USD,8,,\n"
        "schwab,2026-07-06,帳戶費用,USD,1,,\n"))
    assert _kinds(preview) == []
    assert [r.payload["kind"] for r in preview.rows] == [
        "INTEREST", "INTEREST", "INTEREST_EXPENSE", "INTEREST_EXPENSE",
        "BROKER_FEE", "BROKER_FEE",
    ]


def test_unknown_kind_names_what_was_typed(seeded: sqlite3.Connection) -> None:
    """An unrecognised label passes through the alias map UNCHANGED so the rejection can
    quote it — a map that silently canonicalized would report a kind nobody typed."""
    preview = _built(seeded, _HEADER + "tw_broker,2026-07-01,轉帳,TWD,100,,\n")
    issue = preview.rows[0].issues[0]
    assert issue.kind == "unknown_movement_kind" and "轉帳" in issue.message
    assert preview.rows[0].has_hard_issue


@pytest.mark.parametrize(("row", "fragment"), [
    ("tw_broker,,DEPOSIT,TWD,100,,", "Invalid isoformat"),
    ("tw_broker,2026-07-01,DEPOSIT,,100,,", "幣別（ccy）不可空白"),
    ("tw_broker,2026-07-01,DEPOSIT,GBP,100,,", "僅支援"),
    ("tw_broker,2026-07-01,DEPOSIT,TWD,,,", "金額（amount）不可空白"),
    ("tw_broker,2026-07-01,DEPOSIT,TWD,abc,,", "不是數字"),
    ("tw_broker,2026-07-01,DEPOSIT,USD,100,xyz,", "不是數字"),
])
def test_unparseable_cells_are_hard_parse_errors(
    seeded: sqlite3.Connection, row: str, fragment: str
) -> None:
    preview = _built(seeded, _HEADER + row + "\n")
    assert _row_kinds(preview.rows[0]) == {"parse_error"}
    assert fragment in preview.rows[0].issues[0].message
    assert preview.rows[0].payload == {}  # nothing to write


def test_a_missing_required_column_is_a_parse_error(seeded: sqlite3.Connection) -> None:
    preview = _built(seeded, "account,kind,ccy,amount\ntw_broker,DEPOSIT,TWD,100\n")
    assert _row_kinds(preview.rows[0]) == {"parse_error"}
    assert "date" in preview.rows[0].issues[0].message


def test_legacy_moomoo_account_id_is_aliased(seeded: sqlite3.Connection) -> None:
    """Batch B: the shared ``alias_import_account`` helper, not a sixth copy of the map."""
    preview = _built(seeded, _HEADER + "moomoo_my_us,2026-07-01,DEPOSIT,USD,100,,\n")
    assert _row_kinds(preview.rows[0]) == {"account_alias"}
    assert not preview.rows[0].has_hard_issue
    assert preview.rows[0].payload["account_id"] == "moomoo_my"


# --- the shared guard, reached through the CSV door --------------------------------


def test_withdraw_over_balance_is_hard_and_writes_nothing(
    seeded: sqlite3.Connection,
) -> None:
    """THE point of the whole exercise: the bulk door's guard is the form's guard."""
    insert_cash_movement(seeded, account_id="moomoo_my", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("1000"))
    preview = _built(seeded, _HEADER + "moomoo_my,2026-02-01,WITHDRAW,MYR,1500,,\n")
    issue = preview.rows[0].issues[0]
    assert issue.kind == "withdraw_insufficient_balance"
    assert "1000" in issue.message  # the available balance is stated
    assert preview.rows[0].has_hard_issue
    summary = commit_preview(seeded, preview, accept={0}, writer=write_cash_movement_row)
    assert summary.written == [] and summary.skipped == [0]
    assert len(list_cash_movements(seeded)) == 1  # only the seeded deposit


def test_the_verdict_comes_from_the_injected_arithmetic(
    seeded: sqlite3.Connection,
) -> None:
    """The same overdraft, with a stub probe that reports an inexhaustible pool, is CLEAN.

    This is the guard being watched to go red and green for one reason only. If the probe
    had a default — or if a registration bound nothing — this is the behaviour production
    would have: a bulk import silently overdrafting a pool the form refuses to overdraft.
    """
    insert_cash_movement(seeded, account_id="moomoo_my", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("1000"))
    text = _HEADER + "moomoo_my,2026-02-01,WITHDRAW,MYR,1500,,\n"
    unguarded = build_cash_movement_preview(seeded, text, pool=_rich_pool_fn(seeded))
    assert unguarded.rows[0].issues == []
    assert not unguarded.rows[0].has_hard_issue
    # ...while the real arithmetic refuses the identical row.
    assert _built(seeded, text).rows[0].has_hard_issue


def test_the_probe_is_required(seeded: sqlite3.Connection) -> None:
    """No default, on purpose: forgetting to bind it must be loud, not silently permissive.

    mypy catches it statically; this catches a future edit that "helpfully" adds a default.
    """
    with pytest.raises(TypeError):
        build_cash_movement_preview(seeded, _HEADER)  # type: ignore[call-arg]


def test_backdated_withdraw_before_its_funding_is_blocked(
    seeded: sqlite3.Connection,
) -> None:
    """audit C3 through the CSV door: the END balance covers it, the TIMELINE does not."""
    insert_cash_movement(seeded, account_id="moomoo_my", move_date=date(2026, 5, 1),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("1000"))
    preview = _built(seeded, _HEADER + "moomoo_my,2026-04-01,WITHDRAW,USD,500,,\n")
    issue = preview.rows[0].issues[0]
    assert issue.kind == "withdraw_insufficient_balance"
    assert "出金日早於資金到位" in issue.message


def test_a_files_own_deposit_funds_its_own_withdrawal(seeded: sqlite3.Connection) -> None:
    """Without the batch, the first import into a fresh ledger rejects every withdrawal it
    contains — the feature could not accept the data it exists to accept."""
    preview = _built(seeded, _HEADER + (
        "moomoo_my,2026-01-01,DEPOSIT,MYR,1000,,\n"
        "moomoo_my,2026-02-01,WITHDRAW,MYR,600,,\n"))
    assert _kinds(preview) == []


def test_two_withdrawals_that_only_jointly_overdraft_are_both_caught(
    seeded: sqlite3.Connection,
) -> None:
    """Each row sees its siblings, so neither can hide behind the other's headroom."""
    preview = _built(seeded, _HEADER + (
        "moomoo_my,2026-01-01,DEPOSIT,MYR,1000,,\n"
        "moomoo_my,2026-02-01,WITHDRAW,MYR,700,,\n"
        "moomoo_my,2026-03-01,WITHDRAW,MYR,700,,\n"))
    assert not preview.rows[0].has_hard_issue
    assert _row_kinds(preview.rows[1]) == {"withdraw_insufficient_balance"}
    assert _row_kinds(preview.rows[2]) == {"withdraw_insufficient_balance"}


def test_credits_need_no_balance_guard(seeded: sqlite3.Connection) -> None:
    """DEPOSIT / OPENING / REBATE are credits — they may land on an empty or negative pool."""
    preview = _built(seeded, _HEADER + (
        "moomoo_my,2026-01-01,DEPOSIT,MYR,1,,\n"
        "moomoo_my,2026-01-02,OPENING,MYR,1,,\n"
        "moomoo_my,2026-01-03,REBATE,MYR,1,,\n"))
    assert _kinds(preview) == []


@pytest.mark.parametrize(("row", "kind"), [
    ("ghost,2026-07-01,DEPOSIT,TWD,100,,", "unknown_account"),
    ("tw_broker,2026-07-01,DEPOSIT,USD,100,,", "ccy_not_allowed"),
    ("tw_broker,2026-07-01,DEPOSIT,TWD,0,,", "non_positive_amount"),
    ("tw_broker,2026-07-01,DEPOSIT,TWD,-1,,", "non_positive_amount"),
])
def test_structural_rejections_reach_the_csv_door(
    seeded: sqlite3.Connection, row: str, kind: str
) -> None:
    preview = _built(seeded, _HEADER + row + "\n")
    assert _row_kinds(preview.rows[0]) == {kind}
    assert preview.rows[0].has_hard_issue


# --- acquisition cost (spec 2026-07-30, F1) ----------------------------------------


def test_acquisition_cost_is_stored_quantized_to_the_funding_currency(
    seeded: sqlite3.Connection,
) -> None:
    """The AMOUNT is the authority; it is quantized by the same helper the form uses, so a
    row imported from a file and one typed into the form persist the identical figure."""
    preview = _built(seeded, _HEADER + "moomoo_my,2026-07-01,OPENING,USD,1000,4400.005,\n")
    assert _kinds(preview) == []
    commit_preview(seeded, preview, accept={0}, writer=write_cash_movement_row)
    assert list_cash_movements(seeded)[0].acq_home_amount == Decimal("4400.01")  # MYR 2 dp


def test_acquisition_cost_may_be_blank(seeded: sqlite3.Connection) -> None:
    """"I do not know the rate" is an honest answer; a guessed rate is not. The amount then
    funds the pool but stays out of the weighted average (disclosed via covered_ratio)."""
    preview = _built(seeded, _HEADER + "moomoo_my,2026-07-01,OPENING,USD,1000,,\n")
    assert _kinds(preview) == []
    commit_preview(seeded, preview, accept={0}, writer=write_cash_movement_row)
    assert list_cash_movements(seeded)[0].acq_home_amount is None


@pytest.mark.parametrize(("row", "kind"), [
    # home-currency movement — an acquisition cost is meaningless there
    ("moomoo_my,2026-07-01,DEPOSIT,MYR,1000,1000,", "acq_cost_home_ccy"),
    # a withdrawal is a disposal (N1) — it carries no acquisition cost
    ("moomoo_my,2026-07-01,WITHDRAW,USD,10,44,", "acq_cost_on_withdraw"),
    # Interest is a CREDIT, so the old ``kind == "WITHDRAW"`` test let a cost through on it
    # — and forex/pools.py then IGNORED that cost, because income arising inside the pool
    # inherits the pool average. The guard is keyed on the ACQUISITION axis instead.
    ("moomoo_my,2026-07-01,INTEREST,USD,10,44,", "acq_cost_not_an_acquisition"),
    ("moomoo_my,2026-07-01,BROKER_FEE,USD,10,44,", "acq_cost_not_an_acquisition"),
    ("moomoo_my,2026-07-01,INTEREST_EXPENSE,USD,10,44,", "acq_cost_not_an_acquisition"),
    ("moomoo_my,2026-07-01,OPENING,USD,1000,0,", "acq_cost_not_positive"),
    ("moomoo_my,2026-07-01,OPENING,USD,1000,-1,", "acq_cost_not_positive"),
])
def test_acquisition_cost_rejections_are_the_shared_ones(
    seeded: sqlite3.Connection, row: str, kind: str
) -> None:
    preview = _built(seeded, _HEADER + row + "\n")
    assert kind in _row_kinds(preview.rows[0])
    assert preview.rows[0].has_hard_issue


def test_there_is_no_rate_column(seeded: sqlite3.Connection) -> None:
    """F1 / data-and-pricing.md: store the AMOUNT, never the rate.

    The form accepts a typed rate because a human reads one off a statement, converts it at
    the seam and stores the amount. A rate COLUMN would put a rounded average into the file
    itself — and a file is re-read, re-edited and re-imported, so the average would become
    the de-facto authority. An ``acq_rate`` column is therefore simply ignored (a stray
    column is not an error in any kind), and nothing derives a cost from it.
    """
    assert "acq_rate" not in CASH_MOVEMENT_COLUMNS
    preview = build_cash_movement_preview(
        seeded,
        "account,date,kind,ccy,amount,acq_rate\n"
        "moomoo_my,2026-07-01,OPENING,USD,1000,4.4\n",
        pool=_pool_fn(seeded),
    )
    assert _kinds(preview) == []
    assert "acq_home_amount" not in preview.rows[0].payload


# --- the two SOFT, bulk-only advisories --------------------------------------------


def test_reuploading_the_same_row_warns_but_does_not_block(
    seeded: sqlite3.Connection,
) -> None:
    """A cash movement has no natural key, so a re-uploaded file books every amount twice
    and nothing on any screen says so. Soft — re-depositing the same amount is legitimate."""
    insert_cash_movement(seeded, account_id="tw_broker", move_date=date(2026, 7, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("600000"))
    preview = _built(seeded, _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,600000,,\n")
    assert _row_kinds(preview.rows[0]) == {"duplicate_movement"}
    assert not preview.rows[0].has_hard_issue  # acknowledgeable, not a block


def test_duplicate_matches_on_value_not_on_spelling(seeded: sqlite3.Connection) -> None:
    """``600000.00`` and ``600000`` are the same money; Decimal equality says so."""
    insert_cash_movement(seeded, account_id="tw_broker", move_date=date(2026, 7, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("600000"))
    preview = _built(seeded, _HEADER + "tw_broker,2026-07-01,入金,TWD,600000.00,,\n")
    assert "duplicate_movement" in _row_kinds(preview.rows[0])


def test_a_foreign_withdrawal_raises_the_N1_advisory(seeded: sqlite3.Connection) -> None:
    """N1: a foreign WITHDRAW recognises NO realized FX. If the money was really converted
    back, the correct row is an fx_conversion — soft, because a genuine foreign cash
    withdrawal is legitimate and the importer cannot tell the two apart."""
    insert_cash_movement(seeded, account_id="moomoo_my", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("1000"))
    preview = _built(seeded, _HEADER + "moomoo_my,2026-02-01,WITHDRAW,USD,500,,\n")
    assert _row_kinds(preview.rows[0]) == {"foreign_withdraw_no_fx"}
    assert not preview.rows[0].has_hard_issue
    assert "換匯" in preview.rows[0].issues[0].message  # names the correct entry


def test_a_home_currency_withdrawal_raises_no_advisory(
    seeded: sqlite3.Connection,
) -> None:
    """The advisory is about the FUNDING currency, not about withdrawals in general."""
    insert_cash_movement(seeded, account_id="moomoo_my", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("1000"))
    preview = _built(seeded, _HEADER + "moomoo_my,2026-02-01,WITHDRAW,MYR,500,,\n")
    assert _kinds(preview) == []


def test_the_batch_is_atomic_on_an_unexpected_error(seeded: sqlite3.Connection) -> None:
    """#1: every accepted row is written with ``commit=False`` and committed once, so a
    mid-batch failure leaves NO partial ledger write. The store gained its ``commit`` flag
    for this kind; without it the first row would already be durable."""
    preview = _built(seeded, _HEADER + (
        "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n"
        "tw_broker,2026-07-02,DEPOSIT,TWD,2000,,\n"))

    def exploding(
        conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
    ) -> int:
        if row.index == 1:
            raise RuntimeError("boom")
        return write_cash_movement_row(conn, row, commit=commit)

    with pytest.raises(RuntimeError):
        commit_preview(seeded, preview, accept={0, 1}, writer=exploding)
    assert list_cash_movements(seeded) == []
