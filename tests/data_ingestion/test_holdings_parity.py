"""§7.2 — the parity test: ``holdings.py``'s share walk vs ``build_book``'s replay.

The spec calls this "the single most valuable test here", and the version it originally
specified could not fail on the case the package exists for. Three things about the shape
below are therefore normative (audit F-16, 2026-08-10), and each replaces a natural reading
that quietly removes the test's power:

1. **Iterate the union of ``(account, symbol)`` across all four ledgers, plus BOTH symbols
   of every action** — not ``book.holdings``. ``cost_basis.py`` drops every zero-share
   position before emitting, so an EXCHANGE-emptied source is *structurally absent* from the
   holdings list. That source is exactly what F-07 and F-08 are about, and §7.3's drawer
   footer inherits **all** of its detection power from this test.
2. **Assert against the replay's INTERNAL position map** (the same source §7.1a rule 1
   requires), so a correctly-zero position is distinguishable from one that was flagged and
   skipped. Comparing against "absent from ``book.holdings`` ⇒ 0" cannot tell those apart.
3. **Equality for unflagged positions; divergence merely permitted on flagged ones.** The
   walker is deliberately NOT a second copy of ``_apply_action``'s refusal matrix (§6.0,
   ONE owner per concept), so where the replay refuses an action the two legitimately part
   company — and every such case is named in ``Book.unapplied_actions``.

**What the permitted gap actually is — state it, or a green §7.2 reads as "the two paths
agree".** It does not say that. It says *the two paths agree wherever the replay booked the
action*. The refusals split into three groups, and the split is what the property means:

* **E1** (source never existed) and **E2** (source already empty) — **agree, with no rule
  needed.** Every delta is computed from a zero source, so the arithmetic already yields 0.
* **E3** while the count is negative, and **E5** (an open declared short being EXCHANGEd or
  SPUN OFF) — **agree, skipped under D33.** Long and short are mutually exclusive, so a
  negative signed count is *exactly* one of those two states: one comparison, no import.
* **E3** STICKY-but-now-positive, **E18** (destination short) and **E22** (destination
  ``ever_oversold``) — **DIVERGE.** Their preconditions are replay state a share-only path
  cannot see without importing the replay's model, which D33 declined by name because it
  would collapse the two implementations and end the cross-check §6.3's footer exists to be.

Every divergent case leaves a row in ``Book.unapplied_actions``, which is what the ``flagged``
skip below reads — so the gap is bounded by "the replay said so", never by silence.
:func:`test_the_permitted_divergence_is_bounded_and_flagged` pins it, because a divergence
this concrete should be met as documented behaviour rather than as a surprise.

The fixture is one ledger carrying all three action kinds, a chain, a merge into an existing
position, a same-day opening, an open declared short, and a symbol with no action at all.
Every one of those is a case that has been observed to break something.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import holdings as holdings_mod
from portfolio_dash.data_ingestion.holdings import current_shares, load_action_index
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio import cost_basis
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal

SPLIT_DAY = date(2026, 3, 1)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A two-account ledger exercising every walker branch at once.

    | position           | what it proves |
    | ------------------ | -------------- |
    | schwab AAA         | SPLIT, then a later sell against the post-split count |
    | schwab BBB         | NO action at all — the short-circuit, inside the parity fixture |
    | schwab CCC → DDD   | **the EXCHANGE-emptied source** (invisible to `book.holdings`) |
    | schwab DDD         | an exchange MERGING into a position that already exists |
    | schwab EEE → FFF   | SPINOFF: the parent keeps its shares (F-26), the child is created |
    | schwab GGG→HHH→×2  | a CHAIN — the transitive case, and F-08's live blocker |
    | schwab III         | an open DECLARED SHORT re-denominated by a SPLIT (E4) |
    | moomoo_my AAA      | **opening inventory dated ON the action date** (F-18 / D3) |
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.executemany(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        [("schwab", "Schwab", "Schwab", "USD", "TWD", "schwab_us", "drip_us"),
         ("moomoo_my", "Moomoo", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us")],
    )
    for sym in SYMBOLS:
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))

    def buy(acct: str, sym: str, qty: str, day: date, *, short: bool = False,
            side: Side = Side.BUY) -> None:
        insert_transaction(c, account_id=acct, symbol=sym, side=side, quantity=D(qty),
                           price=D("50"), fees=D("1"), tax=D("0"), trade_date=day,
                           short_sale=short)

    # --- AAA: split, then trade against the post-split count -----------------
    buy("schwab", "AAA", "100", date(2026, 1, 10))
    insert_corporate_action(c, account_id="schwab", action_date=SPLIT_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))
    buy("schwab", "AAA", "50", date(2026, 4, 1), side=Side.SELL)
    # A DRIP after the split — zero-cost shares on top of a re-denominated position.
    insert_dividend(c, account_id="schwab", symbol="AAA", div_date=date(2026, 4, 15),
                    div_type="DRIP", gross=D("70"), withholding=D("21"), net=D("49"),
                    reinvest_shares=D("2"), reinvest_price=D("24.5"))

    # --- moomoo_my AAA: opening dated ON the split date (F-18 / D3) ----------
    # D28 makes the split a two-row set, one per holding account; both rows are written.
    upsert_opening(c, account_id="moomoo_my", symbol="AAA", shares=D("100"),
                   original_cost_total=D("4000"), build_date=SPLIT_DAY)
    insert_corporate_action(c, account_id="moomoo_my", action_date=SPLIT_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1"))

    # --- BBB: no corporate action anywhere near it ---------------------------
    buy("schwab", "BBB", "80", date(2026, 1, 12))
    # A NET dividend CARRYING reinvest_shares (F-12): `build_book` books NET as cash and
    # adds no shares, so the naive SQL admitting it inflated this path by 7 shares on a
    # symbol with no corporate action at all.
    insert_dividend(c, account_id="schwab", symbol="BBB", div_date=date(2026, 5, 2),
                    div_type="NET", gross=D("30"), withholding=D("0"), net=D("30"),
                    reinvest_shares=D("7"), reinvest_price=D("4.3"))

    # --- CCC -> DDD: the EXCHANGE, merging into a position that already exists ---
    buy("schwab", "CCC", "40", date(2026, 1, 10))
    buy("schwab", "DDD", "10", date(2026, 2, 1))
    insert_corporate_action(c, account_id="schwab", action_date=date(2026, 5, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="CCC",
                            to_symbol="DDD", ratio_to=D("1"), ratio_from=D("2"))

    # --- EEE -> FFF: the SPINOFF. The parent KEEPS its 200 shares ------------
    buy("schwab", "EEE", "200", date(2026, 1, 10))
    insert_corporate_action(c, account_id="schwab", action_date=date(2026, 5, 15),
                            kind=CorporateActionKind.SPINOFF, from_symbol="EEE",
                            to_symbol="FFF", ratio_to=D("1"), ratio_from=D("4"),
                            cost_carry=D("0.2"))

    # --- GGG -> HHH -> split: the chain (F-08's live blocker) ----------------
    buy("schwab", "GGG", "60", date(2026, 1, 5))
    insert_corporate_action(c, account_id="schwab", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="GGG",
                            to_symbol="HHH", ratio_to=D("1"), ratio_from=D("1"))
    insert_corporate_action(c, account_id="schwab", action_date=date(2026, 6, 1),
                            kind=CorporateActionKind.SPLIT, from_symbol="HHH",
                            to_symbol="HHH", ratio_to=D("2"), ratio_from=D("1"))

    # --- III: an open DECLARED short, re-denominated by a split (E4) ---------
    buy("schwab", "III", "10", date(2026, 1, 10))
    buy("schwab", "III", "30", date(2026, 2, 10), short=True, side=Side.SELL)
    insert_corporate_action(c, account_id="schwab", action_date=date(2026, 6, 10),
                            kind=CorporateActionKind.SPLIT, from_symbol="III",
                            to_symbol="III", ratio_to=D("2"), ratio_from=D("1"))
    c.commit()
    yield c
    c.close()


def _replay(conn: sqlite3.Connection) -> tuple[
    dict[tuple[str, str], Decimal], set[tuple[str, str]]
]:
    """Replay the ledger and return the INTERNAL position map plus the flagged keys.

    ``build_book`` keeps ``positions`` local, so it is captured through the module's own
    seam: ``_apply_action`` receives the live dict and mutates it in place, so a wrapper
    holds the final map once the replay returns. Test-only, and it breaks loudly if that
    signature ever changes — which is the right failure for a test whose whole job is to
    notice the two implementations parting company.

    The flagged set comes from ``Book.unapplied_actions``, NOT from per-position booleans:
    two of the three ways an action goes unapplied leave no position to flag (F-47), and
    those are precisely the ones the walker would silently diverge on.
    """
    captured: dict[tuple[str, str], cost_basis._Position] = {}
    original = cost_basis._apply_action

    def spy(positions: dict[tuple[str, str], cost_basis._Position], *a: object,
            **kw: object) -> None:
        captured.clear()
        captured.update(positions)          # same object; mutations land in `captured`
        original(positions, *a, **kw)       # type: ignore[arg-type]
        captured.clear()
        captured.update(positions)

    cost_basis._apply_action = spy
    try:
        book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    finally:
        cost_basis._apply_action = original

    signed = {k: p.shares - p.short_shares for k, p in captured.items()}
    flagged: set[tuple[str, str]] = set()
    for u in book.unapplied_actions:
        flagged.add((u.account_id, u.from_symbol))
        flagged.add((u.account_id, u.to_symbol))
    return signed, flagged


def _all_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """The union of ``(account, symbol)`` over all four ledgers PLUS both action symbols.

    Deliberately NOT ``book.holdings`` (F-16). A zero-share position is dropped before it is
    emitted, so an enumeration built from the holdings list can never ask about the one case
    an EXCHANGE is most likely to get wrong.
    """
    keys: set[tuple[str, str]] = set()
    for sql in (
        "SELECT account_id, symbol FROM transactions",
        "SELECT account_id, symbol FROM opening_inventory",
        "SELECT account_id, symbol FROM dividends",
        "SELECT account_id, from_symbol FROM corporate_actions",
        "SELECT account_id, to_symbol FROM corporate_actions",
    ):
        keys.update((str(r[0]), str(r[1])) for r in conn.execute(sql))
    return keys


def test_parity_over_every_account_symbol(conn: sqlite3.Connection) -> None:
    """§7.2: the two independent share paths agree on EVERY position in the ledger."""
    replayed, flagged = _replay(conn)
    index = load_action_index(conn)
    keys = _all_keys(conn)
    # The enumeration must reach further than the emitted holdings, or nothing below bites.
    assert ("schwab", "CCC") in keys
    assert len(keys) > len(build_book(load_ledger_bundle(conn), allow_oversell=True).holdings)

    mismatches: list[str] = []
    for account_id, symbol in sorted(keys):
        if (account_id, symbol) in flagged:
            continue                        # divergence permitted, never required (§6.3)
        walked = current_shares(conn, account_id, symbol, index=index)
        expected = replayed.get((account_id, symbol), Decimal("0"))
        if walked != expected:
            mismatches.append(f"{symbol}({account_id}): walk {walked} != replay {expected}")
    assert not mismatches, "share paths disagree — " + "; ".join(mismatches)


def test_no_action_was_refused_by_the_replay(conn: sqlite3.Connection) -> None:
    """The fixture must exercise the walker, not the flagged-and-skipped exemption.

    Without this, a change that made every action unbookable would empty the parity test's
    key set through the ``flagged`` skip and leave it green — a test disabling itself.
    """
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert book.unapplied_actions == []
    assert not any(h.unbookable_action or h.oversold for h in book.holdings)


def test_the_cases_the_enumeration_exists_for(conn: sqlite3.Connection) -> None:
    """Pin the individual answers, so a parity failure says WHICH rule broke.

    ``test_parity_over_every_account_symbol`` proves the two paths agree; it does not prove
    they agree on the RIGHT number. These are the hand-computed values.
    """
    index = load_action_index(conn)

    def held(acct: str, sym: str) -> Decimal:
        return current_shares(conn, acct, sym, index=index)

    assert held("schwab", "AAA") == D("252")     # 100 ×3 − 50 + 2 DRIP
    assert held("moomoo_my", "AAA") == D("300")  # F-18: opening ON the split date, ×3
    assert held("schwab", "BBB") == D("80")      # F-12: the NET dividend adds NOTHING
    assert held("schwab", "CCC") == D("0")       # the EXCHANGE-emptied source
    assert held("schwab", "DDD") == D("30")      # 10 held + 40 exchanged at 1-for-2
    assert held("schwab", "EEE") == D("200")     # F-26: a SPINOFF's parent keeps its shares
    assert held("schwab", "FFF") == D("50")      # 200 at 1-for-4
    assert held("schwab", "GGG") == D("0")
    assert held("schwab", "HHH") == D("120")     # 60 exchanged 1-for-1, then split 2-for-1
    assert held("schwab", "III") == D("-40")     # long 10 − short 30, both re-denominated


def test_a_zero_walk_is_distinguishable_from_an_absent_position(
    conn: sqlite3.Connection,
) -> None:
    """Rule 2's reason to exist: CCC is 0 *and present*, JJJ is absent — not the same fact.

    Asserting against ``Book.holdings`` would collapse both to "not there", which is how a
    walker that forgot to empty an EXCHANGE source passes review.
    """
    replayed, _ = _replay(conn)
    assert replayed[("schwab", "CCC")] == D("0")
    assert ("schwab", "JJJ") not in replayed
    assert ("schwab", "CCC") not in {
        (h.account_id, h.symbol)
        for h in build_book(load_ledger_bundle(conn), allow_oversell=True).holdings
    }


def test_parity_test_detects_a_broken_walker(conn: sqlite3.Connection,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """DETECTION POWER — the mutation §7.2 exists to catch, applied to the walker itself.

    A SPINOFF that also empties its parent (the F-26 mistake: a generic "source side" branch
    mirroring EXCHANGE) produces a child that is still perfectly correct, so every per-kind
    unit test on FFF stays green. Only an enumeration that reaches EEE sees it.
    """
    real_delta = holdings_mod._Walk._delta_of

    def broken(self: holdings_mod._Walk, account_id: str, symbol: str,
               action: object) -> Decimal:
        kind = getattr(action, "kind", None)
        from_symbol = getattr(action, "from_symbol", None)
        if kind is CorporateActionKind.SPINOFF and symbol == from_symbol:
            return -self.shares(account_id, symbol,
                                (action.date, 10))  # type: ignore[attr-defined]
        return real_delta(self, account_id, symbol, action)  # type: ignore[arg-type]

    monkeypatch.setattr(holdings_mod._Walk, "_delta_of", broken)
    index = load_action_index(conn)
    assert current_shares(conn, "schwab", "FFF", index=index) == D("50")   # child still right
    assert current_shares(conn, "schwab", "EEE", index=index) == D("0")    # parent wiped
    with pytest.raises(AssertionError, match="EEE"):
        test_parity_over_every_account_symbol(conn)


# ============================================ D33 + the boundary of §7.2's property


def _short_then_exchange(conn: sqlite3.Connection) -> None:
    """schwab III already carries an OPEN DECLARED SHORT (−20 before the 2026-06-10 split).

    Exchanging it away is E5: 換股／分拆 has no honest entry for an open short, so the replay
    refuses. Dated before the split so the short is still open on the action date.
    """
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 20),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="III",
                            to_symbol="JJJ", ratio_to=D("1"), ratio_from=D("1"))
    conn.commit()


def test_d33_skips_an_exchange_off_a_negative_source_and_flags_it(
    conn: sqlite3.Connection,
) -> None:
    """D33 — the walk must NOT manufacture a destination the replay never created.

    Applied unconditionally, the share path moves −20 shares into JJJ: a symbol with no
    transaction, no opening and no holding, and therefore **no flag of any kind**. §6.3's
    drawer then renders ``＋公司行動 −20`` under a red 對帳不一致 with nothing to explain it.
    D33's ruling is skip **and** flag, and both halves are asserted here.
    """
    upsert_instrument(conn, Instrument(symbol="JJJ", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="JJJ"))
    _short_then_exchange(conn)
    index = load_action_index(conn)
    assert current_shares(conn, "schwab", "JJJ", index=index) == D("0")
    assert ("schwab", "JJJ") in index.negative_source_skips()
    assert ("schwab", "III") in index.negative_source_skips()

    # …and the replay agrees: it refused the same action, for its own reason.
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert [(u.from_symbol, u.to_symbol) for u in book.unapplied_actions] == [("III", "JJJ")]
    assert "JJJ" not in {h.symbol for h in book.holdings}


def test_d33_does_not_touch_a_split_on_an_open_short(conn: sqlite3.Connection) -> None:
    """E4 deliberately ALLOWS a split to re-denominate an open short, and it must survive.

    This is why D33's ``< 0`` skip is scoped to EXCHANGE / SPINOFF: a SPLIT has no separate
    destination to manufacture, and skipping it would silently under-report a real position.
    """
    index = load_action_index(conn)
    assert current_shares(conn, "schwab", "III", index=index) == D("-40")   # −20, split 2-for-1
    assert index.negative_source_skips() == frozenset()


def test_the_permitted_divergence_is_bounded_and_flagged(
    conn: sqlite3.Connection,
) -> None:
    """§7.2's property has a boundary, and it is E22 — measure it rather than assume it.

    The destination is 賣超 and then bought back to positive, so ``ever_oversold`` is STICKY
    while the share count is not negative. E22 refuses the EXCHANGE; the walker cannot see
    the sticky flag without importing the replay's oversell model, which D33 declined by
    name. So the two paths DIVERGE here — and the point of this test is that the divergence
    is bounded: the replay names the row in ``Book.unapplied_actions``, so §7.2's ``flagged``
    skip covers it and nothing silent slips through.
    """
    upsert_instrument(conn, Instrument(symbol="KKK", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="KKK"))
    # KKK: sell 30 with nothing held (賣超, basis discarded), then buy 50 back → +20, STICKY.
    insert_transaction(conn, account_id="schwab", symbol="KKK", side=Side.SELL,
                       quantity=D("30"), price=D("9"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 20))
    insert_transaction(conn, account_id="schwab", symbol="KKK", side=Side.BUY,
                       quantity=D("50"), price=D("9"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 25))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 7, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="BBB",
                            to_symbol="KKK", ratio_to=D("1"), ratio_from=D("1"))
    conn.commit()

    replayed, flagged = _replay(conn)
    index = load_action_index(conn)
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)

    # THE SOURCE SIDE — the symptom measured against the live app on 2026-08-10: the share
    # path empties BBB, so `/api/input/holdings` drops it from 持有 as 已結清, while the book
    # still holds its pre-action shares flagged 待釐清. RC-4's disagreement with the sides
    # swapped: validation used to be blind to actions the replay applied; now the share path
    # applies one the replay refuses.
    assert current_shares(conn, "schwab", "BBB", index=index) == D("0")
    assert replayed[("schwab", "BBB")] == D("80")
    assert next(h for h in book.holdings if h.symbol == "BBB").unbookable_action is True

    # THE DESTINATION SIDE: the walker moved BBB into KKK, the replay refused (E22).
    assert current_shares(conn, "schwab", "KKK", index=index) == D("100")   # 20 + 80 BBB
    assert replayed[("schwab", "KKK")] == D("20")
    assert index.negative_source_skips() == frozenset()   # KKK is POSITIVE — D33 cannot see it

    # …and it is bounded: both ends are named by the replay, so §7.2 skips them knowingly.
    assert ("schwab", "KKK") in flagged
    assert ("schwab", "BBB") in flagged
    assert any(u.to_symbol == "KKK" for u in
               build_book(load_ledger_bundle(conn), allow_oversell=True).unapplied_actions)

    # The rest of the ledger is untouched — containment, tier 1 (§8.1).
    test_parity_over_every_account_symbol(conn)
