"""``heavy_probe <out_json> <heavy_lock> <seconds>``: record this child's life span and
whether the heavy gate's lock was held while it lived (it must be: the parent holds it)."""

from __future__ import annotations

import sys
import time

from portfolio_dash.shared import proc_lock
from tests.shared.child_fixtures._io import write_json


def main() -> None:
    out, lock_path, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    start = time.time_ns()
    held_at_start = proc_lock.is_held(lock_path)
    time.sleep(seconds)
    held_at_end = proc_lock.is_held(lock_path)
    write_json(out, {"start_ns": start, "end_ns": time.time_ns(),
                     "held_at_start": held_at_start, "held_at_end": held_at_end})


if __name__ == "__main__":
    main()
