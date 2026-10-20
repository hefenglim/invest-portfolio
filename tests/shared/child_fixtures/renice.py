"""``renice <out_json>``: lower this child's priority the way a worker ``main()`` does."""

from __future__ import annotations

import os
import sys
from typing import Any

from portfolio_dash.shared.child_runner import lower_own_priority
from tests.shared.child_fixtures._io import write_json


def main() -> None:
    applied = lower_own_priority()
    report: dict[str, Any] = {"applied": list(applied), "niceness": None, "policy": None,
                              "sched_idle": None}
    if sys.platform != "win32":
        report["niceness"] = os.nice(0)
    if sys.platform == "linux":
        report["policy"] = os.sched_getscheduler(0)
        report["sched_idle"] = getattr(os, "SCHED_IDLE", None)
    write_json(sys.argv[1], report)


if __name__ == "__main__":
    main()
