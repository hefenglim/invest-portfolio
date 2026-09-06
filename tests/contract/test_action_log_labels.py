"""G-03: 系統操作記錄 has a Chinese label for every action it records — derived, not listed.

``api/action_log.py`` carries two hand-written tables: ``_LABELS`` (path prefix -> zh label,
first match wins) and ``_EXCLUDED_PREFIXES`` (paths that compute but change nothing). Both
had drifted behind the router table, so the 2026-09-02 sweep read raw English paths in the
owner's log — ``POST /api/ledgers/corporate-actions``, ``POST /api/llm-fail-log/export``,
``POST /api/news/run``, ``POST /api/insight-tasks/official-pack`` — while the *same* action
arriving through the ``/api/insight-types`` alias was labelled 「AI 洞察任務操作」. One
action, two formats, depending on which page fired it. The corporate-action form is worse
than cosmetic: its DEBOUNCED preview was logged, so one backfill wrote a burst of phantom
"operations" into a log whose own docstring promises previews are excluded.

**Why this test enumerates the app's routes instead of pinning the four reported gaps.**
All four gaps arrived the same way: an endpoint was added and nobody remembered the tables.
A test naming the four certifies the four and lets the fifth open silently — the repo's own
"a pin that names one field certifies one field" lesson. Reading FastAPI's route table makes
the guard a function of the app, so the day a mutating ``/api/*`` route is added without a
label, THIS fails and names it. That is the only version of this check that cannot rot.

The scan asks two things of every mutating ``/api/*`` route: it is either excluded (a
preview) or it has a real label — never the ``f"{method} {path}"`` fallback.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from portfolio_dash.api.action_log import label_for, should_log
from portfolio_dash.api.app import create_app

_PATH_PARAM = re.compile(r"\{[^}]+\}")
_MUTATING = ("POST", "PUT", "DELETE")


def _concrete(path: str) -> str:
    """A templated route path as the middleware actually sees it (``{id}`` -> ``1``).

    ``label_for``/``should_log`` match on ``str.startswith``, so they are fed a REQUEST
    path, never a template — otherwise ``/api/ledgers/{x}`` would match prefixes a real
    request never could.
    """
    return _PATH_PARAM.sub("1", path)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app()


def _mutating_api_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, concrete path) the action log can be asked to record."""
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in route.methods or set():
            if method in _MUTATING:
                out.add((method, _concrete(route.path)))
    return sorted(out)


def _is_fallback(method: str, path: str) -> bool:
    return label_for(method, path) == f"{method} {path}"


def test_the_route_scan_finds_the_app(app: FastAPI) -> None:
    """Positive control: an empty route list would make every assertion below vacuous."""
    routes = _mutating_api_routes(app)
    assert len(routes) > 50, f"expected the full router table, got {len(routes)}"
    assert ("POST", "/api/input/manual/commit") in routes


def test_the_fallback_is_reachable() -> None:
    """Positive control for the detector itself: an unknown path DOES hit the fallback."""
    assert _is_fallback("POST", "/api/definitely-not-a-route")
    assert not _is_fallback("POST", "/api/input/manual/commit")


def test_every_recorded_action_has_a_chinese_label(app: FastAPI) -> None:
    """No mutating /api route falls through to the raw ``METHOD /path`` fallback."""
    holes = [f"{m} {p}" for m, p in _mutating_api_routes(app)
             if should_log(m, p) and _is_fallback(m, p)]
    assert not holes, (
        f"{len(holes)} mutating /api endpoint(s) would be logged as a raw English path — "
        "add a label to _LABELS (or, for a pure preview, a prefix to _EXCLUDED_PREFIXES) "
        "in portfolio_dash/api/action_log.py:\n  " + "\n  ".join(holes)
    )


def test_every_preview_endpoint_is_excluded(app: FastAPI) -> None:
    """"Previews/what-ifs are excluded" — the module docstring's own promise, enforced.

    A preview computes and writes nothing, and the debounced ones fire per keystroke: one
    logged preview endpoint turns the log into request noise.
    """
    logged = [f"{m} {p}" for m, p in _mutating_api_routes(app)
              if p.endswith("/preview") and should_log(m, p)]
    assert not logged, (
        "preview endpoint(s) recorded as operations: " + ", ".join(logged)
        + " — add the prefix to _EXCLUDED_PREFIXES."
    )


def test_the_insight_task_alias_pair_labels_identically(app: FastAPI) -> None:
    """spec 07 §7.0 makes /api/insight-tasks/* a FULL alias of /api/insight-types/*.

    One resource reached by two paths must produce ONE action label; otherwise the log
    records the page the click came from instead of the thing that happened.
    """
    mismatched = []
    for method, path in _mutating_api_routes(app):
        if not path.startswith("/api/insight-types"):
            continue
        alias = path.replace("/api/insight-types", "/api/insight-tasks", 1)
        if label_for(method, path) != label_for(method, alias):
            mismatched.append(
                f"{method} {path} -> {label_for(method, path)!r} but "
                f"{alias} -> {label_for(method, alias)!r}")
    assert not mismatched, "alias pair labelled differently:\n  " + "\n  ".join(mismatched)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/ledgers/corporate-actions"),   # 公司行動新增 had no label
        ("POST", "/api/llm-fail-log/export"),         # sits outside export.py by design
        ("POST", "/api/insight-tasks/official-pack"),  # the alias half of a labelled action
        ("POST", "/api/insight-tasks/1/preflight"),
        ("POST", "/api/news/run"),
    ],
    ids=["corp_action_create", "fail_log_export", "official_pack", "preflight", "news_run"],
)
def test_the_reported_gaps_are_labelled(method: str, path: str) -> None:
    """Named regressions for the four classes the 2026-09-02 sweep actually measured."""
    assert should_log(method, path)
    assert not _is_fallback(method, path), f"{method} {path} still logs as a raw path"


def test_the_corporate_action_preview_is_not_recorded() -> None:
    """The debounce case: one 補登 must not write a burst of phantom operations."""
    assert not should_log("POST", "/api/ledgers/corporate-actions/preview")
    # ...while the write it previews IS recorded (the exclusion must not swallow it).
    assert should_log("POST", "/api/ledgers/corporate-actions")
