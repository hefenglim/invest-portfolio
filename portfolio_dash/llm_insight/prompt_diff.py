"""Line diff between two prompt bodies (DEF-033, owner ruling 2026-09-24).

The strategy-prompt version history shows what changed between two saved versions. The diff
is computed HERE, server-side, with the stdlib ``difflib`` (no new dependency — ``stack.md``),
so the page only renders the ops it is handed and holds no diff algorithm of its own.

Pure: stdlib + pydantic only. No LLM, no money, no connection.
"""

import difflib
from typing import Literal

from pydantic import BaseModel

DiffOp = Literal["same", "add", "del"]


class DiffLine(BaseModel):
    """One rendered diff line.

    ``op`` is ``same`` (in both), ``del`` (only in the OLDER body) or ``add`` (only in the
    NEWER body); ``old_no`` / ``new_no`` are 1-based line numbers in each body (None on the
    side the line is absent from).
    """

    op: DiffOp
    text: str
    old_no: int | None
    new_no: int | None


class LineDiff(BaseModel):
    lines: list[DiffLine]
    added: int
    removed: int

    @property
    def identical(self) -> bool:
        return self.added == 0 and self.removed == 0


def line_diff(older: str, newer: str) -> LineDiff:
    """The line-by-line diff that turns *older* into *newer* (every line kept, in order).

    A ``replace`` block is emitted as its deleted lines followed by its added lines — the
    reading order of a unified diff — so the page never interleaves the two sides.
    """
    a = older.splitlines()
    b = newer.splitlines()
    out: list[DiffLine] = []
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append(DiffLine(op="same", text=a[i1 + k], old_no=i1 + k + 1,
                                    new_no=j1 + k + 1))
            continue
        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                out.append(DiffLine(op="del", text=a[k], old_no=k + 1, new_no=None))
                removed += 1
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                out.append(DiffLine(op="add", text=b[k], old_no=None, new_no=k + 1))
                added += 1
    return LineDiff(lines=out, added=added, removed=removed)
