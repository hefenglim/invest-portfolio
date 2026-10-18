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
"""

import json
from pathlib import Path
from typing import Any

import pytest

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
        expected = (_PAYLOADS / name / rel).read_bytes()
        if actual != expected:
            where = _json_diff(expected, actual) if rel.endswith(".json") else None
            problems.append(f"{name}/{rel}: bytes differ"
                            + (f" — first difference at {where}" if where else ""))
    assert not problems, "\n".join(problems)
