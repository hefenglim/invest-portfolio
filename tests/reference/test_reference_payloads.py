"""The current code reproduces v0.1.31's reference payloads byte for byte (PERF plan W0.2 (a)).

``tests/reference/payloads/`` was written by v0.1.31's OWN code (commit 473fe5a) through
``scripts/perf/make_reference_payloads.py`` — never by the code under test, so a reference
cannot drift with the change it is meant to catch. For every corpus this renders the same
files with the current code (``payload_gen.render_corpus``: the same requests, the same clock,
the same reporting currency) and compares bytes: ``dashboard.json`` (the whole
``GET /api/dashboard`` body), the tax package's members, and every month end's truncated-world
period subset. The full month-end sweep runs by default (~10 s for all five corpora); see
``payloads/README.md``.

A corpus fingerprint is checked FIRST, so a seed edited since the payloads were generated
fails as "the corpus changed", not as a money difference. The only intended payload
differences of the PERF program are listed in the plan's invariant 1; anything else here is a
regression.

**One intended difference since: PERF-F2** (``tests/reference/perf_f2.py``). On
``unquoted_ccy_cash`` the fixed trend loads the rebate's currency, so its ``dashboard.json`` and
its month ends from the rebate's month on differ from v0.1.31 on exactly the listed paths, each
carrying the value derived from ``golden``'s own v0.1.31 file; every other byte of those files,
and every other file, is v0.1.31's.

**One more since: the trend points' ``pnl``** (owner ruling 2026-10-22,
``tests/reference/trend_pnl.py``). Every served trend point gains one key, appended after
``net_worth``: its own ``total_value − net_invested`` at the reporting currency's minor unit.
It composes with PERF-F2 — ``trend_pnl.expected_bytes`` starts from ``perf_f2.expected_bytes``
— and changes nothing else.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.reference import perf_f2, trend_pnl
from tests.reference.canon import first_difference
from tests.reference.corpora import CORPUS_NAMES, ensure_template, fingerprint, open_db
from tests.reference.payload_gen import render_corpus

_PAYLOADS = Path(__file__).resolve().parent / "payloads"


def _manifest() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((_PAYLOADS / "manifest.json").read_bytes())
    return doc


def _json_diff(expected: bytes, actual: bytes) -> str | None:
    """Where two JSON documents first differ, as a path (``None`` if they parse equal)."""
    def form(v: Any) -> Any:
        if isinstance(v, dict):
            return ("M", tuple((k, form(x)) for k, x in v.items()))
        if isinstance(v, list):
            return ("L", tuple(form(x) for x in v))
        return v
    try:
        return first_difference(form(json.loads(expected)), form(json.loads(actual)))
    except ValueError:
        return None


def test_manifest_lists_every_corpus() -> None:
    manifest = _manifest()
    assert manifest["source"].startswith("473fe5a"), manifest["source"]
    assert list(manifest["corpora"]) == list(CORPUS_NAMES)


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_current_code_reproduces_the_v0131_payloads(
    name: str, corpus_template_dir: Path, tmp_path: Path, sockets_enabled: None,
) -> None:
    entry = _manifest()["corpora"][name]
    conn = open_db(ensure_template(name, corpus_template_dir))
    try:
        digest = fingerprint(conn)
    finally:
        conn.close()
    assert digest == entry["fingerprint"], (
        f"corpus {name!r} no longer seeds the rows the reference payloads were generated "
        f"from (a seed in tests/conftest.py, scripts/seed_demo.py or tests/reference/corpora.py "
        f"changed). Regenerate with scripts/perf/make_reference_payloads.py — never by hand.")

    files = render_corpus(name, corpus_template_dir, tmp_path / "work")
    assert sorted(files) == sorted(entry["files"]), "the set of reference files changed"
    problems: list[str] = []
    for rel, actual in files.items():
        expected = trend_pnl.expected_bytes(name, rel)
        if actual != expected:
            where = _json_diff(expected, actual) if rel.endswith(".json") else None
            problems.append(f"{name}/{rel}: bytes differ"
                            + (f" — first difference at {where}" if where else ""))
    assert not problems, "\n".join(problems)


def test_perf_f2_changes_exactly_the_listed_paths() -> None:
    """The intended difference is exactly :data:`perf_f2.DASHBOARD_CHANGES` /
    :data:`perf_f2.MONTH_END_CHANGES` on exactly the affected files: every listed path really
    differs from v0.1.31, and no other path does (the test above then pins the values)."""
    files = _manifest()["corpora"][perf_f2.CORPUS]["files"]
    affected = [rel for rel in files if perf_f2.is_affected(perf_f2.CORPUS, rel)]
    assert sorted(affected) == ["dashboard.json", "month_end/2026-03.json",
                                "month_end/2026-04.json", "month_end/2026-05.json"]
    for rel in affected:
        old = json.loads(perf_f2.v0131_bytes(perf_f2.CORPUS, rel))
        new = json.loads(perf_f2.expected_bytes(perf_f2.CORPUS, rel))
        assert perf_f2.changed_paths(old, new) == list(perf_f2.changes(rel)), rel
    for name in CORPUS_NAMES:
        for rel in _manifest()["corpora"][name]["files"]:
            if not perf_f2.is_affected(name, rel):
                assert perf_f2.expected_bytes(name, rel) == (
                    _PAYLOADS / name / rel).read_bytes(), f"{name}/{rel}"


def test_trend_pnl_adds_exactly_one_key_to_every_point() -> None:
    """The ``pnl`` difference is additive and nothing else: against PERF-F2's expectation, a
    file with trend points differs only at ``trend.points``, where every point gains ``pnl`` as
    its LAST key and keeps every other key and value; a file without them (the tax package) is
    PERF-F2's byte for byte. The test above then pins every value against what is served."""
    seen = 0
    for name in CORPUS_NAMES:
        for rel in _manifest()["corpora"][name]["files"]:
            before = perf_f2.expected_bytes(name, rel)
            after = trend_pnl.expected_bytes(name, rel)
            if not trend_pnl.carries_points(rel):
                assert after == before, f"{name}/{rel}"
                continue
            old, new = json.loads(before), json.loads(after)
            points = old["trend"]["points"]
            assert perf_f2.changed_paths(old, new) == (["trend.points"] if points else []), \
                f"{name}/{rel}"
            for p, q in zip(points, new["trend"]["points"], strict=True):
                assert list(q) == [*p, trend_pnl.KEY], f"{name}/{rel} {p['date']}"
                assert {k: v for k, v in q.items() if k != trend_pnl.KEY} == p
                seen += 1
    assert seen > 2000, seen   # every corpus's whole series and every month end's point D


@pytest.mark.parametrize(("total", "invested", "ccy", "expected"), [
    ("1234567.8", "1000000.3", "TWD", "234568"),
    ("100", "100.5", "TWD", "-1"),
    ("100", "100.3", "TWD", "0"),
    ("100", "100.003", "USD", "0.00"),
    ("1000.005", "0", "USD", "1000.01"),
    ("4082.123", "4000", "MYR", "82.12"),
])
def test_the_transcribed_rounding_rule(total: str, invested: str, ccy: str,
                                       expected: str) -> None:
    """The oracle's own rule, pinned: ROUND_HALF_UP at the minor unit, no ``-0``."""
    assert trend_pnl.point_pnl(total, invested, ccy) == expected
