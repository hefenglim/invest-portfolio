"""``lock_holder <lock_path> <ready_json>``: take the lock, announce it, hold it until killed."""

from __future__ import annotations

import os
import sys
import time

from portfolio_dash.shared import proc_lock
from tests.shared.child_fixtures._io import write_json


def main() -> None:
    handle = proc_lock.lock(sys.argv[1], timeout=10.0)
    write_json(sys.argv[2], {"pid": os.getpid(), "held": handle.held})
    time.sleep(60)


if __name__ == "__main__":
    main()
