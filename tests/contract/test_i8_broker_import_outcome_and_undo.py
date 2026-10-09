"""I-8 (D-2 / E-4): the 券商匯出檔 door reads a commit and an undo the way the CSV door does.

Four defects on one page (``web/broker-import.js``), all the server already answered and the
page never read:

* the step report printed ``'跳過 ' + r.skipped + ' 筆'`` — one number for two events (rows the
  owner left unticked, and rows the re-check DROPPED), the exact wording DEF-024 retired from
  ``input.js``; a dropped row did not even stop the sequence, so the later kinds were written
  against a position that was never built;
* 最近匯入 offered 復原 on a batch the server refuses to undo (``undoable: false`` —
  期初庫存), and printed the LIVE ``row_count`` alone, so a batch that wrote 5 and still owns 2
  read as a 2-row import;
* an undo that found nothing (``deleted: 0`` + ``message``) toasted 「已復原 刪除 0 筆」, and
  the 422 ``batch_not_undoable`` left the stale 復原 in place;
* the confirm said 「手動輸入的紀錄與其他批次不受影響」 while the one-row forms write through
  this door and appear in the same list as 「手動輸入」 batches — which 復原 removes.

The step summary is run for real in Node on the shipped functions (input.js's
``commitOutcome`` + broker-import.js's ``stepSummary``), not on a copy.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_INPUT = (_WEB / "input.js").read_text(encoding="utf-8")
_BROKER = (_WEB / "broker-import.js").read_text(encoding="utf-8")


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _summaries(cases: dict[str, Any]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    code = (
        "const KIND_ZH = { transactions: '交易', corporate_actions: '公司行動' };\n"
        + _slice(_INPUT, "  function commitOutcome(", "  /* CSV-import success handler (C7)")
        + _slice(_BROKER, "  function stepSummary(", "  function renderCommitResult(")
        + "const co = { read: commitOutcome, lines: outcomeLines };\n"
        + "const cases = " + json.dumps(cases, ensure_ascii=False) + ";\n"
        + "const out = {};\n"
        + "for (const [k, r] of Object.entries(cases))\n"
        + "  out[k] = stepSummary('transactions', r, co);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    done = subprocess.run([str(node), "-e", code], capture_output=True, timeout=30,
                          encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def test_a_dropped_row_is_blocked_with_its_reason_and_stops_the_sequence() -> None:
    got = _summaries({
        "narrowed": {"written": 0, "skipped": 2, "skipped_rows": [
            {"row": 1, "symbol": "AAPL", "code": "deselected", "message": "未勾選"},
            {"row": 2, "symbol": "AAPL", "code": "sell_exceeds_holdings",
             "message": "賣出 150 股，超過持有 0 股"}]},
        "choice_only": {"written": 3, "skipped": 2},
        "rejected": {"written": 1, "skipped": 0, "rejected": 1, "rejected_rows": [
            {"row": 4, "kind": "shares_not_integer", "message": "台股股數必須是整數"}]},
        "clean": {"written": 3, "skipped": 0, "duplicates": 2},
    })
    assert got["narrowed"] == {
        "text": "交易 寫入 0 筆・未勾選 1 筆・被擋下 1 筆",
        "lines": ["第 2 列 AAPL：賣出 150 股，超過持有 0 股"], "stopped": 1}
    assert got["choice_only"] == {"text": "交易 寫入 3 筆・未勾選 2 筆", "lines": [],
                                  "stopped": 0}
    assert got["rejected"]["stopped"] == 1
    assert got["rejected"]["lines"] == ["第 4 列：台股股數必須是整數"]
    assert got["clean"]["text"] == "交易 寫入 3 筆・已匯入過 2 筆"
    assert "跳過" not in json.dumps(got, ensure_ascii=False)


def _fn(src: str, name: str) -> str:
    m = re.search(r"\n\s*(?:async\s+)?function " + name + r"\(", src)
    assert m, name
    nxt = re.search(r"\n  (?:async\s+)?function \w+\(", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def test_the_report_and_the_stop_rule_read_the_outcome_not_the_bare_count() -> None:
    assert not re.search(r"r\.skipped\b", _fn(_BROKER, "renderCommitResult"))
    assert "stepSummary(" in _fn(_BROKER, "renderCommitResult")
    assert "stepSummary(step.kind, r).stopped" in _fn(_BROKER, "commitAll")
    assert "window.pdCommitOutcome = { read: commitOutcome, lines: outcomeLines }" in _INPUT


def test_the_batch_list_honours_undoable_and_names_both_counts() -> None:
    body = _fn(_BROKER, "loadBatches")
    assert "b.undoable === false" in body and "無法依批次復原" in body
    assert "batchCountText(b)" in body
    count = _fn(_BROKER, "batchCountText")
    assert "written_count" in count and "原寫入" in count


def test_the_undo_says_what_it_did_and_the_confirm_says_what_it_touches() -> None:
    # DEF-049 split the flow: undoBatch confirms, runUndo sends (and resends with the replay
    # guard's acknowledgements) and reports — the undo is the two together.
    body = _fn(_BROKER, "undoBatch") + _fn(_BROKER, "runUndo")
    assert "手動輸入的紀錄與其他批次不受影響" not in body
    assert "「手動輸入」" in body and "只刪除這一批寫入的列" in body
    assert "r.deleted === 0" in body and "r.message" in body
    assert "batch_not_undoable" in body
    assert "undoRestoreNotes(r)" in body
    notes = _fn(_BROKER, "undoRestoreNotes")
    assert "band_restore" in notes and "weight_restore" in notes
