"""Atomic JSON hand-off from a child fixture to its test."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Write *payload* so a polling reader never sees a half-written file."""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, target)
