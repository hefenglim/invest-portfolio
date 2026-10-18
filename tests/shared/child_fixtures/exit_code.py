"""``exit_code <code>``: say goodbye on stderr and exit with *code*."""

from __future__ import annotations

import sys


def main() -> None:
    code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sys.stderr.write(f"bye with {code}\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
