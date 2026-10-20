"""``raises <message>``: die of an unhandled exception carrying *message*."""

from __future__ import annotations

import sys


def main() -> None:
    raise RuntimeError(sys.argv[1] if len(sys.argv) > 1 else "boom")


if __name__ == "__main__":
    main()
