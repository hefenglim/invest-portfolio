"""``sleeper <ready_json> [seconds]``: announce itself (pid), then sleep (default 60 s)."""

from __future__ import annotations

import os
import sys
import time

from tests.shared.child_fixtures._io import write_json


def main() -> None:
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    write_json(sys.argv[1], {"pid": os.getpid()})
    time.sleep(seconds)


if __name__ == "__main__":
    main()
