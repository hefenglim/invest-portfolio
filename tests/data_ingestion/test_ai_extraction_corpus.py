"""Rot guard for the W4 AI-extraction corpus (AI-D20) — deterministic, no LLM involved.

The corpus (``tests/golden/ai_extraction/cases.json``) is the ground truth the live runner
(``scripts/ai_extraction_eval.py``) measures prompt quality against. It cannot drift,
because prompt tuning against a rotten corpus is the blind editing it exists to prevent —
so this test pins its STRUCTURE and its COVERAGE FLOORS. What it deliberately does NOT do
is call any model: the accuracy run is live, manual, and costs tokens (pytest-socket bans
the network here anyway).
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.data_ingestion.agents import _TXN_CSV_COLUMNS
from portfolio_dash.data_ingestion.cash_import import CASH_MOVEMENT_COLUMNS
from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS
from portfolio_dash.data_ingestion.dividend_import import DIVIDEND_COLUMNS
from portfolio_dash.shared.cash_kinds import CASH_KIND_VALUES

CORPUS = Path(__file__).resolve().parents[1] / "golden" / "ai_extraction" / "cases.json"

#: Required asserted fields per kind (the union's money-of-record skeleton).
_REQUIRED = {
    "txn": {"account", "symbol", "side", "date", "shares", "price", "daytrade", "short_sale"},
    "div": {"account", "symbol", "date", "type", "gross"},
    "cash": {"account", "date", "kind", "ccy", "amount"},
}
#: Allowable asserted fields per kind — exactly the kind's CSV columns (the runner compares
#: at the commit-CSV level; a field outside the columns is a field nothing reads).
_ALLOWED = {
    "txn": set(_TXN_CSV_COLUMNS),
    "div": set(DIVIDEND_COLUMNS),
    "cash": set(CASH_MOVEMENT_COLUMNS),
}
#: The accounts the live runner seeds in self-contained mode (``seed_accounts``). A case
#: asserting any other id can never pass a live run — and a red baseline that is corpus rot,
#: not a prompt regression, is exactly what this guard exists to rule out.
_ACCOUNT_IDS = {a.account_id for a in DEFAULT_ACCOUNTS}


def _cases() -> list[dict[str, object]]:
    doc = json.loads(CORPUS.read_text(encoding="utf-8"))
    cases: list[dict[str, object]] = doc["cases"]
    return cases


def _check_case(c: dict[str, Any]) -> None:
    """One case's well-formedness, as assertions — extracted so the guard itself can be
    tested against rot (a check that cannot fail is not a check)."""
    assert c["id"] and c["input"].strip(), c["id"]
    if "today" in c:
        date.fromisoformat(c["today"])
    expect = c["expect"]
    assert isinstance(expect["rows"], list)
    assert isinstance(expect["unparsed_contains"], list)
    for sub in expect["unparsed_contains"]:
        # "" is a substring of EVERY confession — a blank expectation makes the runner's
        # recall check pass vacuously, which is rot wearing a green checkmark.
        assert isinstance(sub, str) and sub.strip(), (
            f"{c['id']}: blank unparsed_contains substring")
    for row in expect["rows"]:
        kind = row["kind"]
        assert kind in _REQUIRED, f"{c['id']}: unknown row kind {kind!r}"
        fields = row["fields"]
        missing = _REQUIRED[kind] - fields.keys()
        assert not missing, f"{c['id']}: {kind} row missing required fields {missing}"
        stray = fields.keys() - _ALLOWED[kind]
        assert not stray, f"{c['id']}: {kind} row asserts non-CSV fields {stray}"
        assert fields["account"] in _ACCOUNT_IDS, (
            f"{c['id']}: account {fields['account']!r} is not a seeded account id "
            f"— the live runner can never pass this case")
        for name, value in fields.items():
            # Money/quantity as a JSON float would make the ground truth itself fuzzy.
            assert isinstance(value, str), (
                f"{c['id']}.{kind}.{name}: expected values are strings, got {value!r}")
        date.fromisoformat(fields["date"])


def test_corpus_parses_and_every_case_is_well_formed() -> None:
    doc = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert date.fromisoformat(doc["today_default"])
    cases = doc["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case id"
    for c in cases:
        _check_case(c)


def test_the_guard_catches_rotten_cases() -> None:
    """The guard is only worth its determinism if rot actually fails it (AI-D20).

    Two rot shapes the structural checks alone do not see: an account id the runner never
    seeds (the case goes red forever and reads as a prompt regression), and a blank
    ``unparsed_contains`` substring (the confession recall passes vacuously).
    """
    good: dict[str, object] = {
        "id": "synthetic", "input": "入金 1000",
        "expect": {
            "rows": [{"kind": "cash", "fields": {
                "account": "tw_broker", "date": "2026-06-01", "kind": "DEPOSIT",
                "ccy": "TWD", "amount": "1000"}}],
            "unparsed_contains": [],
        },
    }
    _check_case(good)  # sanity: the fixture shape itself is well-formed
    bad_account = json.loads(json.dumps(good))
    bad_account["expect"]["rows"][0]["fields"]["account"] = "yuanta"
    blank_substring = json.loads(json.dumps(good))
    blank_substring["expect"]["rows"] = []
    blank_substring["expect"]["unparsed_contains"] = ["  "]
    for rotten in (bad_account, blank_substring):
        with pytest.raises(AssertionError):
            _check_case(rotten)


def test_corpus_vocabulary_is_the_doors_own() -> None:
    """Sides, dividend types and cash kinds are the STORED spellings — a corpus that teaches
    anything else would be measuring the model against a vocabulary the door rejects."""
    for c in _cases():
        for row in c["expect"]["rows"]:  # type: ignore[index]
            fields = row["fields"]
            if row["kind"] == "txn":
                assert fields["side"] in ("BUY", "SELL"), c["id"]
                assert fields["daytrade"] in ("0", "1"), c["id"]
                assert fields["short_sale"] in ("0", "1"), c["id"]
            elif row["kind"] == "div":
                assert fields["type"] in ("CASH", "STOCK", "DRIP", "NET"), c["id"]
            else:
                assert fields["kind"] in CASH_KIND_VALUES, c["id"]
                assert fields["ccy"] in ("TWD", "USD", "MYR"), c["id"]


def test_corpus_coverage_floors() -> None:
    """The floors that make 'the corpus covers it' a checkable claim (AI-D20)."""
    cases = _cases()
    assert len(cases) >= 30, "the corpus thinned below its floor — extend, don't prune"
    rows = [row for c in cases for row in c["expect"]["rows"]]  # type: ignore[index]
    by_kind = {"txn": 0, "div": 0, "cash": 0}
    for row in rows:
        by_kind[row["kind"]] += 1
    assert by_kind["txn"] >= 10 and by_kind["div"] >= 4 and by_kind["cash"] >= 8

    # The three silent-money fields, each proven on BOTH values.
    daytrade_on = sum(1 for r in rows
                      if r["kind"] == "txn" and r["fields"]["daytrade"] == "1")
    short_on = sum(1 for r in rows
                   if r["kind"] == "txn" and r["fields"]["short_sale"] == "1")
    assert daytrade_on >= 2, "daytrade=1 needs explicit-wording cases"
    assert short_on >= 2, "short_sale=1 needs explicit-wording cases"

    # Every cash kind appears at least once — a kind the corpus never asserts is a kind
    # whose mislabel rate the report silently cannot measure.
    seen_kinds = {r["fields"]["kind"] for r in rows if r["kind"] == "cash"}
    assert seen_kinds == CASH_KIND_VALUES, (
        f"cash kinds never asserted: {sorted(CASH_KIND_VALUES - seen_kinds)}")

    # The confession list is exercised (fx / corporate action / option / ambiguous).
    confessed = [c for c in cases if c["expect"]["unparsed_contains"]]  # type: ignore[index]
    assert len(confessed) >= 3
    # And at least one mixed multi-kind case — the shape AI-D3 exists for.
    assert any(len({r["kind"] for r in c["expect"]["rows"]}) >= 3  # type: ignore[index]
               for c in cases), "no three-kind mixed case"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_every_case_expectation_is_internally_consistent(case: dict[str, object]) -> None:
    """A case may not demand rows AND a full confession of everything — the two halves of
    its expectation must leave the model a reachable target."""
    rows = case["expect"]["rows"]  # type: ignore[index]
    confessed = case["expect"]["unparsed_contains"]  # type: ignore[index]
    assert rows or confessed, f"{case['id']}: expects nothing at all"
