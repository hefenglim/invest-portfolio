"""Shared plumbing for the ``scripts/perf`` tools (PERF plan W0.2 / W0.4).

Every measurement tool works on a COPY of the database it is given and refuses a ``--db`` or
``--out`` inside the repository: the repo is public, and the only real-data databases these
tools are ever pointed at (the two scratch ledgers) must never be read in place or have a
derived file land in a checkout.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

#: The checkout this script lives in (``scripts/perf/_refcommon.py`` -> the repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]


def ensure_repo_on_path() -> None:
    """Put this checkout first on ``sys.path`` so ``portfolio_dash`` and ``tests.reference``
    resolve HERE — the editable install maps ``portfolio_dash`` to the main checkout, and a
    worktree's script must run the worktree's code."""
    root = str(REPO_ROOT)
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)


def inside_repo(path: Path) -> bool:
    return path.resolve().is_relative_to(REPO_ROOT)


def refuse_inside_repo(path: Path, flag: str) -> None:
    """Exit with a message when *path* lies inside the repository."""
    if inside_repo(path):
        raise SystemExit(f"refusing {flag} {path}: it lies inside the repository "
                         f"({REPO_ROOT}); use a path outside it")


def copy_db_to_temp(src: Path) -> tuple[Path, Path]:
    """Copy *src* (plus any ``-wal`` / ``-shm`` side files) into a new temp directory.

    Returns ``(copy, temp_dir)``; the caller removes ``temp_dir``.
    """
    if not src.is_file():
        raise SystemExit(f"--db {src}: no such file")
    tmp = Path(tempfile.mkdtemp(prefix="pd-perf-"))
    dest = tmp / src.name
    shutil.copyfile(src, dest)
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copyfile(side, dest.with_name(dest.name + suffix))
    return dest, tmp
