"""The read-only POSTs: requests that send a body yet write nothing (PERF plan P2).

They POST only because their input does not fit a query string (a draft trade, a rebalance
target, a prompt being edited). Each answers from the computed state and writes no row — not
even an action-log row (``api/action_log.py::_EXCLUDED_PREFIXES`` lists them all) — so the
settlement store treats them like a GET (spec 2026-10-18 §12.2): they may read the result cache
and never get the change actor's TEMP trigger (§7).

``POST /api/insight-tasks/{id}/preflight`` is deliberately NOT here: it writes an action-log
row (``action_log.py`` labels it), so it stays an ordinary POST. Dropping it from the action log
would need an owner ruling, and with the store its build is a settled build anyway.

``tests/contract/test_read_only_posts.py`` proves each entry writes nothing and is excluded
from the action log.
"""

from __future__ import annotations

#: (method, exact path) of every read-only POST.
READ_ONLY_POSTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/whatif"),
        ("POST", "/api/rebalance/preview"),
        ("POST", "/api/prompts/preview"),
    }
)


def is_read_only(method: str, path: str) -> bool:
    """``True`` for a GET / HEAD / OPTIONS and for a declared read-only POST."""
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    return (method, path.rstrip("/") or "/") in READ_ONLY_POSTS


__all__ = ["READ_ONLY_POSTS", "is_read_only"]
