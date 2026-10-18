"""``whoami <out_json> [ENV_KEY ...]``: report which code and environment this child runs."""

from __future__ import annotations

import os
import sys

import portfolio_dash
from tests.shared.child_fixtures._io import write_json


def main() -> None:
    write_json(sys.argv[1], {
        "package_file": portfolio_dash.__file__,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "flags": {"isolated": sys.flags.isolated},
        "env": {key: os.environ.get(key) for key in
                ("PYTHONPATH", "DB_PATH", "PYTHONIOENCODING", *sys.argv[2:])},
    })


if __name__ == "__main__":
    main()
