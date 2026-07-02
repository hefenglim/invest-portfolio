"""Build identity: version + git commit + tag-release status (deploy visibility).

Every instance runs from a git checkout (prod = a released tag, test site = a
branch), so the checkout is the source of truth for "what exactly is running".
Resolved once per process (``lru_cache``); a non-git install (wheel / container)
degrades to the ``BUILD_COMMIT`` / ``BUILD_RELEASE`` env overrides or
``"unknown"`` / ``"unreleased"`` without ever raising.
"""

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from portfolio_dash import __version__

_REPO_ROOT = Path(__file__).resolve().parents[2]


class BuildInfo(BaseModel):
    version: str
    commit: str  # short hash, or "unknown"
    release: str  # exact tag on HEAD (e.g. "v0.1.4"), else "unreleased"


def _git(*args: str) -> str | None:
    """Run one git command at the repo root; None on any failure (never raises)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    """The running build's identity: package version + short commit + release tag.

    ``release`` is the tag name when HEAD is EXACTLY a tag (a promoted release),
    else ``"unreleased"`` — the deploy check that prod never runs untagged code.
    """
    commit = os.environ.get("BUILD_COMMIT") or _git("rev-parse", "--short", "HEAD")
    tag = os.environ.get("BUILD_RELEASE") or _git("describe", "--tags", "--exact-match", "HEAD")
    return BuildInfo(
        version=__version__,
        commit=commit or "unknown",
        release=tag or "unreleased",
    )
