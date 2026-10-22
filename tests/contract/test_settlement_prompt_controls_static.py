"""``web/settlement.js``: the settlement's own controls never re-arm the re-settle prompt
(code review of v0.1.32, 2026-10-20). The behaviour is driven in a browser by
``tests/e2e/test_settlement_prompt_rearm_flow.py``; this pins the listener in the hermetic
suite, so the filter cannot be dropped between e2e runs.
"""

from __future__ import annotations

import re
from pathlib import Path

_SETTLE = (Path(__file__).resolve().parents[2] / "web" / "settlement.js").read_text(
    encoding="utf-8")


def test_the_prompt_listener_skips_exactly_the_two_settlement_controls() -> None:
    controls = re.search(r"const SETTLEMENT_CONTROLS = \[([^\]]*)\];", _SETTLE)
    assert controls, "settlement.js no longer names its controls"
    assert re.findall(r"'([^']+)'", controls.group(1)) == [
        "/api/settlement/run", "/api/settlement/cancel"]
    listener = re.search(
        r"document\.addEventListener\('pd-mutated', \(ev\) => \{(.*?)\n  \}\);", _SETTLE, re.S)
    assert listener, "the pd-mutated listener changed shape"
    body = listener.group(1)
    skip = body.find("if (isSettlementControl(ev && ev.detail)) return;")
    arm = body.find("promptWanted = true;")
    assert 0 <= skip < arm, "a settlement control must return before the prompt is re-armed"
