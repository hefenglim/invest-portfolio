"""W2 — the corporate-action ledger: schema, CRUD, and every §5 validation rejection.

The fixture builds a REAL two-account ledger through the real write paths, because the
rules under test are relational: E13 counts how many accounts hold the symbol on the
action date, E12 looks at same-day siblings, and E22 reads a replayed book. A hand-rolled
stub would let all three pass vacuously.
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.holdings import MAX_ACTION_DEPTH
from portfolio_dash.data_ingestion.store import (
    delete_corporate_action,
    get_corporate_action,
    insert_corporate_action,
    insert_transaction,
    list_corporate_actions,
    list_ledger_audit,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    Issue,
    _accounts_holding_on,
    validate_corporate_action,
    validate_corporate_action_change,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
ACTION_DAY = date(2026, 6, 15)
BUY_DAY = date(2026, 1, 10)


def _kinds(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues}


def _hard(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues if not i.needs_confirm}


def _soft(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues if i.needs_confirm}


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.executemany(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        [
            ("schwab", "Schwab", "Schwab", "USD", "TWD", "schwab_us", "drip_us"),
            ("moomoo_my", "Moomoo", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us"),
        ],
    )
    for sym, name in (("AAA", "AAA Corp"), ("BBB", "BBB Corp"), ("CCC", "CCC Corp")):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=name))
    # MYR-quoted, for the E11 currency-mismatch row.
    upsert_instrument(c, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                    sector="Financials", name="MY Bank"))
    # AAA held in BOTH accounts on the action date — this is what makes E13 bite.
    for acct in ("schwab", "moomoo_my"):
        insert_transaction(c, account_id=acct, symbol="AAA", side=Side.BUY,
                           quantity=D("100"), price=D("50"), fees=D("0"), tax=D("0"),
                           trade_date=BUY_DAY)
    c.commit()
    return c


def _inp(**over: object) -> CorporateActionInput:
    base: dict[str, object] = {
        "account_id": "schwab", "date": ACTION_DAY, "kind": "SPLIT",
        "from_symbol": "AAA", "to_symbol": "AAA",
        "ratio_to": D("3"), "ratio_from": D("1"),
    }
    base.update(over)
    return CorporateActionInput(**base)  # type: ignore[arg-type]


def _both_accounts(**over: object) -> list[CorporateActionInput]:
    """A COMPLETE multi-account entry — the shape E13 requires."""
    return [_inp(account_id="schwab", **over), _inp(account_id="moomoo_my", **over)]


# ----------------------------------------------------------------- schema + CRUD


def test_insert_list_get_roundtrip(conn: sqlite3.Connection) -> None:
    action_id = insert_corporate_action(
        conn, account_id="schwab", action_date=ACTION_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("3"), ratio_from=D("1"), note="3-for-1")
    (stored,) = list_corporate_actions(conn)
    assert stored.id == action_id
    assert (stored.kind, stored.ratio_to, stored.ratio_from) == ("SPLIT", D("3"), D("1"))
    assert stored.cost_carry is None
    assert get_corporate_action(conn, action_id) == stored
    assert get_corporate_action(conn, action_id + 999) is None


def test_list_filters_match_either_end_of_the_action(conn: sqlite3.Connection) -> None:
    """A symbol's history includes the actions that CREATED it, not just those it fed."""
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="AAA",
                            to_symbol="BBB", ratio_to=D("1"), ratio_from=D("1"))
    assert len(list_corporate_actions(conn, symbol="AAA")) == 1
    assert len(list_corporate_actions(conn, symbol="BBB")) == 1
    assert list_corporate_actions(conn, symbol="CCC") == []
    assert list_corporate_actions(conn, account_id="moomoo_my") == []


def test_update_and_delete_are_audited(conn: sqlite3.Connection) -> None:
    """E16: editing an action re-computes history, so the before-image must be captured."""
    action_id = insert_corporate_action(
        conn, account_id="schwab", action_date=ACTION_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("3"), ratio_from=D("1"))
    assert get_corporate_action(conn, action_id) is not None

    from portfolio_dash.data_ingestion.store import update_corporate_action
    assert update_corporate_action(
        conn, action_id, account_id="schwab", action_date=ACTION_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("2"), ratio_from=D("1")) is True
    updated = get_corporate_action(conn, action_id)
    assert updated is not None and updated.ratio_to == D("2")

    assert delete_corporate_action(conn, action_id) is True
    assert get_corporate_action(conn, action_id) is None
    assert delete_corporate_action(conn, action_id) is False  # already gone

    audit = list_ledger_audit(conn, table_name="corporate_actions")
    assert [a["action"] for a in audit] == ["delete", "update"]  # newest first
    assert '"ratio_to": "3"' in str(audit[-1]["before_json"])    # the pre-update value


def test_bundle_carries_actions_as_domain_models(conn: sqlite3.Connection) -> None:
    """The promise LedgerBundle was built for: a new ledger is ONE field, no call site moved."""
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    bundle = load_ledger_bundle(conn)
    (action,) = bundle.actions
    assert action.kind is CorporateActionKind.SPLIT
    assert action.ratio_to == D("3")
    # through() must cut the new ledger too — forgetting it is silent.
    assert bundle.through(ACTION_DAY).actions == [action]
    assert bundle.through(ACTION_DAY.replace(day=14)).actions == []


def test_unregistered_symbols_covers_both_ends_of_an_action(conn: sqlite3.Connection) -> None:
    """E21: an action symbol outside `instruments` must join the skip-set, or quote_ccy()
    raises KeyError in build_book — a 500, and a different exception type from every other
    degradation path."""
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="AAA",
                            to_symbol="ZZZ", ratio_to=D("1"), ratio_from=D("1"))
    bundle = load_ledger_bundle(conn)
    assert "ZZZ" in bundle.unregistered_symbols
    assert bundle.without_unregistered().actions == []


# ----------------------------------------------------------------- E6 / E6a (D14)


@pytest.mark.parametrize("to_term", ["0.2857", "0", "-3", "1.5"])
def test_ratio_terms_must_be_positive_integers(conn: sqlite3.Connection, to_term: str) -> None:
    issues = validate_corporate_action(conn, _inp(ratio_to=D(to_term)),
                                       batch=_both_accounts(ratio_to=D(to_term)))
    assert "ratio_not_positive_integer" in _hard(issues)
    assert any("正整數" in i.message for i in issues)


def test_ratio_from_zero_is_rejected_before_it_can_divide_by_zero(
    conn: sqlite3.Connection,
) -> None:
    """Load-bearing, not cosmetic: ratio_from == 0 divides by zero INSIDE the replay."""
    issues = validate_corporate_action(conn, _inp(ratio_from=D("0")),
                                       batch=_both_accounts(ratio_from=D("0")))
    assert "ratio_not_positive_integer" in _hard(issues)


def test_a_clean_multi_account_split_has_no_issues(conn: sqlite3.Connection) -> None:
    batch = _both_accounts()
    assert validate_corporate_action(conn, batch[0], batch=batch) == []
    assert validate_corporate_action(conn, batch[1], batch=batch) == []


# ----------------------------------------------------------------- E20 / E7


def test_split_requires_the_same_symbol(conn: sqlite3.Connection) -> None:
    issues = validate_corporate_action(conn, _inp(to_symbol="BBB"),
                                       batch=_both_accounts(to_symbol="BBB"))
    assert "split_symbol_mismatch" in _hard(issues)


@pytest.mark.parametrize("kind", ["EXCHANGE", "SPINOFF"])
def test_exchange_and_spinoff_reject_a_self_reference(
    conn: sqlite3.Connection, kind: str
) -> None:
    """A self-EXCHANGE silently rescales while looking like a rename; a self-SPINOFF
    double-counts the carried basis, because P and Q are the same object."""
    over: dict[str, object] = {"kind": kind, "to_symbol": "AAA"}
    if kind == "SPINOFF":
        over["cost_carry"] = D("0.3")
    issues = validate_corporate_action(conn, _inp(**over), batch=_both_accounts(**over))
    assert "self_referential_action" in _hard(issues)


def test_no_op_split_warns_but_a_one_to_one_exchange_does_not(
    conn: sqlite3.Connection,
) -> None:
    """E7 is SPLIT-only — ratio 1 on an EXCHANGE is the ordinary rename case."""
    noop = _both_accounts(ratio_to=D("1"))
    assert "split_ratio_one" in _soft(validate_corporate_action(conn, noop[0], batch=noop))
    rename = _both_accounts(kind="EXCHANGE", to_symbol="BBB", ratio_to=D("1"))
    assert "split_ratio_one" not in _kinds(
        validate_corporate_action(conn, rename[0], batch=rename))


# ----------------------------------------------------------------- E8 / E9


def test_spinoff_cost_carry_rules(conn: sqlite3.Connection) -> None:
    missing = _both_accounts(kind="SPINOFF", to_symbol="BBB")
    assert "missing_cost_carry" in _hard(
        validate_corporate_action(conn, missing[0], batch=missing))
    bad = _both_accounts(kind="SPINOFF", to_symbol="BBB", cost_carry=D("1.5"))
    assert "cost_carry_out_of_range" in _hard(
        validate_corporate_action(conn, bad[0], batch=bad))
    stray = _both_accounts(cost_carry=D("0.3"))
    assert "cost_carry_not_applicable" in _hard(
        validate_corporate_action(conn, stray[0], batch=stray))


def test_full_cost_carry_warning_names_the_payback_migration(
    conn: sqlite3.Connection,
) -> None:
    """E9's text must name the DISPLAY consequence. The arithmetic is correct and the
    label lies: a parent that really received dividends reads 0.00% while a child that
    never paid one inherits the parent's progress, possibly showing 已回本."""
    batch = _both_accounts(kind="SPINOFF", to_symbol="BBB", cost_carry=D("1"))
    issues = validate_corporate_action(conn, batch[0], batch=batch)
    assert "cost_carry_all" in _soft(issues)
    (msg,) = [i.message for i in issues if i.kind == "cost_carry_all"]
    assert "回本進度" in msg and "已回本" in msg


# ----------------------------------------------------------------- E10 / D19 / E11


def test_unregistered_symbol_is_rejected_on_either_end(conn: sqlite3.Connection) -> None:
    """D19: keyed on REGISTRATION, a database fact — never on the shape of the string.

    A regex for 'looks like a broker identifier' eventually rejects a legitimate ticker
    and locks the owner out of their own ledger, so the message explains the identifier
    case instead of the rule trying to detect it.
    """
    batch = _both_accounts(kind="EXCHANGE", to_symbol="99887766")
    issues = validate_corporate_action(conn, batch[0], batch=batch)
    assert "unregistered_symbol" in _hard(issues)
    assert any("內部代碼" in i.message for i in issues)


def test_a_legitimate_numeric_ticker_is_accepted(conn: sqlite3.Connection) -> None:
    """Detection power for D19: 1155 is a real Bursa ticker and looks exactly like an
    identifier. Registration is what distinguishes them; if this ever fails, someone
    replaced the database check with a string-shape check."""
    issues = validate_corporate_action(
        conn, _inp(kind="EXCHANGE", to_symbol="1155"),
        batch=_both_accounts(kind="EXCHANGE", to_symbol="1155"))
    assert "unregistered_symbol" not in _kinds(issues)


def test_quote_currency_must_match(conn: sqlite3.Connection) -> None:
    batch = _both_accounts(kind="EXCHANGE", to_symbol="1155")
    assert "quote_ccy_mismatch" in _hard(
        validate_corporate_action(conn, batch[0], batch=batch))


# ----------------------------------------------------------------- E1a


def test_action_before_the_position_existed_is_rejected(conn: sqlite3.Connection) -> None:
    """The corporate-action analogue of the date-aware sell guard. Without it the stranded
    row reaches build_book, which dashboard.py calls with NO try/except — a 500."""
    early = date(2025, 1, 1)
    batch = _both_accounts(date=early)
    issues = validate_corporate_action(conn, batch[0], batch=batch)
    assert "no_position_on_action_date" in _hard(issues)


# ----------------------------------------------------------------- E12 (D15)


def test_same_date_intersecting_actions_are_rejected(conn: sqlite3.Connection) -> None:
    """Measured: split-first gives 600 shares, exchange-first gives 200 — from the same two
    rows, decided by typing order — and the conservation test is green on both."""
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    issues = validate_corporate_action(
        conn, _inp(kind="EXCHANGE", to_symbol="BBB", ratio_to=D("2")),
        batch=_both_accounts(kind="EXCHANGE", to_symbol="BBB", ratio_to=D("2")))
    assert "same_date_action_conflict" in _hard(issues)


def test_same_date_non_intersecting_actions_stay_legal(conn: sqlite3.Connection) -> None:
    """Independent events on one day are ordinary — only an INTERSECTION is ambiguous."""
    insert_transaction(conn, account_id="schwab", symbol="CCC", side=Side.BUY,
                       quantity=D("10"), price=D("5"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="CCC",
                            to_symbol="CCC", ratio_to=D("2"), ratio_from=D("1"))
    batch = _both_accounts()
    assert "same_date_action_conflict" not in _kinds(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_conflicting_ratios_on_one_symbol_and_date_are_rejected(
    conn: sqlite3.Connection,
) -> None:
    insert_corporate_action(conn, account_id="moomoo_my", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    issues = validate_corporate_action(conn, _inp(ratio_to=D("2")),
                                       batch=[_inp(ratio_to=D("2"))])
    assert "conflicting_ratio" in _hard(issues)


def test_an_exact_duplicate_is_rejected_with_its_own_reason(
    conn: sqlite3.Connection,
) -> None:
    """DEVIATION from the spec's E15 "soft warning", recorded 2026-08-09.

    Two things had to change together. First, soft was wrong: acknowledging it applies the
    ratio TWICE (a 3-for-1 becomes a 9-for-1), and unlike a duplicate transaction a
    corporate action is an event that happens once per (account, symbol, date). Second,
    the check has to run BEFORE E12 with its own message — an exact duplicate is by
    construction a same-date intersecting pair, so E12 swallowed every one of them and E15
    could never fire. That is the same "the ⚠ provably never fires" defect E13 was
    rewritten to remove, and this test is what caught it.
    """
    for acct in ("schwab", "moomoo_my"):
        insert_corporate_action(conn, account_id=acct, action_date=ACTION_DAY,
                                kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                                to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    issues = validate_corporate_action(conn, _inp(), batch=_both_accounts())
    assert "duplicate_action" in _hard(issues)
    # The reason given must be the DOUBLING, not E12's ambiguous-ordering story.
    (msg,) = [i.message for i in issues if i.kind == "duplicate_action"]
    assert "兩次" in msg
    assert "same_date_action_conflict" not in _kinds(issues)


# ----------------------------------------------------------------- E13 (D13)


def test_partial_multi_account_application_is_rejected(conn: sqlite3.Connection) -> None:
    """The ⚠ the earlier spec promised provably never fires: an account with no action row
    has corporate_delta 0, so its drawer footer prints ✓ 對帳一致 while its market value is
    wrong by the whole ratio."""
    issues = validate_corporate_action(conn, _inp(), batch=[_inp()])  # schwab only
    assert "incomplete_account_coverage" in _hard(issues)
    assert any("moomoo_my" in i.message for i in issues)


def test_an_account_that_bought_after_the_action_is_not_counted(
    conn: sqlite3.Connection,
) -> None:
    """It never held the pre-action shares, so it needs no row."""
    insert_transaction(conn, account_id="schwab", symbol="CCC", side=Side.BUY,
                       quantity=D("10"), price=D("5"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    insert_transaction(conn, account_id="moomoo_my", symbol="CCC", side=Side.BUY,
                       quantity=D("10"), price=D("5"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 12, 1))  # AFTER the action
    only_schwab = _inp(from_symbol="CCC", to_symbol="CCC")
    assert "incomplete_account_coverage" not in _kinds(
        validate_corporate_action(conn, only_schwab, batch=[only_schwab]))


# ----------------------------------------------------------------- E22 / E18 / E3 / E5


def _make_oversold(conn: sqlite3.Connection, account_id: str, symbol: str) -> None:
    """Sell more than held so the STICKY guard discards the basis (an acked 賣超)."""
    insert_transaction(conn, account_id=account_id, symbol=symbol, side=Side.SELL,
                       quantity=D("500"), price=D("60"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1))


def test_action_on_an_oversold_source_is_rejected(conn: sqlite3.Connection) -> None:
    _make_oversold(conn, "schwab", "AAA")
    issues = validate_corporate_action(conn, _inp(), batch=_both_accounts())
    assert "oversold_source" in _hard(issues)


def test_exchange_into_an_oversold_destination_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    """E22 (D16). Q.original_total += P.original_total deposits REAL money onto a position
    whose basis the sticky guard deliberately discarded, producing a confident,
    ordinary-looking average over shares that have no basis at all."""
    insert_transaction(conn, account_id="schwab", symbol="BBB", side=Side.BUY,
                       quantity=D("60"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    _make_oversold(conn, "schwab", "BBB")
    batch = _both_accounts(kind="EXCHANGE", to_symbol="BBB")
    assert "oversold_destination" in _hard(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_exchange_from_an_open_short_is_rejected(conn: sqlite3.Connection) -> None:
    insert_transaction(conn, account_id="schwab", symbol="CCC", side=Side.SELL,
                       quantity=D("40"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1), short_sale=True)
    only = _inp(kind="EXCHANGE", from_symbol="CCC", to_symbol="BBB")
    assert "short_source" in _hard(validate_corporate_action(conn, only, batch=[only]))


def test_exchange_into_an_open_short_is_rejected(conn: sqlite3.Connection) -> None:
    """E18: Q.shares += carried on a short destination breaks the long/short exclusivity
    the whole replay is built on."""
    insert_transaction(conn, account_id="schwab", symbol="BBB", side=Side.SELL,
                       quantity=D("40"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1), short_sale=True)
    batch = _both_accounts(kind="EXCHANGE", to_symbol="BBB")
    assert "short_destination" in _hard(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_split_on_an_open_short_is_allowed(conn: sqlite3.Connection) -> None:
    """E4: you owe more shares, you still received the same money — the average sale price
    scales correctly. Only EXCHANGE/SPINOFF have no honest booking."""
    insert_transaction(conn, account_id="schwab", symbol="CCC", side=Side.SELL,
                       quantity=D("40"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1), short_sale=True)
    only = _inp(from_symbol="CCC", to_symbol="CCC")
    assert "short_source" not in _kinds(
        validate_corporate_action(conn, only, batch=[only]))


def test_unknown_kind_is_rejected_in_zh_without_crashing(conn: sqlite3.Connection) -> None:
    issues = validate_corporate_action(conn, _inp(kind="MERGER"), batch=[])
    assert _hard(issues) == {"unknown_action_kind"}
    assert "MERGER" in issues[0].message


def test_every_message_is_traditional_chinese(conn: sqlite3.Connection) -> None:
    """No English leaks: v0.1.27 shipped 21 English strings straight to the screen."""
    collected: list[str] = []
    for over in ({"ratio_to": D("0.2857")}, {"to_symbol": "BBB"},
                 {"kind": "SPINOFF", "to_symbol": "BBB"},
                 {"kind": "EXCHANGE", "to_symbol": "ZZZ"},
                 {"kind": "EXCHANGE", "to_symbol": "1155"},
                 {"date": date(2025, 1, 1)}):
        collected += [i.message for i in
                      validate_corporate_action(conn, _inp(**over), batch=[_inp(**over)])]
    assert collected
    for msg in collected:
        assert any("一" <= ch <= "鿿" for ch in msg), msg


# ================================================================== W4 repairs
# Four defects the 2026-08-10 spec-conflict audit found in this file's subject, each
# reproduced against HEAD before the fix. Their common shape: a rule that reads a share
# count the replay does not agree with, or reads a collection the entry shape never fills.


def test_the_second_action_of_a_chain_is_committable(conn: sqlite3.Connection) -> None:
    """F-08 — the LIVE blocker: the feature could not accept its own motivating data.

    E1a called the action-unaware ``shares_through``, which reads ``opening_inventory`` /
    ``transactions`` / ``dividends`` for the destination symbol, finds all three empty and
    returns 0. So every chain was hard-rejected at its SECOND action as 「沒有持倉」 while
    ``build_book`` handled the same chain correctly. §10.5 defines "done" as accepting a
    ledger full of exactly these chains.
    """
    for acct in ("schwab", "moomoo_my"):
        insert_corporate_action(conn, account_id=acct, action_date=date(2026, 3, 1),
                                kind=CorporateActionKind.EXCHANGE, from_symbol="AAA",
                                to_symbol="BBB", ratio_to=D("1"), ratio_from=D("1"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert {(h.symbol, h.shares) for h in book.holdings} == {("BBB", D("100"))}

    second = [_inp(account_id=a, from_symbol="BBB", to_symbol="BBB",
                   date=date(2026, 9, 1), ratio_to=D("2"))
              for a in ("schwab", "moomoo_my")]
    assert _kinds(validate_corporate_action(conn, second[0], batch=second)) == set()


def test_e1a_still_refuses_an_action_on_a_symbol_never_held(
    conn: sqlite3.Connection,
) -> None:
    """…and the guard must still bite, or the fix above is just a deletion."""
    stranded = _inp(from_symbol="CCC", to_symbol="CCC")
    assert "no_position_on_action_date" in _hard(
        validate_corporate_action(conn, stranded, batch=[stranded]))


def test_e13_sees_a_position_acquired_only_by_a_corporate_action(
    conn: sqlite3.Connection,
) -> None:
    """F-07 — the SILENT half: E13's candidate set could not name the account at all.

    The union was ``transactions`` ∪ ``opening_inventory``. A position created purely by an
    EXCHANGE is in neither, so ``shares_through`` was never even called for it and D13's
    all-or-nothing rule went quiet exactly where the spec says the state "must not be
    reachable at all" — a partial application whose drawer footer prints ✓ 對帳一致.
    """
    for acct in ("schwab", "moomoo_my"):
        insert_corporate_action(conn, account_id=acct, action_date=date(2026, 3, 1),
                                kind=CorporateActionKind.EXCHANGE, from_symbol="AAA",
                                to_symbol="BBB", ratio_to=D("1"), ratio_from=D("1"))
    assert _accounts_holding_on(conn, "BBB", date(2026, 9, 1)) == {"schwab", "moomoo_my"}

    schwab_only = _inp(from_symbol="BBB", to_symbol="BBB", date=date(2026, 9, 1))
    issues = validate_corporate_action(conn, schwab_only, batch=[schwab_only])
    assert "incomplete_account_coverage" in _hard(issues)
    assert any("moomoo_my" in i.message for i in issues)


def test_conflicting_ratio_guard_fires_on_a_batch_sibling(
    conn: sqlite3.Connection,
) -> None:
    """F-06a — the guard read ``stored`` only, and D28 mandates ONE N-row batch.

    In that entry shape ``stored`` is empty for every row, so the rule **provably never
    fired on the primary door**. Two rows of one batch disagreeing about the ratio is the
    single most likely way the conflict actually arrives.
    """
    batch = [_inp(account_id="schwab", ratio_to=D("3")),
             _inp(account_id="moomoo_my", ratio_to=D("2"))]
    assert "conflicting_ratio" in _hard(validate_corporate_action(conn, batch[0], batch=batch))
    assert "conflicting_ratio" in _hard(validate_corporate_action(conn, batch[1], batch=batch))


def test_the_same_ratio_written_two_ways_is_accepted(conn: sqlite3.Connection) -> None:
    """F-06b — 「3 比 1」 and 「30 比 10」 are ONE ratio, so a batch mixing the spellings is
    complete and consistent, not conflicting. Term-wise comparison rejected it."""
    batch = [_inp(account_id="schwab", ratio_to=D("3"), ratio_from=D("1")),
             _inp(account_id="moomoo_my", ratio_to=D("30"), ratio_from=D("10"))]
    assert _kinds(validate_corporate_action(conn, batch[0], batch=batch)) == set()
    assert _kinds(validate_corporate_action(conn, batch[1], batch=batch)) == set()


def test_the_same_ratio_written_two_ways_is_a_duplicate_when_stored(
    conn: sqlite3.Connection,
) -> None:
    """…and once one spelling is stored, the other is the same EVENT — E15, not E12.

    Reaching this state is how F-10 became live: two surviving entries for one split made
    ``split_factor`` return 9 instead of 3.
    """
    insert_corporate_action(conn, account_id="schwab", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    again = _inp(ratio_to=D("30"), ratio_from=D("10"))
    assert "duplicate_action" in _hard(validate_corporate_action(conn, again, batch=[again]))


def test_a_genuinely_different_ratio_is_still_rejected(conn: sqlite3.Connection) -> None:
    """The guard must still bite, or the quotient comparison is just a disabling."""
    insert_corporate_action(conn, account_id="moomoo_my", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    two_for_one = _inp(ratio_to=D("2"))
    assert "conflicting_ratio" in _hard(
        validate_corporate_action(conn, two_for_one, batch=[two_for_one]))


# ------------------------------------------------------- F-32: E13 on delete / update


def _stored_split_set(conn: sqlite3.Connection) -> list[int]:
    """The D28 shape: one event, one row per holding account."""
    return [
        insert_corporate_action(conn, account_id=acct, action_date=ACTION_DAY,
                                kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                                to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
        for acct in ("schwab", "moomoo_my")
    ]


def test_deleting_one_row_of_a_set_is_refused(conn: sqlite3.Connection) -> None:
    """F-32 — E13 was enforced at INSERT only, and ``store.py`` has zero references to it.

    ``split_factor``'s dedup key is ``(symbol, date, ratio)`` with **no account**, so
    deleting one row leaves the GLOBAL price correction standing while that account's shares
    go uncorrected — and the drawer footer prints ✓ 對帳一致 over the mismatch.
    """
    first, _second = _stored_split_set(conn)
    issues = validate_corporate_action_change(conn, first)
    assert "partial_action_set_change" in _hard(issues)
    assert any("moomoo_my" in i.message for i in issues)


def test_deleting_a_lone_row_is_allowed(conn: sqlite3.Connection) -> None:
    action_id = insert_corporate_action(
        conn, account_id="schwab", action_date=ACTION_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("3"), ratio_from=D("1"))
    assert validate_corporate_action_change(conn, action_id) == []


def test_editing_one_row_off_the_event_is_refused(conn: sqlite3.Connection) -> None:
    """An UPDATE that moves a row to another date is a DELETE from the set (E16 / N2)."""
    first, _second = _stored_split_set(conn)
    moved = _inp(date=date(2026, 7, 1))
    assert "partial_action_set_change" in _hard(
        validate_corporate_action_change(conn, first, replacement=moved))


def test_editing_one_rows_ratio_out_of_step_is_refused(conn: sqlite3.Connection) -> None:
    """F-06's conflict recreated from the inside — and the quotient rule applies here too."""
    first, _second = _stored_split_set(conn)
    assert "conflicting_ratio" in _hard(
        validate_corporate_action_change(conn, first, replacement=_inp(ratio_to=D("2"))))
    assert validate_corporate_action_change(
        conn, first, replacement=_inp(ratio_to=D("30"), ratio_from=D("10"))) == []


def test_changing_an_unknown_action_is_reported_not_crashed(
    conn: sqlite3.Connection,
) -> None:
    assert _hard(validate_corporate_action_change(conn, 9999)) == {"unknown_action"}


# --------------------------------------------------------------- D31: the depth cap


def test_a_chain_past_the_depth_cap_raises_needs_confirm(conn: sqlite3.Connection) -> None:
    """D31: not ``Decimal | None`` — the 賣超 tier, blocking until acknowledged.

    The read paths kept the bare ``Decimal`` and fell back to the action-unaware count; the
    validation path reads the same per-request set and refuses to guess on top of it.
    """
    depth = MAX_ACTION_DEPTH + 4
    for i in range(depth + 1):
        upsert_instrument(conn, Instrument(symbol=f"C{i}", market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name=f"C{i}"))
    insert_transaction(conn, account_id="schwab", symbol="C0", side=Side.BUY,
                       quantity=D("100"), price=D("5"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    for i in range(depth):
        insert_corporate_action(
            conn, account_id="schwab", action_date=date(2026, 2, 1) + timedelta(days=i),
            kind=CorporateActionKind.EXCHANGE, from_symbol=f"C{i}", to_symbol=f"C{i + 1}",
            ratio_to=D("1"), ratio_from=D("1"))
    tail = f"C{depth}"
    deep = _inp(from_symbol=tail, to_symbol=tail, date=date(2027, 1, 1))
    issues = validate_corporate_action(conn, deep, batch=[deep])
    assert "action_chain_too_deep" in _soft(issues)


def test_a_short_chain_raises_no_depth_issue(conn: sqlite3.Connection) -> None:
    """The cap is insurance, not a limit — it must not fire on a legitimate ledger."""
    batch = _both_accounts()
    assert "action_chain_too_deep" not in _kinds(
        validate_corporate_action(conn, batch[0], batch=batch))
