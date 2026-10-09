"""The demo corpus is a release gate, so the script that produces it is tested.

`engineering-process.md` deploys every tag to the demo site FIRST and verifies it there
before prod. That check is only worth running against data that exercises the feature, so
spec §7.7 (D25) requires `scripts/seed_demo.py` to carry one corporate action of each kind
— and trap #22 is shipping the feature without it, so the staged deploy returns green
having tested none of it. Nothing else in the suite touches this script; without these
tests the corpus can rot silently and the failure surfaces as a demo deploy that proved
nothing, which is the least useful place to find it.

Four things are asserted, each because it has its own way of going quietly wrong:

1. **The corpus reaches an ALREADY-SEEDED database.** The live demo was seeded long before
   corporate actions existed and demo data is never reset (owner ruling 2026-07-31). A
   top-up that only ran on a fresh seed would never arrive.
2. **The app's own entry path would accept every row.** A fixture the validator rejects
   teaches the wrong lesson at exactly the moment someone is verifying a release.
3. **D13's all-accounts rule is really satisfied** — two rows for the two-account SPLIT.
4. **The stored price basis is right, and the split factor is 3 and not 9.** Deduplication
   across accounts is §5.1 detail 3's silent failure: a multi-account 3-for-1 that factors
   per row raises the price to the power of the holder count, and nothing else notices.
"""

import sqlite3
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import list_corporate_actions
from portfolio_dash.data_ingestion.validate import validate_corporate_action
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from scripts import seed_demo


@pytest.fixture()
def conn(tmp_path: Any) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "demo.db"))
    c.row_factory = sqlite3.Row
    return c


def _legacy_demo_db(c: sqlite3.Connection) -> None:
    """A demo database as it existed BEFORE this feature: base ledger, no actions."""
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    seed_demo._seed_base(c)


def _price_row(c: sqlite3.Connection, symbol: str, day: str) -> sqlite3.Row:
    row: sqlite3.Row | None = c.execute(
        "SELECT close, close_raw, split_basis FROM prices WHERE instrument=? AND as_of_date=?",
        (symbol, day),
    ).fetchone()
    assert row is not None, f"no price row for {symbol} on {day}"
    return row


def test_seed_installs_the_corpus_on_an_already_seeded_database(
    conn: sqlite3.Connection,
) -> None:
    """The live-demo case: a ledger is already there, and the corpus must still arrive.

    `seed()` keeps refusing to re-seed the BASE ledger (that refusal is what protects the
    accumulating stress-test corpus), so this asserts both halves at once — the base row
    counts are untouched and the corporate actions appear anyway.
    """
    _legacy_demo_db(conn)
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0

    seed_demo.seed(conn)

    actions = list_corporate_actions(conn)
    assert len(actions) == 4, "the §7.7 corpus did not reach an already-seeded database"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > before
    # the base ledger's own rows are untouched: 6 originals + the corpus's own positions
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE symbol IN ('2330','0056','AAPL','NVDA','1155')"
    ).fetchone()[0] == 6


def test_seeding_twice_changes_nothing(conn: sqlite3.Connection) -> None:
    """Re-running must not duplicate a ledger row or move a stored price.

    An ops script the operator may run twice is one that WILL be run twice. A duplicated
    corporate action is not a cosmetic problem: E15 exists because re-entering a 3-for-1
    applies the ratio again and makes it a 9-for-1.
    """
    seed_demo.seed(conn)
    snapshot = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("transactions", "dividends", "cash_movements",
                      "fx_conversions", "corporate_actions", "instruments")
    }
    prices = conn.execute(
        "SELECT instrument, as_of_date, close, close_raw, split_basis FROM prices ORDER BY 1,2"
    ).fetchall()

    seed_demo.seed(conn)

    assert snapshot == {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in snapshot
    }
    assert [tuple(r) for r in prices] == [
        tuple(r) for r in conn.execute(
            "SELECT instrument, as_of_date, close, close_raw, split_basis "
            "FROM prices ORDER BY 1,2"
        ).fetchall()
    ]


def test_every_seeded_action_passes_the_real_entry_validator(
    conn: sqlite3.Connection,
) -> None:
    """Validated in the state the entry surface sees: positions and destination prices
    written, actions not yet stored. Soft issues count too — a `needs_confirm` row is one
    the owner would have had to click through, which is not a state to bake into a fixture.
    """
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    seed_demo._seed_base(conn)
    seed_demo._seed_ca_positions(conn)
    seed_demo._seed_ca_prices(conn)

    batch = seed_demo._ca_batch()
    found = {
        (inp.account_id, inp.from_symbol): validate_corporate_action(conn, inp, batch=batch)
        for inp in batch
    }
    assert all(not issues for issues in found.values()), {
        key: [(i.kind, i.message) for i in issues] for key, issues in found.items() if issues
    }


def test_the_corpus_covers_one_of_each_kind_and_all_accounts(
    conn: sqlite3.Connection,
) -> None:
    """§7.7's table, row by row — including D13's all-or-nothing across accounts.

    The multi-account SPLIT is TWO rows carrying the same (symbol, date, ratio). One row is
    the exact state E13 forbids, and it is invisible from every existing check: the
    un-actioned account's footer still prints ✓ 對帳一致 over a share count that is wrong
    by the ratio (trap #13).

    Asserted structurally, and NOT by re-running the validator, because measured on this
    corpus the validator does not catch it in a multi-symbol batch: E13's `covered` set is
    `{b.account_id for b in batch}` with no filter on symbol/date/kind, so the seeded
    SPINOFF row (moomoo_my / KEMB) "covers" a missing moomoo_my / ORBT split row and the
    guard goes silent. Dropping the second ORBT row and validating it ALONE still rejects
    it; validating it alongside the other two kinds does not. Reported, not fixed here.
    """
    seed_demo.seed(conn)
    actions = list_corporate_actions(conn)
    by_kind = {k: [a for a in actions if a.kind == k.value] for k in CorporateActionKind}

    assert len(by_kind[CorporateActionKind.SPLIT]) == 2
    assert len(by_kind[CorporateActionKind.EXCHANGE]) == 1
    assert len(by_kind[CorporateActionKind.SPINOFF]) == 1

    splits = by_kind[CorporateActionKind.SPLIT]
    assert {a.account_id for a in splits} == {"schwab", "moomoo_my"}, "D13: one row per holder"
    assert len({(a.from_symbol, a.date, a.ratio_to, a.ratio_from) for a in splits}) == 1
    assert all(a.from_symbol == a.to_symbol for a in splits)  # E20

    (spin,) = by_kind[CorporateActionKind.SPINOFF]
    assert spin.cost_carry is not None and Decimal("0") < spin.cost_carry < Decimal("1")

    (xchg,) = by_kind[CorporateActionKind.EXCHANGE]
    assert xchg.ratio_to == xchg.ratio_from, "§7.7 asks for a rename, i.e. ratio 1"
    assert xchg.from_symbol != xchg.to_symbol


def test_the_split_symbols_price_series_spans_the_action_date(
    conn: sqlite3.Connection,
) -> None:
    """§7.7 row 2, and the reason rows 1 and 2 are one symbol.

    Price rows must exist on BOTH sides of the action, or §5.1(b)'s stored basis, (c)'s
    reconcile and (d)'s carry-forward re-expression all have nothing to act on and the
    demo verifies a code path it never enters.
    """
    seed_demo.seed(conn)
    dates = [
        r[0] for r in conn.execute(
            "SELECT as_of_date FROM prices WHERE instrument=? ORDER BY as_of_date",
            (seed_demo._SPLIT_SYM,),
        )
    ]
    action = seed_demo._SPLIT_DATE.isoformat()
    assert any(d < action for d in dates), "no price before the split"
    assert any(d > action for d in dates), "no price after the split"


def test_the_stored_price_basis_is_as_traded_and_the_factor_is_not_squared(
    conn: sqlite3.Connection,
) -> None:
    """§5.1(a)(b): a stored close is AS TRADED, and `close == close_raw × split_basis`.

    The factor must be 3 — the ratio — and not 9. `prices` has no `account_id`, so a
    two-account 3-for-1 is two ledger rows and ONE price event; a factor formed per row
    would store 522.00 where 174.00 traded, silently, and only for multi-account holders
    (§5.1 detail 3; the same defect is a 27-for-1 at three accounts).
    """
    seed_demo.seed(conn)
    pre = _price_row(conn, seed_demo._SPLIT_SYM, "2026-05-01")   # before 2026-05-15
    post = _price_row(conn, seed_demo._SPLIT_SYM, "2026-06-01")  # after

    assert Decimal(pre["split_basis"]) == Decimal("3"), "dedup broken: factor is not the ratio"
    assert Decimal(pre["close"]) == Decimal(pre["close_raw"]) * Decimal(pre["split_basis"])
    assert Decimal(pre["close"]) == Decimal("213.00")            # the price that traded
    # A post-split row has no split dated after it: identity basis, untouched close.
    assert post["split_basis"] == "1"
    assert Decimal(post["close"]) == Decimal(post["close_raw"])


def test_the_replay_produces_the_positions_the_corpus_is_meant_to_demonstrate(
    conn: sqlite3.Connection,
) -> None:
    """The corpus is only worth seeding if the replay actually moves the share counts.

    Also pins the EXCHANGE's emptied source: §4.2 leaves it in the map with zeroed fields,
    and `build_book` drops zero-share positions from `holdings` — so the old ticker must be
    gone from the report while the new one carries the whole position.
    """
    seed_demo.seed(conn)
    from portfolio_dash.data_ingestion.store import load_ledger_bundle

    book = build_book(load_ledger_bundle(conn))
    shares = {(h.account_id, h.symbol): h.shares for h in book.holdings}

    # SPLIT 3-for-1, applied in BOTH accounts (30 -> 90, 20 -> 60)
    assert shares[("schwab", seed_demo._SPLIT_SYM)] == Decimal("90")
    assert shares[("moomoo_my", seed_demo._SPLIT_SYM)] == Decimal("60")
    # EXCHANGE 1-for-1: the whole position moved to the new ticker
    assert shares[("schwab", seed_demo._XCHG_TO_SYM)] == Decimal("40")
    assert ("schwab", seed_demo._XCHG_FROM_SYM) not in shares
    # SPINOFF 1-for-5: the parent keeps its shares, the child is created
    assert shares[("moomoo_my", seed_demo._SPIN_PARENT)] == Decimal("5000")
    assert shares[("moomoo_my", seed_demo._SPIN_CHILD)] == Decimal("1000")


def test_the_spinoff_child_has_a_payback_figure_for_D21_to_label(
    conn: sqlite3.Connection,
) -> None:
    """D21 rules that a SPINOFF child's 回本進度 renders with its provenance. That ruling
    is unreachable unless the child inherits a NON-ZERO `dividend_portion`, and it only
    does when the parent's `adjusted_total` was reduced by a cash dividend — which is why
    the seeded parent is a MY (MYR) position with one, and not a US position (a US cash
    dividend does not reduce adjusted cost until D35 lands, in another spec).

    The equality is D21's whole argument: scaling both totals by the same `c` cancels, so
    a company that has never paid a dividend renders exactly its parent's payback.
    """
    seed_demo.seed(conn)
    from portfolio_dash.data_ingestion.store import load_ledger_bundle

    book = build_book(load_ledger_bundle(conn))
    by_symbol = {h.symbol: h for h in book.holdings}
    parent, child = by_symbol[seed_demo._SPIN_PARENT], by_symbol[seed_demo._SPIN_CHILD]

    assert child.dividend_portion > 0, "nothing for D21's provenance label to qualify"
    assert Decimal("0") < child.payback_ratio < Decimal("1"), "keep 已回本 off this fixture"
    assert child.payback_ratio == parent.payback_ratio


def test_every_seeded_etf_sector_instrument_carries_the_etf_flag(
    conn: sqlite3.Connection,
) -> None:
    """M10-01 (2026-09-06): the seed registered 0056 (元大高股息) with ``sector="ETF"`` and
    no ``is_etf``, so the model default landed ``is_etf=False, etf_flag_unknown=False`` —
    "answered: not an ETF" — and a demo sell of 0056 was taxed at 現股 0.3% instead of 0.1%
    with NO disclosure (AI-D40's soft issue needs ``unknown=True``, and the seed asserted a
    wrong answer rather than no answer).

    Scope: the SEED SCRIPT only. ``shared/sectors.py`` forbids deriving ``is_etf`` from the
    sector label at runtime, and this test does not — it checks that the AUTHOR of the seed
    answered the tax question for every row whose sector label says ETF."""
    seed_demo.seed(conn)
    rows = conn.execute(
        "SELECT symbol, is_etf, etf_flag_unknown FROM instruments WHERE sector='ETF' "
        "ORDER BY symbol"
    ).fetchall()
    symbols = [r["symbol"] for r in rows]
    assert "0056" in symbols, f"the seed no longer registers 0056 as an ETF-sector row: {symbols}"
    for r in rows:
        assert (int(r["is_etf"]), int(r["etf_flag_unknown"])) == (1, 0), (
            f"{r['symbol']}: sector says ETF but is_etf={r['is_etf']} "
            f"etf_flag_unknown={r['etf_flag_unknown']} — the seed asserted a wrong answer"
        )
