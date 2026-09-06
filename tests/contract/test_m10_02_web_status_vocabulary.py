"""M10-02 — one run, one word: the scheduler center speaks ``pipeline.js``'s ``partial``.

A ``partial`` insight run was painted 「失敗」 by the scheduler center, 「部分」 by
``web/pipeline.js`` and 「完成」 by ``web/detail.js`` — three screens, three answers. The
scheduler center has FOUR render points that only knew ok / running / skipped; each now
maps ``partial`` to exactly the class + label ``pipeline.js`` already uses, read from
``pipeline.js`` here so the two cannot drift apart silently.

``web/shell.js``'s toast knew two faces (``'fail'`` red ✕, everything else green ✓) — three
callers already passed ``'warn'`` and were painted as success. The third face exists now, and
the quote-refresh handler reads the door's ``results`` instead of declaring 完成 unread.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_SCHED = (_WEB / "settings-scheduler.js").read_text(encoding="utf-8")
_PIPE = (_WEB / "pipeline.js").read_text(encoding="utf-8")
_SHELL = (_WEB / "shell.js").read_text(encoding="utf-8")


def _pipeline_partial() -> tuple[str, str]:
    m = re.search(r"partial:\s*\[\s*'([\w-]+)'\s*,\s*'([^']+)'\s*\]", _PIPE)
    assert m, "pipeline.js no longer declares a partial mapping"
    return m.group(1), m.group(2)


def test_pipeline_vocabulary_is_the_one_we_copy() -> None:
    assert _pipeline_partial() == ("pill-warn", "部分")


def test_history_table_maps_partial_like_pipeline() -> None:
    cls, label = _pipeline_partial()
    m = re.search(r"const HIST_STATUS = \{(.*?)\};", _SCHED, re.S)
    assert m, "HIST_STATUS map missing"
    assert re.search(rf"partial:\s*\['{cls}',\s*'{label}'\]", m.group(1)), (
        "history table does not map partial to pipeline.js's class/label")


def test_modal_and_chip_and_dot_render_partial() -> None:
    """The modal (執行結果), the live status chip and the last-run dot each branch on
    ``partial`` BEFORE falling through to 失敗, and use pipeline.js's label."""
    cls, label = _pipeline_partial()
    branches = re.findall(
        rf"status === 'partial'\)\s*\{{\s*cls = '{cls}';\s*label = '{label}';", _SCHED)
    assert len(branches) == 2, f"expected the modal + chip branches, found {len(branches)}"
    assert re.search(r"j\.last\.status === 'partial'", _SCHED), "last-run dot ignores partial"


def test_toast_has_a_third_face() -> None:
    m = re.search(r"window\.toast = function \(msg, kind, sub\) \{(.*?)\n  \};", _SHELL, re.S)
    assert m, "window.toast not found"
    body = m.group(1)
    assert "toast-warn" in body, "toast still has only ok/fail classes"
    assert "kind === 'warn'" in body
    assert re.search(r"warn:\s*\(msg2, sub2\) => settle\('warn'", _SHELL), (
        "toastProgress has no warn() settle")


def test_refresh_quotes_handler_reads_results() -> None:
    m = re.search(r"mkOpt\('更新報價'.*?\n      \}\)\);", _SHELL, re.S)
    assert m, "更新報價 handler not found"
    body = m.group(0)
    assert "resp.results" in body, "handler still declares 完成 without reading results"
    assert "'partial'" in body and "prog.warn(" in body
    assert "'error'" in body and "prog.fail(" in body
