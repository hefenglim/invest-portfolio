"""DEF-024, the frontend half (functional test I-06, 2026-09-23): a blocked row is never a success.

The server half (``tests/contract/test_def024_error_rows_never_cover.py``) made
``/api/import/commit`` say WHY each skipped row was skipped (``skipped_rows[].code``). This
file holds ``web/input.js`` to reading it: a file whose ticked sell was dropped at the
re-check announced 「✓ 寫入成功 成功 0 筆・跳過 2 筆」 — the comment above that line even
claimed 「跳過 says you didn't tick it」. The page now says 「未勾選」 for the owner's own
choice and 「⚠ 寫入完成（有列被擋下）」 + one line per row for everything else, and a preview
row's ADVISORY (``info``) is a grey note, never a warning.

The outcome logic is run for real (Node, from Playwright's driver — the same interpreter
``test_web_js_parses.py`` uses) on the exact functions shipped in input.js, not on a copy.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_SRC = (_WEB / "input.js").read_text(encoding="utf-8")


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _slice(start: str, end: str) -> str:
    i = _SRC.index(start)
    return _SRC[i:_SRC.index(end, i)]


def _run(cases: dict[str, Any]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    code = (
        "const el = () => null;\n"
        + _slice("  function commitOutcome(", "  /* CSV-import success handler (C7)")
        + _slice("  function rowReasonParts(", "  function appendInfoLines(")
        + "const cases = " + json.dumps(cases, ensure_ascii=False) + ";\n"
        + "const out = {};\n"
        + "for (const [k, c] of Object.entries(cases)) {\n"
        + "  if (c.row) { out[k] = rowReasonParts(c.row); continue; }\n"
        + "  const o = commitOutcome(c.resp);\n"
        + "  out[k] = { text: outcomeText(o), stopped: outcomeStopped(o), lines: outcomeLines(o),"
        + " deselected: o.deselected };\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    done = subprocess.run([str(node), "-e", code], capture_output=True, timeout=30,
                          encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def test_the_measured_cases_read_the_way_the_owner_needs() -> None:
    got = _run({
        # 「buy 100」 unticked + 「sell 150」 ticked: the sell is dropped at the re-check.
        "narrowed": {"resp": {"written": 0, "skipped": 2, "skipped_rows": [
            {"row": 1, "symbol": "2884", "code": "deselected", "message": "未勾選"},
            {"row": 2, "symbol": "2884", "code": "sell_exceeds_holdings",
             "message": "賣出 150 股，超過持有 0 股"}]}},
        # the owner's own choice only — a success, worded as the choice it was
        "deselected_only": {"resp": {"written": 1, "skipped": 2, "skipped_rows": [
            {"row": 2, "symbol": "", "code": "deselected", "message": "未勾選"},
            {"row": 3, "symbol": "", "code": "deselected", "message": "未勾選"}]}},
        # a hard refusal, reported by the importer as rejected
        "rejected": {"resp": {"written": 1, "skipped": 0, "rejected": 1, "rejected_rows": [
            {"row": 1, "kind": "shares_not_integer", "message": "台股股數必須是整數"}]}},
        # an older server without skipped_rows: every skip keeps its old meaning
        "legacy": {"resp": {"written": 1, "skipped": 2}},
        "clean": {"resp": {"written": 3, "skipped": 0, "duplicates": 2}},
    })
    assert got["narrowed"] == {
        "text": "成功 0 筆・未勾選 1 筆・被擋下 1 筆", "stopped": 1, "deselected": 1,
        "lines": ["第 2 列 2884：賣出 150 股，超過持有 0 股"]}
    assert got["deselected_only"] == {
        "text": "成功 1 筆・未勾選 2 筆", "stopped": 0, "deselected": 2, "lines": []}
    assert got["rejected"] == {
        "text": "成功 1 筆・被擋下 1 筆", "stopped": 1, "deselected": 0,
        "lines": ["第 1 列：台股股數必須是整數"]}
    assert got["legacy"]["text"] == "成功 1 筆・未勾選 2 筆" and got["legacy"]["stopped"] == 0
    assert got["clean"]["text"] == "成功 3 筆・已匯入過 2 筆"


def test_an_advisory_is_never_the_gating_reason() -> None:
    got = _run({
        "advisory_only": {"row": {"status": "ok", "reason": "早於帳本起點",
                                  "info": ["早於帳本起點"]}},
        "gating_and_advisory": {"row": {"status": "warn", "reason": "重複交易",
                                        "info": ["早於帳本起點"]}},
        "plain": {"row": {"status": "warn", "reason": "重複交易"}},
    })
    assert got["advisory_only"] == {"gating": "", "info": ["早於帳本起點"]}
    assert got["gating_and_advisory"] == {"gating": "重複交易", "info": ["早於帳本起點"]}
    assert got["plain"] == {"gating": "重複交易", "info": []}


def _fn(name: str) -> str:
    m = re.search(r"\n\s*(?:async\s+)?function " + name + r"\(", _SRC)
    assert m, name
    nxt = re.search(r"\n  (?:async\s+)?function \w+\(", _SRC[m.end():])
    return _SRC[m.start():m.end() + (nxt.start() if nxt else len(_SRC))]


@pytest.mark.parametrize("name", ["onCsvWritten", "finishAiCommits", "onAiCommitted",
                                  "oneRowNotWrittenReason"])
def test_every_commit_door_reads_the_outcome_not_the_bare_count(name: str) -> None:
    """The three doors (CSV, AI, one-row forms) share commitOutcome; none may go back to
    rendering the raw `skipped` number as a success."""
    body = _fn(name)
    assert "commitOutcome(" in body, f"{name} no longer reads commitOutcome"
    assert not re.search(r"resp\.skipped\b", body), f"{name} reads the bare skipped count"


def test_the_success_toast_is_gated_on_nothing_being_blocked() -> None:
    for name in ("onCsvWritten", "finishAiCommits"):
        body = _fn(name)
        warn = body.index("'⚠ 寫入完成（有列被擋下）'")
        ok = body.index("window.toast('寫入成功'")
        assert warn < ok and "} else {" in body[warn:ok], name


def test_both_previews_render_advisories_through_one_helper() -> None:
    assert "rowReasonParts(r)" in _fn("renderCsvPreview")
    assert "rowReasonParts(r)" in _fn("aiStatusCell")
    assert "appendInfoLines(" in _fn("renderCsvPreview") and "appendInfoLines(" in _fn(
        "aiStatusCell")
