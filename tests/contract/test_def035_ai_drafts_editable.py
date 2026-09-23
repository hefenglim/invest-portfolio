"""DEF-035 / DEF-036, the static half: the AI draft table edits DRAFT FIELDS, never CSV text.

The behaviour is driven end to end by ``tests/e2e/test_def004_def035_input_previews_flow.py``;
this file pins the shape that makes it trustworthy, in a second:

* each kind's editable columns are exactly draft fields the backend model declares (a renamed
  field would otherwise be edited in the browser and silently dropped by ``extra="forbid"``…
  or worse, accepted under the old name by nobody);
* an edit goes back through the AI door as ``drafts`` — the browser never assembles or parses
  a CSV line for an AI row (the commit CSV is the server's, regenerated per edit);
* a kind mid-edit is never written (``aiKindBlocked`` gates both the button and the commit);
* DEF-036: a contradicted row is never pre-ticked, and a ticked one is confirmed once more.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from portfolio_dash.data_ingestion.agents import CashDraft, DivDraft, TxnDraft

_SRC = (Path(__file__).resolve().parents[2] / "web" / "input.js").read_text(encoding="utf-8")

_EDITABLE: dict[str, tuple[type[BaseModel], set[str]]] = {
    "aiTxnCells": (TxnDraft, {"account_id", "date", "side", "symbol", "shares", "price"}),
    "aiDivCells": (DivDraft, {"account_id", "date", "symbol", "type", "gross", "withholding",
                              "net", "reinvest_shares", "reinvest_price"}),
    "aiCashCells": (CashDraft, {"account_id", "date", "cash_kind", "ccy", "amount",
                                "acq_home_amount"}),
}


def _fn(name: str) -> str:
    m = re.search(r"\n\s*(?:async\s+)?function " + name + r"\(", _SRC)
    assert m, f"web/input.js lost {name}"
    nxt = re.search(r"\n  (?:async\s+)?function \w+\(", _SRC[m.end():])
    return _SRC[m.start():m.end() + (nxt.start() if nxt else len(_SRC))]


def _edited_fields(body: str) -> set[str]:
    fields = set(re.findall(r"aiEdit(?:Input|Select)\(\s*[\w']+,\s*r\.n,\s*'(\w+)'", body))
    if "aiAccountSelect(" in body:
        fields.add("account_id")
    if "aiSymbolEditCell(" in body:
        fields.add("symbol")
    return fields


@pytest.mark.parametrize("fn", sorted(_EDITABLE))
def test_each_kind_edits_exactly_its_draft_fields(fn: str) -> None:
    model, expected = _EDITABLE[fn]
    got = _edited_fields(_fn(fn))
    assert got == expected, f"{fn} edits {sorted(got)}, expected {sorted(expected)}"
    assert got <= set(model.model_fields), (
        f"{fn} edits {sorted(got - set(model.model_fields))}, which {model.__name__} "
        "does not declare — the edit would be refused (extra='forbid') or lost")


def test_the_account_column_is_a_select_of_display_names_never_the_raw_id() -> None:
    body = _fn("aiAccountSelect")
    assert "pdNames.accountOption(a)" in body
    for fn in _EDITABLE:
        assert not re.search(r"el\('td',\s*'col-text',\s*d\.account_id", _fn(fn)), fn


def test_an_edit_goes_back_through_the_ai_door_as_drafts() -> None:
    body = _fn("revalidateAiKind")
    assert re.search(r"api\.post\('/api/input/ai/preview',\s*\{\s*drafts:", body)
    assert "aiCsvTexts[kind] =" in body            # the server's regenerated CSV replaces ours
    for fn in ("onAiEdit", "revalidateAiKind", "aiTxnCells", "aiDivCells", "aiCashCells"):
        b = _fn(fn)
        assert "csvEscape(" not in b and "oneRowCsv(" not in b, (
            f"{fn} assembles a CSV line in the browser — the AI door regenerates it")


def test_a_typed_value_is_kept_and_flagged_not_rewritten() -> None:
    body = _fn("onAiEdit")
    assert "AI_PLAIN_NUMBER.test(v)" in body
    assert "d[field] = (spec && !spec[1] && v === '') ? null : v;" in body   # kept as typed
    assert "aiKindHasFieldErr(kind)" in body


def test_a_kind_mid_edit_is_never_written() -> None:
    assert "aiKindBlocked" in _fn("refreshAiWriteBtn")
    assert "aiKindBlocked" in _fn("commitAi")
    m = re.search(r"const aiKindBlocked = \(kind\) => ([^;]+);", _SRC)
    assert m and all(k in m.group(1) for k in ("aiPending", "aiStale", "aiKindHasFieldErr"))


def test_a_contradicted_amount_is_not_preticked_and_is_confirmed() -> None:
    """DEF-036: the flag is preview-only, so the acknowledgement lives on this page."""
    assert "!aiIsMismatch(r)" in _fn("aiCheckboxCell")
    commit = _fn("commitAi")
    assert "aiIsMismatch(r)" in commit and "window.confirmDialog(" in commit
    assert "amount_mismatch" in _SRC and "stated_amount" in _SRC
