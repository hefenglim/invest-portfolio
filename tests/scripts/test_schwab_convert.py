"""The offline converter: it produces importable CSVs, or it produces nothing.

Two properties carry the weight here, and neither is about the happy path:

* **the output re-parses through the REAL preview builders** — the same guard
  ``tests/contract/test_import_template.py`` puts on the downloadable templates. A converter
  that emits a column the importer does not accept is a converter nobody can use, and the
  failure would otherwise surface for the first time on the owner's own ledger.
* **stdout stays pasteable.** The privacy argument in the script's docstring is only true
  while nothing prints an amount, and that is a property a test can hold rather than a habit
  a reader has to maintain.

Everything runs against the committed synthetic corpus (``tests/golden/broker/``). The real
export the rules were derived from is the owner's financial history and is git-ignored.
"""

import csv
import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from portfolio_dash.api.routers.input_center import _BUILDERS
from portfolio_dash.data_ingestion.csv_import import normalize_import_csv
from portfolio_dash.data_ingestion.import_templates import (
    DATE_COLUMN_BY_KIND,
    template_columns,
)
from portfolio_dash.data_ingestion.preview import ImportPreview
from scripts import schwab_convert as script
from scripts.privacy import safe_symbol

_CORPUS = Path(__file__).resolve().parents[1] / "golden" / "broker"

#: What each generated file must re-parse as. The two worksheets are deliberately absent —
#: they carry blank required fields BY DESIGN and are supposed to be rejected until filled.
_IMPORTABLE: dict[str, str] = {
    "import_transactions.csv": "transactions",
    "import_dividends.csv": "dividends",
    "import_cash.csv": "cash",
    "import_fx.csv": "fx",
    "import_corporate_actions.csv": "corporate_actions",
}


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """The two well-formed corpus files. ``schwab_unmapped.csv`` is left out — it exists to
    prove rule 7 fires, and including it here would make every test in this file fail at the
    door instead of testing what it is named after."""
    src = tmp_path / "in"
    src.mkdir()
    for name in ("schwab_2024.csv", "schwab_2025.csv"):
        (src / name).write_bytes((_CORPUS / name).read_bytes())
    return src


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = script.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _convert(corpus_dir: Path, out: Path, capsys: pytest.CaptureFixture[str]) -> tuple[
    int, str, str
]:
    return _run(
        ["--export", str(corpus_dir), "--out", str(out), "--account", "schwab"], capsys
    )


# ============================================================ the happy path


def test_a_clean_export_writes_every_file_and_passes(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code, stdout, stderr = _convert(corpus_dir, out, capsys)
    assert code == 0, stderr
    assert "PASS" in stdout
    for name in [*_IMPORTABLE, "corporate_actions_TO_COMPLETE.csv",
                 "openings_TO_COMPLETE.csv", "conversion_report.txt"]:
        assert (out / name).exists(), name


def test_the_same_input_converts_byte_identically(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-running the converter must not produce a diff — the owner will run it more than
    once, and a moving output makes 'what changed?' unanswerable."""
    first, second = tmp_path / "a", tmp_path / "b"
    _convert(corpus_dir, first, capsys)
    _convert(corpus_dir, second, capsys)
    for name in _IMPORTABLE:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


# ============================================================ the output must be importable


def _built(kind: str, conn: sqlite3.Connection, text: str) -> ImportPreview:
    """Exactly the runtime path: canonicalize at the import seam, then the PRODUCTION builder
    from ``input_center._BUILDERS`` — not a copy kept in the test."""
    norm = normalize_import_csv(text, DATE_COLUMN_BY_KIND[kind])
    return _BUILDERS[kind](conn, norm.text)


@pytest.fixture
def converted(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Iterator[Path]:
    out = tmp_path / "out"
    code, _, stderr = _convert(corpus_dir, out, capsys)
    assert code == 0, stderr
    yield out


@pytest.mark.parametrize(("filename", "kind"), sorted(_IMPORTABLE.items()))
def test_every_generated_csv_reparses_through_the_real_builder(
    converted: Path, golden_db: sqlite3.Connection, filename: str, kind: str
) -> None:
    text = (converted / filename).read_text(encoding="utf-8")
    preview = _built(kind, golden_db, text)
    fatal = [
        row for row in preview.rows
        if any(i.kind in {"parse_error", "unknown_account"} for i in row.issues)
    ]
    assert fatal == [], [
        (r.index, [i.kind for i in r.issues]) for r in fatal
    ]


@pytest.mark.parametrize(("filename", "kind"), sorted(_IMPORTABLE.items()))
def test_every_generated_csv_has_the_parsers_own_header(
    converted: Path, filename: str, kind: str
) -> None:
    with (converted / filename).open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header == template_columns(kind)


def test_the_worksheets_carry_the_action_and_opening_headers(converted: Path) -> None:
    for name, kind in (("corporate_actions_TO_COMPLETE.csv", "corporate_actions"),
                       ("openings_TO_COMPLETE.csv", "openings")):
        with (converted / name).open(encoding="utf-8", newline="") as handle:
            assert next(csv.reader(handle)) == template_columns(kind)


# ============================================================ what the conversion says


def test_a_reinvestment_is_written_with_the_derived_share_count(converted: Path) -> None:
    rows = list(csv.DictReader((converted / "import_dividends.csv").open(encoding="utf-8")))
    drip = next(r for r in rows if r["type"] == "DRIP")
    assert drip["reinvest_shares"] not in ("", "0")
    assert "." in drip["reinvest_shares"]          # a derived count, not a whole number
    assert drip["net"] != drip["gross"]            # withholding was subtracted, not dropped


def test_the_three_new_cash_kinds_survive_the_round_trip(converted: Path) -> None:
    rows = list(csv.DictReader((converted / "import_cash.csv").open(encoding="utf-8")))
    assert {"INTEREST", "INTEREST_EXPENSE", "BROKER_FEE"} <= {r["kind"] for r in rows}
    # The ledger takes a magnitude plus a kind; a signed amount would double the direction.
    assert all(not r["amount"].startswith("-") for r in rows)


def test_a_row_seen_in_two_files_is_written_and_marked(converted: Path) -> None:
    """Written, not dropped — two deposits of the same amount on one day is a thing that
    happens. Marked, because the note column is in front of whoever opens the CSV."""
    rows = list(csv.DictReader((converted / "import_cash.csv").open(encoding="utf-8")))
    marked = [r for r in rows if script._DUPLICATE_MARK in r["note"]]
    assert len(marked) == 2
    assert marked[0]["amount"] == marked[1]["amount"]


def test_an_opening_position_gets_a_blank_cost_and_a_date_before_the_ledger(
    converted: Path,
) -> None:
    rows = list(csv.DictReader((converted / "openings_TO_COMPLETE.csv").open(encoding="utf-8")))
    assert rows, "the corpus contains a position older than its window"
    assert all(r["original_cost_total"] == "" for r in rows)
    trades = list(csv.DictReader((converted / "import_transactions.csv").open(encoding="utf-8")))
    assert all(r["build_date"] < min(t["date"] for t in trades) for r in rows)


def test_a_split_the_file_does_not_determine_goes_to_the_worksheet_blank(
    converted: Path,
) -> None:
    rows = list(
        csv.DictReader((converted / "corporate_actions_TO_COMPLETE.csv").open(encoding="utf-8"))
    )
    assert rows
    assert all(r["ratio_to"] == "" and r["ratio_from"] == "" for r in rows)
    assert all(r["note"] for r in rows), "a blank field must say what it is waiting for"


def test_a_derivable_action_is_written_ready_to_import(converted: Path) -> None:
    rows = list(
        csv.DictReader((converted / "import_corporate_actions.csv").open(encoding="utf-8"))
    )
    assert rows
    for r in rows:
        assert r["ratio_to"].isdigit() and r["ratio_from"].isdigit()
        assert r["from_symbol"] and r["to_symbol"]


# ============================================================ refusing to write


def test_a_blocking_issue_writes_no_csv_at_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All or nothing. A partial conversion turns one bad row into an afternoon of
    reconciling a ledger against a statement."""
    src = tmp_path / "in"
    src.mkdir()
    (src / "free.csv").write_text(
        "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\r\n"
        '01/05/2026,Buy,AAA,"WHEN ISSUED",50,$170.00,,\r\n',
        encoding="utf-8", newline="",
    )
    out = tmp_path / "out"
    code, stdout, _ = _run(
        ["--export", str(src), "--out", str(out), "--account", "schwab"], capsys
    )
    assert code == 1
    assert "FAIL" in stdout
    assert (out / "conversion_report.txt").exists()
    assert not any((out / name).exists() for name in _IMPORTABLE)


def test_an_unmapped_row_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 7. A catch-all default would have booked it as something and moved on."""
    src = tmp_path / "in"
    src.mkdir()
    (src / "u.csv").write_bytes((_CORPUS / "schwab_unmapped.csv").read_bytes())
    code, _, stderr = _run(
        ["--export", str(src), "--out", str(tmp_path / "out"), "--account", "schwab"], capsys
    )
    assert code == 2
    assert "unmapped broker row" in stderr


def test_a_missing_export_path_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, stdout, stderr = _run(
        ["--export", str(tmp_path / "nope"), "--out", str(tmp_path / "o"),
         "--account", "schwab"],
        capsys,
    )
    assert code == 2
    assert stdout == ""
    assert stderr.strip()


def test_a_malformed_alias_exits_two(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, stderr = _run(
        ["--export", str(corpus_dir), "--out", str(tmp_path / "o"), "--account", "schwab",
         "--alias", "11111A111"],
        capsys,
    )
    assert code == 2
    assert "CUSIP=TICKER" in stderr


def test_neither_path_argument_has_a_default() -> None:
    """A defaulted path is a path that can be read by accident, and what this runs against is
    the owner's real financial history (D27)."""
    parser = script.build_parser()
    for name in ("--export", "--out", "--account"):
        action = next(a for a in parser._actions if name in a.option_strings)
        assert action.required is True
        assert action.default is None


# ============================================================ stdout must stay pasteable


_MONEY = re.compile(r"\d+\.\d+|\$")


def test_stdout_prints_no_amount(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The privacy claim, held by a test rather than by a habit. Integer counts and ISO dates
    are fine; anything with a decimal point or a currency sign is an amount."""
    _, stdout, _ = _convert(corpus_dir, tmp_path / "out", capsys)
    offenders = [line for line in stdout.splitlines() if _MONEY.search(line)]
    assert offenders == []


def test_stdout_prints_no_file_name(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broker's export filename embeds a fragment of the account number, which is why file
    names live on stderr and in the report."""
    _, stdout, _ = _convert(corpus_dir, tmp_path / "out", capsys)
    assert ".csv" not in stdout


def test_an_option_shaped_symbol_is_masked_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol is a free string and the broker fills it with a strike price. The masking is
    shared with ``verify_corporate_actions`` for exactly this reason."""
    assert safe_symbol("TSLA 01/19/2024 200.00 P") == "TSLA ⋯"
    assert safe_symbol("BRK.B") == "BRK.B"


def test_the_detailed_report_keeps_the_amounts_out_of_stdout(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The split is the design: the summary can be pasted, the detail stays on disk beside
    the CSVs, which already hold the whole ledger anyway."""
    out = tmp_path / "out"
    _, stdout, _ = _convert(corpus_dir, out, capsys)
    report = (out / "conversion_report.txt").read_text(encoding="utf-8")
    assert _MONEY.search(report), "the report is the place detail belongs"
    assert not _MONEY.search(stdout)


def test_the_script_runs_as_a_program(corpus_dir: Path, tmp_path: Path) -> None:
    """Imported-and-called is not the owner's path. This is."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "schwab_convert.py"),
         "--export", str(corpus_dir), "--out", str(tmp_path / "out"),
         "--account", "schwab"],
        capture_output=True, text=True, encoding="utf-8", cwd=root, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


# ============================================================ the output-side accounting


def test_a_builder_that_drops_a_row_is_caught_and_nothing_is_written(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second seam. ``reconcile`` proves nothing is lost between the FILE and the grouped
    events; this proves nothing is lost between those events and the CSVs. Simulated the way it
    would really happen — someone adds a condition to a row builder — because the guard is only
    worth its runtime if it has been watched to fire."""
    real = script._transaction_rows
    monkeypatch.setattr(
        script, "_transaction_rows",
        lambda grouped, account, suspect: real(grouped, account, suspect)[:-1],
    )
    out = tmp_path / "out"
    code, stdout, stderr = _run(
        ["--export", str(corpus_dir), "--out", str(out), "--account", "schwab"], capsys
    )
    assert code == 1
    assert "the builder dropped some" in stderr
    assert not (out / "import_transactions.csv").exists()


def test_a_routed_row_that_reaches_no_output_is_caught(
    corpus_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cash kind with no mapping and no entry in ``_UNCONVERTIBLE`` would otherwise leave the
    ledger short by its amount, with every count in the report still looking plausible. Three
    real rows did exactly this on the first run against a real export."""
    real = script._cash_rows
    monkeypatch.setattr(
        script, "_cash_rows",
        lambda grouped, account, currency, suspect: (
            real(grouped, account, currency, suspect)[0][:-1], []
        ),
    )
    out = tmp_path / "out"
    code, _, stderr = _run(
        ["--export", str(corpus_dir), "--out", str(out), "--account", "schwab"], capsys
    )
    assert code == 1
    assert "reached no output and were not named" in stderr
