"""§10.5's acceptance script is itself gated — a FAIL branch that has never run is a guess.

``scripts/verify_corporate_actions.py`` is the deliverable of D27 and the last gate before
``/ship-version``. It runs against git-ignored real data on the owner's machine, so nothing
in CI can ever exercise it on its real input; everything it does must therefore be proven
here, on synthetic fixtures, including the ways it says NO.

Every figure below is invented. Nothing in this file reads, derives from, or resembles the
owner's export.

What is asserted, and why each one has its own way of going quietly wrong:

1. **PASS and FAIL both run.** A script that has only ever printed PASS is a script whose
   FAIL branch is untested, and its exit code is what the owner chains on.
2. **The output leaks nothing.** Asserted as a whitelist over every printed line, not as a
   spot-check for one amount: the constraint is "amounts cannot be printed", and a test that
   greps for the amounts it happens to know about would pass on a build that printed
   different ones.
3. **賣超 is caught even when the position ends FLAT.** ``build_book`` drops any position
   whose signed quantity is exactly zero, so a position that oversold and was later bought
   back carries a discarded basis and emits no ``Holding`` — no flag to read. Proven by
   showing the replay's own flag is unreachable for that ledger and the script still reports
   it.
4. **A DECLARED short is not an oversell.** The opposite error, and the one that would make
   the acceptance run un-passable for a ledger that has one.
5. **The kind dispatcher tracks the parsers.** Each downloadable import template is fed back
   through ``_detect_kind``; a column rename in ``data_ingestion`` then fails here instead of
   silently making a whole ledger unloadable at W10.
6. **A missing path and a missing argument exit non-zero without a traceback.**
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.corporate_action_import import CORPORATE_ACTION_COLUMNS
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.import_templates import (
    TEMPLATE_KINDS,
    render_import_template,
)
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from scripts import verify_corporate_actions as script

D = Decimal

_TXN_HEADER = "account,symbol,side,date,shares,price\n"
_OPEN_HEADER = "account,symbol,shares,original_cost_total,build_date\n"
# The SAME header the app's 5th-ledger importer reads — one owner-authored file feeds both
# the app's import door and this script, which is why the script imports the constant rather
# than restating it.
_ACTION_HEADER = ",".join(CORPORATE_ACTION_COLUMNS) + "\n"

# One synthetic export exercising all three action kinds plus a multi-account SPLIT, plus one
# symbol (GGG) that has no corporate action at all — the containment case, which must stay
# OFF the report entirely.
_EXPORT_TXNS = (
    _TXN_HEADER
    + "schwab,AAA,BUY,2024-01-10,100,140\n"       # + a 7-for-1 split, then sold in full
    + "schwab,AAA,SELL,2024-09-05,700,26\n"
    + "schwab,BBB,BUY,2023-02-01,200,15\n"        # exchanged into CCC 1-for-2, then sold
    + "schwab,CCC,SELL,2024-08-01,100,40\n"
    + "schwab,DDD,BUY,2023-05-05,50,80\n"         # spins EEE off
    + "schwab,FFF,BUY,2023-07-07,30,60\n"         # 2-for-1 held in two accounts
    + "moomoo_my,FFF,BUY,2023-08-08,20,62\n"
)
_EXPORT_OPENINGS = _OPEN_HEADER + "schwab,GGG,40,3200,2022-12-30\n"

_ACTIONS_COMPLETE = (
    _ACTION_HEADER
    + "schwab,2024-06-03,SPLIT,AAA,AAA,7,1,,seven for one\n"
    + "schwab,2024-03-01,EXCHANGE,BBB,CCC,1,2,,merger into CCC\n"
    + "schwab,2024-02-02,SPINOFF,DDD,EEE,1,1,0.3,spinoff child\n"
    + "schwab,2024-04-04,SPLIT,FFF,FFF,2,1,,held in two accounts\n"
    + "moomoo_my,2024-04-04,SPLIT,FFF,FFF,2,1,,held in two accounts\n"
)
# The same list with the AAA split MISSING (so its later sell oversells) and the spinoff's
# cost_carry raised to 1 (so the parent's basis is zeroed with NO oversell — the F-13 class of
# failure, which is what keeps 成本完整 from being a restatement of 賣超).
_ACTIONS_INCOMPLETE = (
    _ACTION_HEADER
    + "schwab,2024-03-01,EXCHANGE,BBB,CCC,1,2,,merger into CCC\n"
    + "schwab,2024-02-02,SPINOFF,DDD,EEE,1,1,1,all cost to the child\n"
    + "schwab,2024-04-04,SPLIT,FFF,FFF,2,1,,held in two accounts\n"
    + "moomoo_my,2024-04-04,SPLIT,FFF,FFF,2,1,,held in two accounts\n"
)

# Every line the script is allowed to print: a ticker line, the missing-row count, or the
# verdict. Anything else is a leak until proven otherwise.
_TICKER_LINE = re.compile(r"^[A-Za-z0-9.\-]+ +· 賣超 (yes|no) +· 成本完整 (yes|no) +"
                          r"· 股數一致 (yes|no) *$")
_COUNT_LINE = re.compile(r"^缺少的公司行動筆數（下限） \d+$")
_VERDICT_LINE = re.compile(r"^(PASS|FAIL \d+)$")


class RunFn(Protocol):
    """Write an export + actions pair and run the script; return ``(exit code, stdout)``."""

    def __call__(self, actions_csv: str, txns_csv: str = ...) -> tuple[int, list[str]]: ...


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """A bare in-memory ledger. ``tests/data_ingestion`` has an identical fixture in its own
    conftest; this package needs one of its own rather than reaching across."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    bootstrap_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> RunFn:
    """Write an export + actions pair to disk and run the script's ``main``."""

    def _run(actions_csv: str, txns_csv: str = _EXPORT_TXNS) -> tuple[int, list[str]]:
        export = tmp_path / "export"
        export.mkdir(exist_ok=True)
        (export / "transactions.csv").write_text(txns_csv, encoding="utf-8")
        (export / "openings.csv").write_text(_EXPORT_OPENINGS, encoding="utf-8")
        actions = tmp_path / "actions.csv"
        actions.write_text(actions_csv, encoding="utf-8")
        code = script.main(["--export", str(export), "--actions", str(actions)])
        return code, capsys.readouterr().out.splitlines()

    return _run


def test_complete_actions_pass(run: RunFn) -> None:
    """The ledger the feature exists to accept: every affected ticker clean, exit 0."""
    code, lines = run(_ACTIONS_COMPLETE)
    assert code == 0
    assert lines[-1] == "PASS"
    assert lines[-2] == "缺少的公司行動筆數（下限） 0"
    reported = [line.split(" ")[0] for line in lines if _TICKER_LINE.match(line)]
    assert reported == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    # D38 invariant 1, from the outside: a symbol with no corporate action is not "affected"
    # and never reaches the report.
    assert "GGG" not in "\n".join(lines)
    assert all(" no  · 成本完整 yes · 股數一致 yes" in line
               for line in lines if _TICKER_LINE.match(line))


def test_missing_action_fails_and_names_the_ticker(run: RunFn) -> None:
    """The FAIL branch: a missing SPLIT trips 賣超 and discards the basis; exit 1."""
    code, lines = run(_ACTIONS_INCOMPLETE)
    assert code == 1
    assert lines[-1] == "FAIL 3"          # 2 failing tickers + 1 missing row
    by_symbol = {line.split(" ")[0]: line for line in lines if _TICKER_LINE.match(line)}
    # AAA: the sell is a post-split quantity against a pre-split count.
    assert "賣超 yes" in by_symbol["AAA"] and "成本完整 no" in by_symbol["AAA"]
    # DDD: cost_carry 1 moves the whole basis to the child. NO oversell, basis gone — the
    # second column earning its place as an independent check (§10.5's F-13 warning).
    assert "賣超 no" in by_symbol["DDD"] and "成本完整 no" in by_symbol["DDD"]
    # …and the count says WHICH kind of failure this is: rows are missing, not miscomputed.
    assert lines[-2] == "缺少的公司行動筆數（下限） 1"


def test_a_partial_multi_account_action_cannot_print_pass(run: RunFn) -> None:
    """D13's silent case: the FFF split supplied for ONE of the two accounts that hold it.

    Nothing on the three per-ticker checks can see it — the un-actioned account keeps its
    pre-action count in BOTH share paths, so they agree, and no later sell exposes it. E13
    rejects the supplied row, the ledger ends up with no FFF action at all, and every ticker
    reports clean. This is exactly the state that must not print PASS, which is why ``n``
    counts findings rather than tickers.
    """
    partial = (
        _ACTION_HEADER
        + "schwab,2024-06-03,SPLIT,AAA,AAA,7,1,,seven for one\n"
        + "schwab,2024-03-01,EXCHANGE,BBB,CCC,1,2,,merger into CCC\n"
        + "schwab,2024-02-02,SPINOFF,DDD,EEE,1,1,0.3,spinoff child\n"
        + "schwab,2024-04-04,SPLIT,FFF,FFF,2,1,,only one of the two holders\n"
    )
    code, lines = run(partial)
    by_symbol = {line.split(" ")[0]: line for line in lines if _TICKER_LINE.match(line)}
    assert "賣超 no" in by_symbol["FFF"] and "股數一致 yes" in by_symbol["FFF"]
    assert lines[-2] == "缺少的公司行動筆數（下限） 1"
    assert lines[-1] == "FAIL 1"
    assert code == 1


def test_a_rejected_row_counts_as_missing(run: RunFn) -> None:
    """A pre-divided decimal ratio (trap #1) is rejected, so the ledger never gets the row.

    The file names it, the ledger does not have it, and to every downstream number that is
    indistinguishable from never having been supplied — which is what "needed but not found"
    means. It also proves the count separates the two next actions: 賣超 on AAA AND a missing
    row, so the owner fixes the CSV rather than escalating a replay bug.
    """
    bad_ratio = (
        _ACTION_HEADER
        + "schwab,2024-06-03,SPLIT,AAA,AAA,0.2857,1,,a rounded quotient\n"
    )
    code, lines = run(bad_ratio)
    by_symbol = {line.split(" ")[0]: line for line in lines if _TICKER_LINE.match(line)}
    assert "賣超 yes" in by_symbol["AAA"]
    # 1 rejected row + 2 positions (AAA, CCC) left 賣超 with no action row of their own —
    # this actions file omits the BBB->CCC exchange entirely, so CCC's sell has nothing to
    # cover it either. All three populations of the count, in one run.
    assert lines[-2] == "缺少的公司行動筆數（下限） 3"
    assert code == 1


def test_output_never_leaks_an_amount(run: RunFn) -> None:
    """Whitelist every printed line. The FAIL path is checked too — that is where a
    well-meaning 'show me the numbers so I can debug' edit would land."""
    for actions in (_ACTIONS_COMPLETE, _ACTIONS_INCOMPLETE):
        _, lines = run(actions)
        for line in lines:
            assert (_TICKER_LINE.match(line) or _COUNT_LINE.match(line)
                    or _VERDICT_LINE.match(line)), f"unrecognised output line: {line!r}"
        blob = "\n".join(lines)
        # Belt as well as braces: no price, share count or account id from the fixture.
        for secret in ("140", "700", "3200", "schwab", "moomoo_my", "26", "62"):
            assert secret not in blob


def test_oversell_is_reported_even_when_the_position_ends_flat(run: RunFn) -> None:
    """賣超 on a position bought back to EXACTLY zero — invisible to ``Holding.oversold``.

    ``build_book`` drops a position whose signed quantity is 0, so the sticky ``ever_oversold``
    marker is discarded with its carrier and there is no flag left to read. The basis was
    still zeroed. Reading the holdings alone would print a clean line over a discarded cost
    basis, which is the exact failure mode §10.5 commissions this script to catch.
    """
    txns = (
        _TXN_HEADER
        + "schwab,HHH,BUY,2024-01-02,100,10\n"
        + "schwab,HHH,SELL,2024-02-02,700,12\n"    # oversells to -600
        + "schwab,HHH,BUY,2024-03-02,600,11\n"     # back to exactly flat
    )
    code, lines = run(_ACTIONS_COMPLETE, txns)
    by_symbol = {line.split(" ")[0]: line for line in lines if _TICKER_LINE.match(line)}
    assert "賣超 yes" in by_symbol["HHH"]
    assert code == 1


def test_the_flat_oversell_really_is_invisible_to_the_replay_flag(
    conn: sqlite3.Connection,
) -> None:
    """The pre-condition of the test above, asserted directly so it cannot rot.

    If ``build_book`` ever starts emitting a flagged zero-share holding, the probe becomes
    redundant — and this test says so loudly rather than leaving a second mechanism nobody
    remembers the reason for.
    """
    _seed_account_and_symbol(conn, "HHH")
    for side, qty, price, day in (
        (Side.BUY, "100", "10", date(2024, 1, 2)),
        (Side.SELL, "700", "12", date(2024, 2, 2)),
        (Side.BUY, "600", "11", date(2024, 3, 2)),
    ):
        insert_transaction(conn, account_id="schwab", symbol="HHH", side=side,
                           quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                           trade_date=day)
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert [h for h in book.holdings if h.symbol == "HHH"] == []   # no holding, so no flag
    assert script._ever_negative(conn, load_action_index(conn)) == {("schwab", "HHH")}


def test_a_declared_short_is_not_an_oversell(conn: sqlite3.Connection) -> None:
    """The opposite error. ``short_sale`` drives the signed count negative legitimately.

    Note for the record: the transaction CSV has no ``short_sale`` column, so this state
    cannot arrive through ``--export`` at all today — the row is written through the store
    directly. That gap is reported with the script, not worked around inside it.
    """
    _seed_account_and_symbol(conn, "III")
    insert_transaction(conn, account_id="schwab", symbol="III", side=Side.SELL,
                       quantity=D("50"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 5, 5), short_sale=True)
    assert script._ever_negative(conn, load_action_index(conn)) == set()


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_detect_kind_tracks_the_downloadable_templates(kind: str) -> None:
    """A column rename in ``data_ingestion`` must fail HERE, not at W10 on real data.

    Parametrized over the app's OWN kind list rather than the script's, so a 5th template —
    which is exactly what corporate actions became — is a visible event instead of a silent
    hole. The corporate-action template is the ``--actions`` argument, not a ledger, so it
    must NOT be dispatched as one; it is recognised separately so the run can name the right
    argument (asserted below).
    """
    header = script._header_of(render_import_template(kind))
    if kind == "corporate_actions":
        assert script._detect_kind(header) is None
        assert script._is_action_header(header)
    else:
        assert script._detect_kind(header) == kind
        assert not script._is_action_header(header)


def test_the_action_file_in_the_export_folder_names_the_right_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dropping the actions CSV into ``--export`` says where it belongs, and writes nothing.

    Loading it as a ledger is not an option and neither is shrugging: the corporate-action
    columns are unmistakable, and the one thing that must never happen is the ratios being
    applied without ``validate_corporate_action`` ever seeing them.
    """
    export = tmp_path / "export"
    export.mkdir()
    (export / "actions.csv").write_text(_ACTIONS_COMPLETE, encoding="utf-8")
    actions = tmp_path / "actions.csv"
    actions.write_text(_ACTION_HEADER, encoding="utf-8")
    assert script.main(["--export", str(export), "--actions", str(actions)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--actions" in captured.err


def test_unknown_header_is_a_blocking_problem(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """An unrecognised CSV aborts the run (exit 2) instead of reporting on a partial ledger."""
    export = tmp_path / "export"
    export.mkdir()
    (export / "mystery.csv").write_text("alpha,beta\n1,2\n", encoding="utf-8")
    actions = tmp_path / "actions.csv"
    actions.write_text(_ACTION_HEADER, encoding="utf-8")
    assert script.main(["--export", str(export), "--actions", str(actions)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""            # nothing pasteable was produced
    assert "mystery.csv" in captured.err


def test_missing_path_exits_two(tmp_path: Path) -> None:
    actions = tmp_path / "actions.csv"
    actions.write_text(_ACTION_HEADER, encoding="utf-8")
    assert script.main(["--export", str(tmp_path / "nope"), "--actions", str(actions)]) == 2


@pytest.mark.parametrize("argv", [[], ["--export", "x"], ["--actions", "y"]])
def test_both_paths_are_required_with_no_default(argv: list[str]) -> None:
    """D27: no defaults, so the script cannot read a private location by accident."""
    with pytest.raises(SystemExit) as exit_info:
        script.main(argv)
    assert exit_info.value.code == 2


def _seed_account_and_symbol(conn: sqlite3.Connection, symbol: str) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                       quote_ccy=Currency.USD, sector="", name=symbol))
