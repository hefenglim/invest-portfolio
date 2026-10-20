"""``noisy <nbytes>``: flood stderr with *nbytes* of filler, end with a zh marker, exit 3."""

from __future__ import annotations

import sys

END_MARKER = "END-MARKER 結束"


def main() -> None:
    nbytes = int(sys.argv[1])
    line = "x" * 99 + "\n"
    for _ in range(nbytes // len(line)):
        sys.stderr.write(line)
    sys.stderr.write(END_MARKER + "\n")
    sys.stderr.flush()
    sys.exit(3)


if __name__ == "__main__":
    main()
