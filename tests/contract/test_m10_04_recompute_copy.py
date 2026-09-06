"""M10-04 — 重算's copy matches what it measures.

The confirm dialog said 「較耗時」 and the toast said 「重算中…」 for a read-only replay that
takes 17–30 ms end to end (E2 measured 24,000 rows in 0.77 s, 50,000 in 1.86 s — under a
second at any scale this app assumes). The copy and the endpoint were born together in the
mock era (2026-06-13) and never measured. Owner ruling: fix the copy, drop the confirm
(a 10 ms read-only action does not need a gate), keep the name 「重算」. The wait the user
actually feels is the ``location.reload()`` that follows, and the copy says so.
"""

import re
from pathlib import Path

_SHELL = (Path(__file__).resolve().parents[2] / "web" / "shell.js").read_text(encoding="utf-8")


def _recompute_block() -> str:
    m = re.search(r"mkOpt\('重算（重建統計）'.*?\n      \}\)\);", _SHELL, re.S)
    assert m, "重算 menu option not found (the name must stay 重算)"
    return m.group(0)


def test_no_confirm_dialog_and_no_slow_copy() -> None:
    block = _recompute_block()
    assert "confirmDialog" not in block, "a 10 ms read-only action must not be gated"
    assert "較耗時" not in block
    assert "重算中…" not in block


def test_copy_names_the_reload_as_the_wait() -> None:
    block = _recompute_block()
    assert "重新整理" in block
    assert "location.reload()" in block  # the reload is kept; the copy now owns it
