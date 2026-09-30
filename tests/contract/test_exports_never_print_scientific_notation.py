"""No export prints a number in scientific notation.

Owner ruling 2026-09-30 on the verifier's R12 note: the llm-usage CSV passed ``llm_usage.cost``
through as stored, and the writer stored ``str(cost)`` — so a sub-micro-dollar call (a 1-token
estimate) reached the file as ``5E-7``, which a spreadsheet reads as text or as a float. Root
cause (d2e5e08): ``shared/llm.py:260`` wrote ``str(cost)`` and ``export/usage.py:41`` wrote
``str(r["cost"])``. The same shape sat in two more cost writers (``insights.cost_usd`` via
``llm_insight/insights_store.py:340``, ``organized_news.cost_usd`` via ``news/store.py:220``)
and in the tax package's three CSV sheets (``export/tax.py`` — every amount through ``str()``,
where an exact zero from an average is ``0E-24``). ``export/holdings.py`` had been fixed the
same way by M8-01; nothing generalised it.

Two guards: the behavioural one builds every export the app offers (the DEF-045 list) over a
ledger holding legacy scientific values and reads every CSV cell; the structural one lists
every bare ``str()`` in the export package, because a value that is not tiny today can be
tomorrow.
"""

from __future__ import annotations

import ast
import csv
import io
import re
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import portfolio_dash
from portfolio_dash.shared import llm as llm_mod
from tests.contract.test_def045_exports_never_print_account_labels import (
    _files,
    rich_client,  # noqa: F401 — the fixture, re-exported for this module's tests
)

_EXPORT_PKG = Path(portfolio_dash.__file__).resolve().parent / "export"
_SCI = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+$")


@pytest.fixture
def sci_client(rich_client: TestClient, golden_db: sqlite3.Connection  # noqa: F811
               ) -> Iterator[TestClient]:
    """rich_client plus rows whose stored text is what str() used to write."""
    golden_db.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2026-06-10T09:00:00+08:00','m','insight_generate',1,1,'5E-7'), "
        "('2026-06-10T09:00:01+08:00','m','insight_generate',0,0,'0E-7')")
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, cost_usd) "
        "VALUES ('news_daily','2026-06-10T09:00:00+08:00','2026-06-10T09:00:05+08:00',"
        "'ok','x','9E-8')")
    golden_db.commit()
    yield rich_client


def test_the_scientific_values_really_are_in_the_fixture(golden_db: sqlite3.Connection,
                                                          sci_client: TestClient) -> None:
    stored = {r["cost"] for r in golden_db.execute("SELECT cost FROM llm_usage")}
    assert {"5E-7", "0E-7"} <= stored  # the detector below has something to find


def test_no_csv_cell_is_in_scientific_notation(sci_client: TestClient) -> None:
    offenders: list[str] = []
    for label, ctype, text in _files(sci_client):
        if "csv" not in ctype:
            continue
        for row in csv.reader(io.StringIO(text)):
            offenders += [f"{label}: {cell}" for cell in row if _SCI.match(cell.strip())]
    assert offenders == []


def test_a_new_cost_is_stored_in_the_canonical_form(golden_db: sqlite3.Connection) -> None:
    """The writer side: a 1-token call at $0.10 / M is $0.0000001 — stored as such."""
    uid = llm_mod.log_usage(golden_db, model="m", agent="x", input_tokens=1, output_tokens=0,
                            cost=Decimal("1") * Decimal("0.10") / Decimal("1000000"))
    [cost] = golden_db.execute("SELECT cost FROM llm_usage WHERE id = ?", (uid,)).fetchone()
    assert cost == "0.0000001"  # str() stored this as "1E-7"


#: Every bare ``str()`` the export package may call, by (file, function) — each with why it
#: cannot print an exponent. Anything else must go through ``decimal_str`` /
#: ``stored_decimal_str`` (or a module's ``_s``, which does).
_ALLOWED_STR = {
    # the non-Decimal branch of the module's cell helper (Decimal handled just above it)
    ("ai_predictions.py", "_s"): 1,
    ("holdings.py", "_s"): 1,
    # raw ledger rows as stored: every ledger writer stores TEXT through money.to_db
    ("ledgers.py", "_read_table"): 1,
    # the print reports' HTML escape: it receives text (names, dates, labels); their numbers
    # go through report_html's _fmt_* display formatters first
    ("report_html.py", "_esc"): 1,
    # timestamps, model / agent / job ids, token and call counts — no Decimal among them
    ("usage.py", "build_llm_usage_csv"): 6,
    ("usage.py", "build_job_runs_csv"): 8,
}


def _bare_str_calls() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_EXPORT_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "str"):
                    key = (path.name, fn.name)
                    found[key] = found.get(key, 0) + 1
    return found


def test_every_bare_str_in_the_export_package_is_listed() -> None:
    assert _bare_str_calls() == _ALLOWED_STR


@pytest.mark.parametrize("value, shown", [
    (Decimal("0E-8"), "0.00000000"), (Decimal("5E-7"), "0.0000005"), (Decimal("1E+2"), "100"),
    (Decimal("-0.0601"), "-0.0601"), (True, None), (None, ""),
])
def test_the_cell_helpers_never_print_an_exponent(value: object, shown: str | None) -> None:
    """Both modules' ``_s``: a Decimal the canonical way; the rest as before (holdings keeps
    ``True``, the predictions export ``true`` — their own long-standing conventions)."""
    from portfolio_dash.export import ai_predictions, holdings

    if shown is None:
        assert (ai_predictions._s(value), holdings._s(value)) == ("true", "True")
        return
    assert ai_predictions._s(value) == holdings._s(value) == shown
