"""A chained corporate action must import in ONE file, and must not justify itself (C2).

**Measured defect, 2026-08-12, and it failed SILENTLY.** ``build_corporate_action_preview``
built its :class:`ActionIndex` from the STORED ledger only. So a SPLIT whose symbol's only
shares arrive from an EXCHANGE **earlier in the same file** walked back to a position that
did not exist yet and was hard-rejected ``no_position_on_action_date``. ``commit_preview``
drops hard-issue rows and the response counted them as ``skipped``, so the import reported
success, the split never applied, and every share count for that symbol was off by the
ratio — with no flag anywhere, because as far as the ledger knew the action was never
attempted.

The shape is not exotic. **Both of the owner's real chains have it** (a de-SPAC into a new
ticker then a rename; a de-SPAC then a reverse split), and a broker export naturally carries
a whole history in one file — which is the only way this ledger can receive one.

The fix is the same sentence as C1's, one ledger over: **the whole file is one batch.**

⚠ The dangerous version of this fix lets a row justify ITSELF. That is guarded structurally
rather than by a rule: the walk's cut is ``(action.date, EventPriority.CORPORATE_ACTION)``
and it applies only actions sorting strictly before it. Every "the chain works" test below is
paired with one proving a lone action onto an empty position is still refused.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.corporate_action_import import (
    build_corporate_action_preview,
)
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
HEADER = "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"


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
    for sym in ("SPAC", "NEWCO", "RENAMED"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    c.commit()
    return c


def _buy(conn: sqlite3.Connection, sym: str, qty: str, day: date) -> None:
    insert_transaction(conn, account_id="schwab", symbol=sym, side=Side.BUY,
                       quantity=D(qty), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=day)
    conn.commit()


def _csv(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def _hard(preview: ImportPreview, idx: int) -> set[str]:
    row = next(r for r in preview.rows if r.index == idx)
    return {i.kind for i in row.issues if not i.needs_confirm}


# --- the chain ----------------------------------------------------------------------


def test_a_split_whose_shares_came_from_an_EXCHANGE_in_the_same_file_commits(
    conn: sqlite3.Connection,
) -> None:
    """★ The measured case: buy SPAC, EXCHANGE into NEWCO, then split NEWCO — one file."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-02-01,EXCHANGE,SPAC,NEWCO,1,1,,",
        "schwab,2026-03-01,SPLIT,NEWCO,NEWCO,4,1,,",
    ))
    assert _hard(preview, 0) == set()
    assert _hard(preview, 1) == set()


def test_a_rename_after_an_exchange_commits_too(conn: sqlite3.Connection) -> None:
    """The owner's other real shape: EXCHANGE, then a 1-for-1 EXCHANGE (a rename)."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-02-01,EXCHANGE,SPAC,NEWCO,1,1,,",
        "schwab,2026-03-01,EXCHANGE,NEWCO,RENAMED,1,1,,",
    ))
    assert _hard(preview, 0) == set()
    assert _hard(preview, 1) == set()


def test_the_chain_commits_no_matter_which_LINE_comes_first(
    conn: sqlite3.Connection,
) -> None:
    """A converter emits rows in whatever order it found them. The index is grouped by
    date, so the chain resolves on its dates and not on its line numbers."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-03-01,SPLIT,NEWCO,NEWCO,4,1,,",
        "schwab,2026-02-01,EXCHANGE,SPAC,NEWCO,1,1,,",
    ))
    assert _hard(preview, 0) == set()
    assert _hard(preview, 1) == set()


# --- and the guard must still bite --------------------------------------------------


def test_a_lone_split_onto_an_empty_position_is_still_rejected(
    conn: sqlite3.Connection,
) -> None:
    """★ The paired proof. Same split, same file, but NOTHING puts shares into NEWCO — so
    the row would be justifying itself if the index made it visible to its own walk."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-03-01,SPLIT,NEWCO,NEWCO,4,1,,",
    ))
    assert "no_position_on_action_date" in _hard(preview, 0)


def test_an_exchange_out_of_a_symbol_nobody_holds_is_still_rejected(
    conn: sqlite3.Connection,
) -> None:
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-02-01,EXCHANGE,SPAC,NEWCO,1,1,,",
    ))
    assert "no_position_on_action_date" in _hard(preview, 0)


def test_a_sibling_dated_AFTER_the_action_does_not_supply_its_position(
    conn: sqlite3.Connection,
) -> None:
    """The split happens BEFORE the exchange that would have created its position. Being
    in the same file must not collapse the two dates into "somewhere in this batch"."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-02-01,SPLIT,NEWCO,NEWCO,4,1,,",
        "schwab,2026-03-01,EXCHANGE,SPAC,NEWCO,1,1,,",
    ))
    assert "no_position_on_action_date" in _hard(preview, 0)
    assert _hard(preview, 1) == set()


def test_a_malformed_sibling_never_reaches_the_walk(conn: sqlite3.Connection) -> None:
    """A ratio of 1.5 is not a :class:`CorporateAction` (D14), so ``convert_stored`` files
    it as unreadable and it cannot lend its (nonexistent) shares to anything. The row is
    rejected on its own terms, and the SECOND row is judged as if it were not there."""
    _buy(conn, "SPAC", "100", date(2026, 1, 5))
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2026-02-01,EXCHANGE,SPAC,NEWCO,1.5,1,,",
        "schwab,2026-03-01,SPLIT,NEWCO,NEWCO,4,1,,",
    ))
    assert _hard(preview, 0) != set()                                # the bad row is refused
    assert "no_position_on_action_date" in _hard(preview, 1)         # and lends nothing


# --- FIX-A2 (QA BUG-04): the BOOK replay sees same-batch predecessors too -----------
#
# The chain tests above have an empty destination ledger, so only the SHARE WALK (E1a)
# needed the sibling — and it has had it since 2026-08-14. The shape below is the owner's
# real GGR history: the post-EXCHANGE trades are ALREADY STORED, so without the sibling the
# four book-derived rejections (E3/E22/E5/E18) replay a destination that sells shares it
# never received, flag it oversold, and E3 hard-rejects the very row whose prerequisite is
# eight lines up in the same file. Pass 2 of the byte-identical file then wrote it, because
# pass 1 had stored the EXCHANGE — an undocumented two-pass behind a message that told the
# owner to 補登 a buy that exists.


def _ggr_shape(conn: sqlite3.Connection) -> None:
    """Stored trades of the real history: buy the SPAC, then trade the DESTINATION between
    the (not yet imported) EXCHANGE and reverse SPLIT dates."""
    _buy(conn, "SPAC", "300", date(2022, 1, 18))
    insert_transaction(conn, account_id="schwab", symbol="NEWCO", side=Side.SELL,
                       quantity=D("299"), price=D("5"), fees=D("0"), tax=D("0"),
                       trade_date=date(2022, 6, 30))
    insert_transaction(conn, account_id="schwab", symbol="NEWCO", side=Side.BUY,
                       quantity=D("199"), price=D("0.53"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 9, 23))
    conn.commit()


def test_a_chain_whose_post_action_trades_are_stored_passes_in_ONE_pass(
    conn: sqlite3.Connection,
) -> None:
    """★ The GGR regression: EXCHANGE + dependent reverse SPLIT in one file, trades already
    stored — no hard issue on either row, first pass."""
    _ggr_shape(conn)
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2022-04-05,EXCHANGE,SPAC,NEWCO,300,300,,",
        "schwab,2025-10-06,SPLIT,NEWCO,NEWCO,10,200,,",
    ))
    assert _hard(preview, 0) == set()
    assert _hard(preview, 1) == set()


def test_the_one_pass_actually_WRITES_both_rows(conn: sqlite3.Connection) -> None:
    """The defect was measured at the commit (written 1 / rejected 1, then pass 2), so the
    pin is on the commit's buckets, not only on the preview's issue sets."""
    from portfolio_dash.data_ingestion.corporate_action_import import (
        write_corporate_action_row,
    )
    from portfolio_dash.data_ingestion.preview import commit_preview

    _ggr_shape(conn)
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2022-04-05,EXCHANGE,SPAC,NEWCO,300,300,,",
        "schwab,2025-10-06,SPLIT,NEWCO,NEWCO,10,200,,",
    ))
    summary = commit_preview(conn, preview, accept={0, 1},
                             writer=write_corporate_action_row)
    assert len(summary.written) == 2
    assert summary.rejected == []


def test_the_same_split_with_the_prerequisite_absent_is_still_oversold_source(
    conn: sqlite3.Connection,
) -> None:
    """The paired proof: no EXCHANGE anywhere — not stored, not in the file — and the
    destination genuinely sold shares it never received, so E3 must still hard-reject,
    and its message (「請先補登…」) is now accurate."""
    _ggr_shape(conn)
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2025-10-06,SPLIT,NEWCO,NEWCO,10,200,,",
    ))
    assert "oversold_source" in _hard(preview, 0)


def test_a_malformed_prerequisite_lends_no_position_to_the_BOOK_either(
    conn: sqlite3.Connection,
) -> None:
    """`convert_stored` is the ONE converter for both halves (index and book), so a ratio
    of 1.5 excludes itself from the replayed book exactly as it does from the walk — the
    dependent SPLIT stays rejected rather than passing on shares a refused row lent it."""
    _ggr_shape(conn)
    preview = build_corporate_action_preview(conn, _csv(
        "schwab,2022-04-05,EXCHANGE,SPAC,NEWCO,1.5,1,,",
        "schwab,2025-10-06,SPLIT,NEWCO,NEWCO,10,200,,",
    ))
    assert _hard(preview, 0) != set()
    assert "oversold_source" in _hard(preview, 1)
