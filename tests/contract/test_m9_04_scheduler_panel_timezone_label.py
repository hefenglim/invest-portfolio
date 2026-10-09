"""M9-04: the 排程工作表 panel printed 「時區 」 and nothing after it — since birth.

``<span id="sched-tz">`` arrived with the spec-19 mock skeleton (``14e5b41``, 2026-06-13)
as a design-handoff panel subtitle, and no script in any version since has written to it:
``git log -S"sched-tz"`` finds that one commit, and ``grep`` over ``web/*.js`` finds nothing.
It could not be filled honestly either — the job table itself carries a 時區 column because
the jobs do not share one (``quotes_us`` runs in America/New_York, ``quotes_my`` in
Asia/Kuala_Lumpur, the rest in Asia/Taipei), and ``GET /api/scheduler/jobs`` has no
top-level timezone to print. Owner ruling: delete the label rather than invent a value that
would be false for two rows.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_HEAD = re.compile(r'<h2 class="panel-title">排程工作表</h2>(.*?)</div>', re.S)


def test_the_scheduler_panel_head_carries_no_orphan_timezone_label() -> None:
    html = (_WEB / "settings.html").read_text(encoding="utf-8")
    assert 'id="sched-tz"' not in html, "the never-filled #sched-tz span is still in the page"
    head = _HEAD.search(html)
    assert head, "排程工作表 panel head not found"
    assert "時區" not in head.group(1), (
        f"the panel-sub still announces a timezone nothing fills: {head.group(1)!r}")


def test_no_script_ever_wrote_the_label() -> None:
    """Why deletion, not a fix: there is no writer to repair."""
    writers = sorted(p.name for p in _WEB.glob("*.js")
                     if "sched-tz" in p.read_text(encoding="utf-8"))
    assert writers == [], f"a script now references #sched-tz — revisit the ruling: {writers}"
