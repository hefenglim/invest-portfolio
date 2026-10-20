"""A byte-identity comparison form for engine results (PERF plan W0.2).

``==`` is too weak to prove two engines identical: ``Decimal("1.0") == Decimal("1")``, dicts
compare without order, and ``model_dump()`` / ``model_dump_json()`` skip every field declared
``exclude=True`` (``TrendSeries.options_value_excluded`` is one). :func:`canon` turns a result
— a Pydantic model, a dataclass, a list, a dict — into nested tuples where a ``Decimal`` keeps
its exact digits and exponent, a dict keeps its insertion order, and EVERY model field is
visited, excluded or not. Two results with equal ``canon`` serialise to the same bytes.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def canon(value: Any) -> Any:
    """The exact, order-preserving, exclude-blind form of *value* (see the module doc)."""
    if isinstance(value, BaseModel):
        return (type(value).__name__,
                tuple((name, canon(getattr(value, name))) for name in type(value).model_fields))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__name__,
                tuple((f.name, canon(getattr(value, f.name))) for f in dataclasses.fields(value)))
    if isinstance(value, Decimal):
        return ("D", str(value))  # str keeps the exponent: "1.0" vs "1", "0E-24" vs "0"
    if isinstance(value, Enum):
        return ("E", type(value).__name__, value.value)
    if isinstance(value, datetime | date):
        return (type(value).__name__, value.isoformat())
    if isinstance(value, Mapping):
        return ("M", tuple((canon(k), canon(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return ("L", tuple(canon(v) for v in value))
    if isinstance(value, set | frozenset):
        return ("S", tuple(sorted(repr(canon(v)) for v in value)))
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"canon: no form for {type(value).__name__}")


def first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    """Where two :func:`canon` forms first differ (``None`` when equal) — for a readable
    assertion message on a 2,000-point series."""
    if type(a) is not type(b):
        return f"{path}: {a!r:.200} != {b!r:.200}"
    if isinstance(a, tuple):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            label = x[0] if isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], str) else i
            found = first_difference(x, y, f"{path}/{label}")
            if found is not None:
                return found
        return None
    return None if a == b else f"{path}: {a!r:.200} != {b!r:.200}"


__all__ = ["canon", "first_difference"]
