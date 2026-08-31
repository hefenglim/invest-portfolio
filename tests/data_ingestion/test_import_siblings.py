"""A CSV import validates each row against the ledger PLUS its own siblings (C1).

**Measured defect, 2026-08-12.** ``build_transaction_preview`` validated every row against
the STORED ledger only, so a sell whose covering buy sat three lines above it in the same
file raised ``sell_exceeds_holdings`` — 7 of 47 rows on a synthetic broker export. The
numbers end up right if the owner confirms, which is precisely the problem: 賣超 is the
**one** confirmation in this system that permanently discards a cost basis (the STICKY rule,
``domain-ledger.md``), and a bulk import that raises it spuriously trains the owner to click
it without reading. A guard everybody clicks through is not a guard.

``cash_import.py`` named this class first — "**The whole file is one batch** … the E1a class
of failure" — and both ``validate_cash_movement`` and ``validate_corporate_action`` have
taken a ``batch`` argument since. The transaction door is the one that never got it.

**Why the fix lives in the WALKER and not in ``validate.py``.** The obvious version adds the
siblings to the returned count. That version is corporate-action-UNAWARE about them: a
sibling buy of 100 dated before a 4-for-1 split must meet a later sell as **400**, and only
``_Walk`` knows that. ``test_a_sibling_buy_is_re_expressed_by_a_split_between_them`` is the
one that fails on the naive version, and it is the split-then-sell cascade the whole
corporate-action feature exists to prevent — so getting it wrong here would reintroduce that
defect through the import door.

Every test that asserts a guard STAYS SILENT is paired with one proving it still fires.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
HEADER = "account,symbol,side,date,shares,price\n"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id,name,broker,settlement_ccy,funding_ccy,"
        "fee_rule_set,dividend_model) VALUES "
        "('schwab','S','Schwab','USD','TWD','schwab','drip_us')"
    )
    for sym in ("AAA", "BBB"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    c.commit()
    return c


def _oversold(preview: ImportPreview) -> list[int]:
    """Row indices carrying the 賣超 warning."""
    return [r.index for r in preview.rows
            if any(i.kind == "sell_exceeds_holdings" for i in r.issues)]


def _csv(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


# --- the defect itself --------------------------------------------------------------


def test_a_sell_covered_by_a_buy_earlier_in_the_same_file_is_not_oversold(
    conn: sqlite3.Connection,
) -> None:
    """The measured case, minimally: buy then sell, one file, empty ledger."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert _oversold(preview) == []


def test_the_same_sell_ALONE_is_still_oversold(conn: sqlite3.Connection) -> None:
    """Remove the sibling and the guard must come back — otherwise the test above is
    passing because the check was deleted rather than because it can see further."""
    preview = build_transaction_preview(conn, _csv("schwab,AAA,sell,2026-02-05,100,12"))
    assert _oversold(preview) == [0]


def test_a_sell_larger_than_its_siblings_cover_is_STILL_oversold(
    conn: sqlite3.Connection,
) -> None:
    """Sibling awareness widens what the guard can SEE, never what it permits."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,150,12",
    ))
    assert _oversold(preview) == [1]


# --- the ORDER of the two rows is a fact about dates, not about line numbers ---------


def test_a_buy_dated_AFTER_the_sell_does_not_cover_it(conn: sqlite3.Connection) -> None:
    """The date-aware rule (2026-07-31) survives the batch: the covering position is the one
    that exists on the sell's OWN trade date, so a later buy still leaves it 賣超 — even
    though the net across the file is zero. A batch-wide net would erase that check, and the
    replay would then discard the symbol's cost basis for good.
    """
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,sell,2026-01-05,100,12",
        "schwab,AAA,buy,2026-02-05,100,10",
    ))
    assert _oversold(preview) == [0]


def test_line_order_does_not_matter_when_the_dates_are_right(
    conn: sqlite3.Connection,
) -> None:
    """The same two rows, sell printed FIRST but dated second: no warning. A broker export
    is not necessarily in date order, and neither is a hand-edited file."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,sell,2026-02-05,100,12",
        "schwab,AAA,buy,2026-01-05,100,10",
    ))
    assert _oversold(preview) == []


# --- the walker's job, and the reason the fix is not a subtraction in validate.py ----


def test_a_sibling_buy_is_re_expressed_by_a_split_between_them(
    conn: sqlite3.Connection,
) -> None:
    """★ The test that fails if the siblings are added OUTSIDE the walk.

    Buy 100 (in the file) → stored 4-for-1 split → sell 400 (in the file). The sell is legal
    only because the sibling buy is re-expressed by the split, which a plain sum of pending
    quantities cannot do: it would compare 400 against 100 and raise the 賣超 that this
    whole feature exists to prevent.
    """
    insert_corporate_action(
        conn, account_id="schwab", action_date=date(2026, 1, 20),
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("4"), ratio_from=D("1"),
    )
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,40",
        "schwab,AAA,sell,2026-02-05,400,12",
    ))
    assert _oversold(preview) == []


def test_the_split_does_not_cover_MORE_than_the_ratio(conn: sqlite3.Connection) -> None:
    """401 is one share too many. The pair proves the ratio is applied, not waved."""
    insert_corporate_action(
        conn, account_id="schwab", action_date=date(2026, 1, 20),
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("4"), ratio_from=D("1"),
    )
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,40",
        "schwab,AAA,sell,2026-02-05,401,12",
    ))
    assert _oversold(preview) == [1]


def test_a_sell_BEFORE_the_split_is_measured_in_pre_split_shares(
    conn: sqlite3.Connection,
) -> None:
    """400 is fine after the 4-for-1 and 賣超 before it. Same file, same ratio, and the
    only difference is which side of the action the sell falls on."""
    insert_corporate_action(
        conn, account_id="schwab", action_date=date(2026, 1, 20),
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("4"), ratio_from=D("1"),
    )
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,40",
        "schwab,AAA,sell,2026-01-15,400,40",
    ))
    assert _oversold(preview) == [1]


# --- scope: a sibling is a sibling of ITS OWN position ------------------------------


def test_a_buy_of_a_DIFFERENT_symbol_covers_nothing(conn: sqlite3.Connection) -> None:
    preview = build_transaction_preview(conn, _csv(
        "schwab,BBB,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert _oversold(preview) == [1]


def test_a_buy_in_a_DIFFERENT_account_covers_nothing(conn: sqlite3.Connection) -> None:
    """Shares are held by an ACCOUNT (CLAUDE.md invariant 5), so a position in one cannot
    cover a sell in another — even for the same symbol, in the same file, in date order."""
    conn.execute(
        "INSERT INTO accounts (account_id,name,broker,settlement_ccy,funding_ccy,"
        "fee_rule_set,dividend_model) VALUES "
        "('schwab2','S2','Schwab','USD','TWD','schwab','drip_us')"
    )
    conn.commit()
    preview = build_transaction_preview(conn, _csv(
        "schwab2,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert _oversold(preview) == [1]


def test_an_unparseable_row_is_not_a_sibling(conn: sqlite3.Connection) -> None:
    """A row with no date and no quantity cannot cover anything. Counting it would be
    inventing a share flow out of a defect — and the file still has a real 賣超 in it."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,not-a-date,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert any(i.kind == "parse_error" for i in preview.rows[0].issues)
    assert _oversold(preview) == [1]


def test_the_stored_ledger_still_counts(conn: sqlite3.Connection) -> None:
    """Siblings are ADDED to the ledger, not substituted for it."""
    insert_transaction(conn, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("60"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2025, 12, 1))
    conn.commit()
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,40,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert _oversold(preview) == []


# --- a declared short moves shares even though its own row is exempt ----------------


def test_a_declared_short_sibling_still_reduces_the_position(
    conn: sqlite3.Connection,
) -> None:
    """The short row is exempt from the guard; the shares it sold are gone all the same, so
    the ORDINARY sell that follows it is 賣超. Exempting the row must not exempt its flow.
    """
    preview = build_transaction_preview(
        conn,
        "account,symbol,side,date,shares,price,short_sale\n"
        "schwab,AAA,buy,2026-01-05,100,10,0\n"
        "schwab,AAA,sell,2026-01-10,100,11,1\n"
        "schwab,AAA,sell,2026-02-05,100,12,0\n",
    )
    assert _oversold(preview) == [2]


# --- trades arriving WITH the actions that legalise them (the broker one-click flow) --


def test_a_post_split_sell_is_clean_when_the_split_arrives_in_the_SAME_run(
    conn: sqlite3.Connection,
) -> None:
    """★ ``pending_actions``: the split is not in the ledger yet, but it is in this import.

    The commit order is trades-then-actions and cannot be reversed — a corporate action's
    own guards need the position to exist, so actions-first rejects them (measured
    2026-08-12: 3 of 5 dropped). That leaves a sell which is only legal AFTER the split
    meeting a pre-split share count, and raising 賣超 — the one confirmation whose
    acknowledgement permanently discards a cost basis. The ledger would still end up right,
    because the split lands seconds later and every report rebuilds from the ledgers; what
    is not acceptable is teaching the owner to click that dialog through false positives.
    """
    from portfolio_dash.data_ingestion.corporate_action_import import parse_action_batch
    from portfolio_dash.data_ingestion.holdings import load_action_index

    actions_csv = (
        "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"
        "schwab,2026-01-20,SPLIT,AAA,AAA,4,1,,\n"
    )
    index = load_action_index(conn, pending=parse_action_batch(actions_csv))
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,40",
        "schwab,AAA,sell,2026-02-05,400,12",
    ), pending_actions=index)
    assert _oversold(preview) == []


def test_without_the_pending_split_the_same_sell_IS_flagged(
    conn: sqlite3.Connection,
) -> None:
    """The paired proof, and also the measurement of what the flag is worth: identical rows,
    identical ledger, and the only difference is whether the importer was told the split is
    coming."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,40",
        "schwab,AAA,sell,2026-02-05,400,12",
    ))
    assert _oversold(preview) == [1]


def test_a_pending_action_widens_what_is_SEEN_not_what_is_allowed(
    conn: sqlite3.Connection,
) -> None:
    """1,601 shares is past what even a 4-for-1 covers. A pending action is evidence, not a
    waiver — if it were a waiver, every broker import would silence the guard entirely."""
    from portfolio_dash.data_ingestion.corporate_action_import import parse_action_batch
    from portfolio_dash.data_ingestion.holdings import load_action_index

    index = load_action_index(conn, pending=parse_action_batch(
        "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"
        "schwab,2026-01-20,SPLIT,AAA,AAA,4,1,,\n"
    ))
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,400,40",
        "schwab,AAA,sell,2026-02-05,1601,12",
    ), pending_actions=index)
    assert _oversold(preview) == [1]


# --- QA-01 on the share ledger (FIX-A1): only rows that will be WRITTEN are siblings --


def test_a_deselected_buy_stops_covering_the_sell(conn: sqlite3.Connection) -> None:
    """★ The commit-door defect at this seam: with ``select`` naming only the sell, the
    covering buy is a flow no ledger will ever hold, so the sell is 賣超 again — the
    cash door's deselected-deposit rule, replayed with shares."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ), select={1})
    assert _oversold(preview) == [1]


def test_select_narrows_the_batch_and_nothing_else(conn: sqlite3.Connection) -> None:
    """Every row stays in the output with its index, raw text and payload — ``row_hashes``
    and the wire depend on that alignment — and a deselected row still gets its own
    verdict; it only stops covering its siblings."""
    full = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    narrowed = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ), select={1})
    assert [r.index for r in narrowed.rows] == [r.index for r in full.rows]
    assert [r.raw for r in narrowed.rows] == [r.raw for r in full.rows]
    assert [r.payload for r in narrowed.rows] == [r.payload for r in full.rows]


def test_selecting_both_rows_is_the_whole_file_batch(conn: sqlite3.Connection) -> None:
    """A selection that keeps the cover changes nothing — the paired proof that the
    narrowing subtracts only what was deselected."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-02-05,100,12",
    ), select={0, 1})
    assert _oversold(preview) == []


# --- FIX-A1b (Master V6): a structurally invalid row is not a sibling either --------


def test_a_structurally_invalid_buy_covers_nothing(conn: sqlite3.Connection) -> None:
    """★ The V6 shape: a buy priced −50.00 is a hard ``error`` on its own line — it can
    never be written, so it can never cover. Until FIX-A1b it stayed in the batch and the
    sell previewed ``ok``; a no-select commit then wrote the sell ALONE (200 / written 1 /
    a lone unacked oversold SELL), reachable by a mere typo. The cash door has always had
    this boundary (``_pool_free_issues`` decides membership)."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,-50.00",
        "schwab,AAA,sell,2026-02-05,60,12",
    ))
    assert preview.rows[0].has_hard_issue
    assert _oversold(preview) == [1]


def test_a_structurally_invalid_sell_does_not_drain_its_siblings(
    conn: sqlite3.Connection,
) -> None:
    """The exclusion errs conservative in BOTH directions: a hard-invalid sell will never
    book its outflow, so the later sell really is covered by what the ledger will hold —
    flagging it would be a false 賣超 against a fictitious drain. Only row 2's verdict is
    pinned: the broken row's OWN soft findings are rendered against ITS siblings (row 2
    included, correctly — they describe what happens if its price is fixed) and are moot
    under its hard error anyway."""
    preview = build_transaction_preview(conn, _csv(
        "schwab,AAA,buy,2026-01-05,100,10",
        "schwab,AAA,sell,2026-01-10,100,-1",
        "schwab,AAA,sell,2026-02-05,100,12",
    ))
    assert preview.rows[1].has_hard_issue
    assert 2 not in _oversold(preview)


def test_the_boundary_is_the_structural_family_and_the_rest_cannot_false_cover(
    conn: sqlite3.Connection,
) -> None:
    """Why the ledger-dependent HARD kinds stay IN the batch: a pending flow covers only
    its own ``(account, symbol)`` key, and those kinds hit every row of that key equally —
    so the "covered" sell is exactly as un-writable as the buy that covers it, and no
    writable row ever gains a false cover. Pinned for the two reachable kinds: an unknown
    account and an unregistered symbol each hard-block BOTH halves of the pair."""
    unknown_account = build_transaction_preview(conn, _csv(
        "ghost,AAA,buy,2026-01-05,100,10",
        "ghost,AAA,sell,2026-02-05,60,12",
    ))
    assert all(r.has_hard_issue for r in unknown_account.rows)
    unregistered = build_transaction_preview(conn, _csv(
        "schwab,NOPE,buy,2026-01-05,100,10",
        "schwab,NOPE,sell,2026-02-05,60,12",
    ))
    assert all(r.has_hard_issue for r in unregistered.rows)


# --- performance: one ActionIndex for the file, not one per row (trap #21) ----------


def test_the_action_index_is_read_once_for_the_whole_file(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trap #21, on the door that had it live.

    ``validate_transaction`` builds its own index when the caller passes none, and
    ``build_transaction_preview`` passed none — so a 1,375-row export re-read and re-grouped
    the whole corporate-action ledger 1,375 times. Counted rather than timed: a timing
    assertion on a small fixture measures the machine, not the code.
    """
    from portfolio_dash.data_ingestion.holdings import load_action_index

    calls = 0

    def counted(c: sqlite3.Connection, **kw: object) -> object:
        nonlocal calls
        calls += 1
        return load_action_index(c, **kw)  # type: ignore[arg-type]

    # BOTH names, by string path. ``from … import`` binds a reference at module load, so
    # patching the definition in ``holdings`` would leave every importer on the original —
    # a patch that silently measures nothing, which is how a performance test comes back
    # green having tested the wrong function.
    monkeypatch.setattr(
        "portfolio_dash.data_ingestion.validate.load_action_index", counted)
    monkeypatch.setattr(
        "portfolio_dash.data_ingestion.csv_import.load_action_index", counted)
    build_transaction_preview(conn, _csv(
        *(f"schwab,AAA,buy,2026-01-0{n},10,10" for n in range(1, 9))
    ))
    assert calls == 1
