"""M10-04 → PERF spec 2026-10-18 §14.4: 重新結算（驗證並重建歷史） replaces 重算（重建統計）.

History of this copy:

- **M10-04 (owner ruling 2026-09-06).** The 重算 item's confirm dialog said 「較耗時」 and its
  toast 「重算中…」 for a read-only replay that took 17–30 ms; the copy was fixed, the confirm
  dropped, and the wait the user felt — the ``location.reload()`` that followed — named.
- **PERF §14.4 (2026-10-18).** With the settlement store, the item becomes 重新結算（驗證並重建
  歷史）: a FULL settlement run whose phase 2 lists every 待釐清 record of the dashboard's
  tolerant replay (a finding is a warning toast and a panel line; it stops nothing), and whose
  swap refreshes the charts in place — so there is no reload any more. The panel carries the
  same button. ``POST /api/actions/recompute`` keeps its tests; the menu reaches it only under
  ``PD_SETTLE=off`` (``state: "off"``), where there is no history to rebuild and the item does
  what it did before (plan P5: off restores today's behaviour) — with the M10-04 copy intact.

Static source scans, like the original: no server, no browser.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_SHELL = (_WEB / "shell.js").read_text(encoding="utf-8")
_SETTLE = (_WEB / "settlement.js").read_text(encoding="utf-8")

_LABEL = "重新結算（驗證並重建歷史）"


def _menu_block() -> str:
    m = re.search(r"mkOpt\('" + re.escape(_LABEL) + r"'.*?\n      \}\)\);", _SHELL, re.S)
    assert m, f"the {_LABEL} menu option is missing from shell.js"
    return m.group(0)


def _function(src: str, name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?\n  \}\n", src, re.S)
    assert m, f"function {name} not found"
    return m.group(0)


def test_the_menu_item_is_renamed_and_the_old_one_is_gone() -> None:
    assert "mkOpt('重算（重建統計）'" not in _SHELL, "重算 is replaced, not kept beside 重新結算"
    block = _menu_block()
    assert "驗證" in block and "背景" in block   # the sub-line names the check and the wait


def test_the_menu_starts_a_full_run_through_settlement_js_with_no_gate_and_no_reload() -> None:
    block = _menu_block()
    assert "confirmDialog" not in block, "a background run the panel can cancel needs no gate"
    assert "window.pdSettlement.rebuild()" in block
    assert "/api/actions/recompute" not in block, "the menu no longer calls the strict replay"
    assert "location.reload()" not in block, "charts refresh on the swap — no reload"
    rebuild = _function(_SETTLE, "rebuild")
    assert "startRun('full')" in rebuild
    start = _function(_SETTLE, "startRun")
    assert "'/api/settlement/run'" in start and "location.reload" not in start


def test_the_panel_carries_the_same_button() -> None:
    idle = _function(_SETTLE, "buildIdle")
    assert f"button('{_LABEL}'" in idle and "startRun('full')" in idle


def test_off_keeps_todays_replay_with_the_m10_04_copy() -> None:
    """Under PD_SETTLE=off the item still runs the strict replay — and its copy stays the one
    the M10-04 ruling measured: no 「較耗時」, no 「重算中…」, the reload named as the wait."""
    rebuild = _function(_SETTLE, "rebuild")
    assert "status.state === 'off'" in rebuild and "legacyRecompute()" in rebuild
    legacy = _function(_SETTLE, "legacyRecompute")
    assert "api.post('/api/actions/recompute')" in legacy
    assert "confirmDialog" not in legacy
    assert "較耗時" not in legacy and "重算中…" not in legacy
    assert "重新整理" in legacy and "location.reload()" in legacy


def test_findings_are_a_warning_toast_and_never_a_stop() -> None:
    told = _function(_SETTLE, "maybeToastFindings")
    assert "'warn'" in told and "待釐清" in told
    assert "'fail'" not in told
