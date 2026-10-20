"""``scripts/perf/engine_equality.py`` on a corpus template (PERF plan W0.4 skeleton)."""

import json
from datetime import date
from pathlib import Path

import pytest

from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.models.ledger import LedgerBundle
from scripts.perf import engine_equality as cli
from scripts.perf._refcommon import REPO_ROOT
from tests.reference.corpora import CORPORA, copy_corpus
from tests.reference.cost_basis_v0131 import ref_build_book


def _template_copy(corpus_template_dir: Path, tmp_path: Path, name: str) -> Path:
    return copy_corpus(name, corpus_template_dir, tmp_path / "in" / f"{name}.db").path


def test_reports_every_event_day_equal(corpus_template_dir: Path, tmp_path: Path) -> None:
    db = _template_copy(corpus_template_dir, tmp_path, "demo")
    out = tmp_path / "out"
    status = cli.main(["--db", str(db), "--now", CORPORA["demo"].now.isoformat(),
                       "--out", str(out)])
    report = json.loads((out / cli.REPORT).read_text(encoding="utf-8"))
    assert status == 0
    assert report["equal"] is True
    assert report["event_days"] > 10
    live = report["engines"]["live"]
    assert live == {**live, "equal": True, "days_compared": report["event_days"],
                    "first_difference": None}


def test_names_the_first_differing_day(corpus_template_dir: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    db = _template_copy(corpus_template_dir, tmp_path, "golden")
    bad_day = date(2026, 3, 1)  # golden's cash dividend

    def drifting(bundle: LedgerBundle, days: list[date]) -> list[tuple[date, Book]]:
        out: list[tuple[date, Book]] = []
        for d in days:
            book = ref_build_book(bundle.through(d), allow_oversell=True)
            if d >= bad_day:
                book = book.model_copy(update={"gross_invested": {}})
            out.append((d, book))
        return out

    monkeypatch.setitem(cli.ENGINES, "drifting", drifting)
    out = tmp_path / "out"
    status = cli.main(["--db", str(db), "--now", CORPORA["golden"].now.isoformat(),
                       "--out", str(out), "--engine", "drifting"])
    report = json.loads((out / cli.REPORT).read_text(encoding="utf-8"))
    assert status == 1
    first = report["engines"]["drifting"]["first_difference"]
    assert first["day"] == bad_day.isoformat()
    assert "gross_invested" in first["where"]
    assert "!=" not in first["where"]  # values (amounts) only with --show-values


@pytest.mark.parametrize("flag", ["--db", "--out"])
def test_refuses_paths_inside_the_repository(flag: str, tmp_path: Path) -> None:
    outside = tmp_path / "x.db"
    outside.write_bytes(b"")
    args = {"--db": str(outside), "--out": str(tmp_path / "out")}
    args[flag] = str(REPO_ROOT / "tests" / "reference")
    with pytest.raises(SystemExit, match="inside the repository"):
        cli.main(["--db", args["--db"], "--now", "2026-06-11T14:30:00+08:00",
                  "--out", args["--out"]])
