"""DEF-027 (2026-09-23): no commit door acknowledges warnings on the owner's behalf.

``web/broker-import.js`` sent ``ack_warnings: true`` on every ``/api/import/commit`` it
made — the one acknowledgement that permanently discards a cost basis, given by a literal
in a request body. A statement selling 1,000 AAPL into an account holding 85.04 therefore
wrote the sell with no dialog: 「✓ 全部寫入完成」, AAPL at −914.96 shares, basis gone.

The class, not the instance: a literal ``ack_warnings: true`` is only legitimate inside the
``onConfirm`` of a dialog that has just shown the owner WHAT is being acknowledged. Every
such literal in ``web/`` is therefore held to that shape here — it must sit within a few
lines below a dialog call — and the broker door, which acknowledges per ROW, must carry no
literal at all: its flag is a variable the dialog's decision sets.

Why the e2e suite did not catch it: ``test_broker_web_import_flow`` asserted the first
commit's status was 200 and that 「全部寫入完成」 appeared. The corpus's PREH sell IS a 賣超
against an empty ledger; the test encoded the blanket ack as the expected behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")
_ACK_LITERAL = re.compile(r"ack_warnings\s*:\s*true\b")
_DIALOG_CALL = re.compile(r"\b(?:confirmDialog|oversellDialog|warningsDialog)\s*\(")

#: How far above a literal ack the dialog that justifies it may sit. Both surviving
#: literals (input.js's two single-row doors) are 11 and 7 lines below theirs.
_DIALOG_WINDOW = 30


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _code(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return _LINE_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, src))


def test_the_broker_door_never_acknowledges_by_literal() -> None:
    src = _code(_WEB / "broker-import.js")
    assert not _ACK_LITERAL.search(src), (
        "web/broker-import.js sends ack_warnings: true as a literal — the acknowledgement "
        "must come from the per-row dialog's decision (DEF-027)"
    )
    # The per-row path exists: the server's refusal code is handled, the rows are fetched
    # through the preview door, and the dialog's ticks start unticked.
    assert "warnings_unacknowledged" in src
    assert "/api/import/preview" in src
    assert "bk-warn-tick" in src
    assert re.search(r"ack_warnings:\s*ack\b", src), "the flag is the dialog's decision"
    # …and the ticked rows travel as `select` indices, never as a re-rendered CSV (F-03).
    assert re.search(r"body\.select\s*=\s*select", src)


def test_every_literal_ack_in_web_sits_inside_a_dialogs_confirm() -> None:
    """The class. A literal ack more than ``_DIALOG_WINDOW`` lines below any dialog call
    is an acknowledgement nobody was asked for."""
    offenders: list[str] = []
    for path in sorted(_WEB.glob("*.js")):
        if path.name == "echarts.min.js":
            continue
        lines = _code(path).split("\n")
        for n, line in enumerate(lines):
            if not _ACK_LITERAL.search(line):
                continue
            window = "\n".join(lines[max(0, n - _DIALOG_WINDOW):n])
            if not _DIALOG_CALL.search(window):
                offenders.append(f"{path.name}:{n + 1}")
    assert not offenders, (
        "ack_warnings: true sent without a dialog naming what is acknowledged: "
        + ", ".join(offenders)
    )


def test_the_detector_can_see_a_blanket_ack() -> None:
    """Positive control for the guard above: the shape it exists to refuse."""
    assert _ACK_LITERAL.search("const body = { kind, csv_text: text, ack_warnings: true };")
    assert not _ACK_LITERAL.search("ack_warnings: ack,")
    assert _DIALOG_CALL.search("window.confirmDialog({")
