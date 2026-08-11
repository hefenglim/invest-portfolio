"""W7 — the 5th CSV kind: the corporate-action importer (audit F-28 / F-29 / F-40).

Everything W2 built was unreachable in production: ``validate_corporate_action`` had zero
callers, so every §5 rejection and every soft warning existed only in tests (F-40). This
importer is the door that makes them real, and it has two obligations no other CSV kind
has (F-29):

* **the FULL batch** is passed to ``validate_corporate_action``. E12 and E13 are
  batch-level rules; a per-row call REJECTS a correct multi-account entry (every row sees
  a sibling account it does not cover) and ACCEPTS a partial one.
* **``book`` and the ``ActionIndex`` are hoisted ONCE** for the whole file. Both replay /
  re-group the entire ledger, so a per-row build is trap #21 with a different object.

Both are asserted here by observation (a counter around the real functions), not by
reading the source — a comment claiming "hoisted once" is not a test.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import corporate_action_import as cai
from portfolio_dash.data_ingestion import validate as validate_module
from portfolio_dash.data_ingestion.corporate_action_import import (
    CORPORATE_ACTION_COLUMNS,
    build_corporate_action_preview,
    write_corporate_action_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, commit_preview
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    list_corporate_actions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
ACTION_DAY = date(2026, 6, 15)
BUY_DAY = date(2026, 1, 10)
_HEADER = ",".join(CORPORATE_ACTION_COLUMNS) + "\n"


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
    for sym in ("AAA", "BBB"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    # AAA held in BOTH accounts on the action date — what makes E13 bite.
    for acct in ("schwab", "moomoo_my"):
        insert_transaction(c, account_id=acct, symbol="AAA", side=Side.BUY,
                           quantity=D("100"), price=D("50"), fees=D("0"), tax=D("0"),
                           trade_date=BUY_DAY)
    c.commit()
    return c


def _row(account: str = "schwab", **over: str) -> str:
    cells: dict[str, str] = {
        "account": account, "date": ACTION_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "AAA", "to_symbol": "AAA",
        "ratio_to": "3", "ratio_from": "1", "cost_carry": "", "note": "",
    }
    cells.update(over)
    return ",".join(cells[c] for c in CORPORATE_ACTION_COLUMNS) + "\n"


def _kinds(preview: ImportPreview) -> list[set[str]]:
    return [{i.kind for i in r.issues} for r in preview.rows]


# --------------------------------------------------------------- F-40: the full batch


def test_the_complete_multi_account_batch_validates_clean(conn: sqlite3.Connection) -> None:
    """AAA is held in two accounts; a file covering BOTH is the shape D13 demands.

    RED without ``batch=``: ``validate_corporate_action`` cannot see the sibling row, so
    E13 fires on EVERY row and a correct entry is rejected.
    """
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab") + _row("moomoo_my"))
    assert len(preview.rows) == 2
    assert _kinds(preview) == [set(), set()]
    assert not any(r.has_hard_issue for r in preview.rows)


def test_a_partial_multi_account_batch_is_rejected(conn: sqlite3.Connection) -> None:
    """One row for a symbol two accounts hold — E13's silent-corruption case (trap #13).

    RED without ``batch=`` only in the other direction; this asserts the guard still bites
    once the batch is threaded, so the fix above cannot be "pass an empty batch".
    """
    preview = build_corporate_action_preview(conn, _HEADER + _row("schwab"))
    assert _kinds(preview) == [{"incomplete_account_coverage"}]
    assert preview.rows[0].has_hard_issue


def test_a_duplicate_inside_one_file_is_seen(conn: sqlite3.Connection) -> None:
    """E12/E15 look at the rest of the SUBMITTED batch, not only at stored rows."""
    text = _HEADER + _row("schwab") + _row("moomoo_my") + _row("schwab", ratio_to="9")
    preview = build_corporate_action_preview(conn, text)
    assert "conflicting_ratio" in _kinds(preview)[2]


# ------------------------------------------------------- F-29: one book, one index


def test_book_and_action_index_are_hoisted_once_per_file(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4-row file reads the ledger ONCE and replays it once PER DISTINCT ACTION DATE.

    Counted on BOTH sides, which is what makes this test able to fail: the importer's own
    calls must be exactly one each, and ``validate.py``'s internal work must be bounded by
    the number of distinct action **dates** — not by the number of rows, which is the
    per-row rebuild F-29 warns about (a ~1,400-row import replaying the whole ledger 1,400
    times).

    ⚠ **The `validate.book` expectation changed from 0 to 1 on 2026-08-11, and the change is
    the point.** It used to assert the validator NEVER replays, because the importer handed
    it a pre-replayed whole-ledger book. That hoist was hoisting the wrong object: the four
    book-derived rejections (E3/E22/E5/E18) must see the ledger **at each action's own
    date**, and a whole-ledger book showed them a future in which the post-action trades had
    already happened — so E3 rejected the very split that made those trades legal (trap #24,
    D41). The validator now scopes and replays it itself, memoised in the caller's
    ``book_cache``. All four rows here share one action date, so **one** replay covers them;
    a file spanning three dates would legitimately replay three times. Asserting 0 again
    would re-introduce the blocker in the name of performance.
    """
    calls = {"cai.book": 0, "cai.index": 0, "validate.book": 0, "validate.index": 0}

    def count(module: object, name: str, key: str) -> None:
        real = getattr(module, name)

        def counted(*a: object, **kw: object) -> object:
            calls[key] += 1
            return real(*a, **kw)

        monkeypatch.setattr(module, name, counted)

    count(cai, "build_book", "cai.book")
    count(cai, "load_action_index", "cai.index")
    count(validate_module, "build_book", "validate.book")
    count(validate_module, "load_action_index", "validate.index")

    text = (_HEADER + _row("schwab") + _row("moomoo_my")
            + _row("schwab", from_symbol="BBB", to_symbol="BBB")
            + _row("moomoo_my", from_symbol="BBB", to_symbol="BBB"))
    preview = build_corporate_action_preview(conn, text)
    assert len(preview.rows) == 4
    # All four rows share ONE action date, so one date-scoped replay serves all of them.
    assert calls == {"cai.book": 1, "cai.index": 1,
                     "validate.book": 1, "validate.index": 0}

    # …and the bound really is per DATE, not a constant: three dates → three replays, still
    # far short of the six rows. Without `book_cache` this would read 6.
    calls.update({"cai.book": 0, "cai.index": 0, "validate.book": 0, "validate.index": 0})
    spread = (_HEADER
              + _row("schwab") + _row("moomoo_my")
              + _row("schwab", date="2026-06-16") + _row("moomoo_my", date="2026-06-16")
              + _row("schwab", date="2026-06-17") + _row("moomoo_my", date="2026-06-17"))
    assert len(build_corporate_action_preview(conn, spread).rows) == 6
    assert calls["validate.book"] == 3


# ------------------------------------------------------------------ parse rejections


def test_a_single_ratio_column_is_a_hard_parse_error(conn: sqlite3.Connection) -> None:
    """E6a: 「ratio」 as one decimal column is refused at the header, with a zh message.

    Accepting ``0.2857`` silently is exactly the failure §3.1(ii) documents — 700 shares
    become 199.99 and the later sell of 200 discards the position's basis.
    """
    text = "account,date,kind,from_symbol,to_symbol,ratio,note\n" \
           f"schwab,{ACTION_DAY.isoformat()},SPLIT,AAA,AAA,0.2857,\n"
    preview = build_corporate_action_preview(conn, text)
    assert _kinds(preview) == [{"single_ratio_column"}]
    assert preview.rows[0].has_hard_issue
    (issue,) = preview.rows[0].issues
    assert "ratio_to" in issue.message and "ratio_from" in issue.message
    assert "小數" in issue.message


def test_a_non_integer_ratio_term_is_rejected(conn: sqlite3.Connection) -> None:
    """D14 / trap #1 — the path the two-integer entry form cannot police."""
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab", ratio_to="0.2857", ratio_from="1"))
    assert "ratio_not_positive_integer" in _kinds(preview)[0]


def test_an_unparseable_cell_is_a_row_level_parse_error(conn: sqlite3.Connection) -> None:
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab", ratio_to="3:1"))
    assert _kinds(preview) == [{"parse_error"}]


def test_a_zh_kind_label_is_accepted(conn: sqlite3.Connection) -> None:
    """The owner reads 分割／換股／分拆 on the statement; the CSV takes either vocabulary."""
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab", kind="分割") + _row("moomoo_my", kind="分割"))
    assert _kinds(preview) == [set(), set()]
    assert preview.rows[0].payload["kind"] == "SPLIT"


def test_a_legacy_account_id_is_aliased(conn: sqlite3.Connection) -> None:
    """Batch B: the shared alias map, same as the other four importers."""
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab") + _row("moomoo_my_us"))
    assert preview.rows[1].payload["account_id"] == "moomoo_my"
    assert "account_alias" in _kinds(preview)[1]


# ------------------------------------------------------------------------- the writer


def test_commit_writes_every_accepted_row(conn: sqlite3.Connection) -> None:
    preview = build_corporate_action_preview(
        conn, _HEADER + _row("schwab") + _row("moomoo_my"))
    summary = commit_preview(
        conn, preview, accept={0, 1}, writer=write_corporate_action_row)
    assert len(summary.written) == 2 and not summary.skipped
    stored = list_corporate_actions(conn)
    assert {s.account_id for s in stored} == {"schwab", "moomoo_my"}
    assert {(s.ratio_to, s.ratio_from) for s in stored} == {(D("3"), D("1"))}


def test_a_spinoff_carries_its_cost_carry_through_the_writer(
    conn: sqlite3.Connection
) -> None:
    text = _HEADER + _row("schwab", kind="SPINOFF", to_symbol="BBB", ratio_to="1",
                          ratio_from="2", cost_carry="0.4") \
                   + _row("moomoo_my", kind="SPINOFF", to_symbol="BBB", ratio_to="1",
                          ratio_from="2", cost_carry="0.4")
    preview = build_corporate_action_preview(conn, text)
    assert not any(r.has_hard_issue for r in preview.rows)
    commit_preview(conn, preview, accept={0, 1}, writer=write_corporate_action_row)
    assert {s.cost_carry for s in list_corporate_actions(conn)} == {D("0.4")}
