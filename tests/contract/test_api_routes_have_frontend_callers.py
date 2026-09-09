"""Every `/api/*` route has a browser caller, or an entry here that says why it does not.

The 2026-09-09 site architecture map matched all 190 routes against `web/` and found ten
with no caller at all (its finding D-10). None of them is broken; each is a surface the UI
never grew or no longer uses. `test_export_endpoints_have_callers.py` already holds the
export router to this rule with an EMPTY allowlist; this file widens the rule to the whole
API and keeps the ten as *decisions* — an allowlist entry with a stated reason — so that:

* a NEW route nobody calls fails the build the day it lands (the drift this map caught), and
* an allowlisted route that later gains a caller fails too, so the entry is deleted with the
  gap instead of outliving it ("an exception nobody uses is an exception nobody notices").

Matching is structural — router paths vs the JS source — because the failure mode is an
endpoint nobody calls, and nothing that exercises an endpoint can see that. Two passes:

1. **Direct, verb-aware.** The first argument of every `pdApi.<verb>(` call, read as a
   literal, a template literal, or a concatenation. A dynamic piece is one wildcard
   segment (`'/api/insight-' + kind + '/official-pack'` reaches both twins; `'/api/x/' + id`
   reaches `/api/x/{id}`); a piece glued onto a path with no slash is a query string.
2. **Indirect, verb-agnostic.** Any OTHER string literal mentioning `/api/` — a path handed
   to a helper (`loadOne('tx', '/api/ledgers/transactions', …)`, `csvExportButton(…)`) or
   stored in a table (`{ path: '/api/export/…' }`). Such a literal reaches the route it
   names and every route that extends it with parameters, whatever the verb.

Pass 2 is deliberately generous: it exists so that a helper-routed call never reads as
"nobody calls this", and the price is that it cannot tell a PUT from a DELETE on the same
base path. That is fine for the question this file asks — is the surface reachable from the
browser at all — and the ten allowlisted routes were checked to stay unreached under both
passes (`test_allowlisted_routes_are_still_uncalled`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portfolio_dash.api.app import create_app

_REPO = Path(__file__).resolve().parents[2]
_WEB = _REPO / "web"

#: Routes intentionally without a browser caller, with the reason. Adding an entry is a
#: decision to record here; removing the gap (building the UI, or deleting the route) must
#: remove the entry, and the stale-entry test below makes sure it does.
_NO_CALLER_ALLOWED: dict[tuple[str, str], str] = {
    ("POST", "/api/instruments/quick"):
        "superseded by the quick-add dialog (FU-D23: GET /api/instruments/lookup + POST "
        "/api/instruments); kept as the scriptable one-step add, contract-tested, action-logged",
    ("GET", "/api/news-prompt"):
        "user-editable news-organizer prompt (FU-D30 registry, storage news_prompt_config) whose "
        "settings UI was never built — backlog; the seed is applied by the news service",
    ("PUT", "/api/news-prompt"): "see GET /api/news-prompt",
    ("POST", "/api/news-prompt/reset"): "see GET /api/news-prompt",
    ("GET", "/api/signals"):
        "the whole-universe collection view for scripts and tests; the UI reads one symbol at a "
        "time (GET /api/signals/{symbol} from the detail drawer)",
    ("GET", "/api/calibrations/{calibration_id}/samples"):
        "spec 4.7 miss-evaluation samples behind a calibration version; the pipeline drawer "
        "shows the version chain, not the samples",
    ("PUT", "/api/insight-types/{insight_type_id}/active-calibration"):
        "spec 4.7 explicit re-pin of the active calibration; the drawer only archives versions "
        "(POST /api/calibrations/{id}/archive) and the newest live one is active by default",
    ("PUT", "/api/insight-tasks/{insight_type_id}/active-calibration"):
        "twin of the insight-types route above",
    ("DELETE", "/api/insight-types/{insight_type_id}/schedule"):
        "schedule removal; the drawer's 排程設定 modal only sets a cron (POST .../schedule) and "
        "disables a task through PUT enabled=false instead",
    ("DELETE", "/api/insight-tasks/{insight_type_id}/schedule"):
        "twin of the insight-types route above",
}

_VERB = {"get": "GET", "post": "POST", "put": "PUT", "del": "DELETE", "download": None}
_CALL = re.compile(r"(?:pdApi|window\.pdApi|api)\.(get|post|put|del|download)\(")
_LITERAL = re.compile(r"""(['"`])((?:(?!\1)[^\n])*?/api/(?:(?!\1)[^\n])*?)\1""")
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def _first_arg(src: str, i: int) -> tuple[int, int]:
    """(start, end) of the first argument beginning at index *i* (just after the '(')."""
    depth = 0
    j = i
    quote: str | None = None
    while j < len(src):
        ch = src[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
            elif quote == "`" and ch == "$" and src[j + 1 : j + 2] == "{":
                k, d = j + 2, 1
                while k < len(src) and d:
                    d += {"{": 1, "}": -1}.get(src[k], 0)
                    k += 1
                j = k
                continue
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i, j
            depth -= 1
        elif ch == "," and depth == 0:
            return i, j
        j += 1
    return i, j


def _pattern(expr: str) -> str | None:
    """A path PATTERN from a call's first argument; `X` is one dynamic segment.

    None when the argument carries no literal at all (a fully dynamic path).
    """
    e = expr.strip()
    m = re.match(r"^`(.*)`$", e, re.S)
    if m:
        out = re.sub(r"\$\{[^}]*\}", "X", m.group(1))
    else:
        out, saw_literal = "", False
        for part in re.split(r"\s*\+\s*", e):
            part = part.strip()
            lm = re.match(r"^(['\"])(.*)\1$", part, re.S)
            if lm:
                out += lm.group(2)
                saw_literal = True
            elif part:
                out += "X"
        if not saw_literal:
            return None
    out = out.split("?")[0]
    # a dynamic piece glued onto a finished path with no slash is a query string, not a segment
    out = re.sub(r"(?<=[^/X])X$", "", out)
    if not out.startswith("/"):
        out = "/" + out
    return out


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def _twin(path: str) -> str:
    return path.replace("/api/insight-types", "/api/insight-tasks")


def _matches(route: str, pattern: str, *, prefix_ok: bool) -> bool:
    """Does *pattern* reach *route*?  Route params and X segments are wildcards; with
    *prefix_ok* a pattern that stops right before the route's parameters also counts."""
    rs, ps = _segments(_twin(route)), _segments(_twin(pattern))
    if len(ps) > len(rs):
        return False
    for r, p in zip(rs, ps, strict=False):
        if r.startswith("{") or "X" in p or r == p:
            continue
        return False
    rest = rs[len(ps):]
    if not rest:
        return True
    if pattern.endswith("/") and len(rest) == 1 and rest[0].startswith("{"):
        return True  # '/api/x/' + id, where the id lived outside the first argument
    return prefix_ok and all(s.startswith("{") for s in rest)


def _sources(web: Path) -> list[str]:
    out = [p.read_text(encoding="utf-8") for p in sorted(web.glob("*.js"))
           if p.name != "echarts.min.js"]
    for html in sorted(web.glob("*.html")):
        out.extend(m.group(1) for m in _INLINE_SCRIPT.finditer(html.read_text(encoding="utf-8")))
    return out


def _calls_and_literals(
    sources: list[str],
) -> tuple[list[tuple[str | None, str]], list[str]]:
    """Pass 1: (verb, pattern) per direct call.  Pass 2: every other /api/ literal."""
    calls: list[tuple[str | None, str]] = []
    literals: list[str] = []
    for src in sources:
        spans: list[tuple[int, int]] = []
        for m in _CALL.finditer(src):
            a, b = _first_arg(src, m.end())
            spans.append((a, b))
            p = _pattern(src[a:b])
            if p is not None:
                calls.append((_VERB[m.group(1)], p))
        for lm in _LITERAL.finditer(src):
            if any(a <= lm.start(2) < b for a, b in spans):
                continue
            p = _pattern(lm.group(0))
            if p is not None:
                literals.append(p)
    return calls, literals


def _reached(
    routes: set[tuple[str, str]],
    calls: list[tuple[str | None, str]],
    literals: list[str],
) -> set[tuple[str, str]]:
    hit: set[tuple[str, str]] = set()
    for method, path in routes:
        direct = any(
            (verb is None or verb == method) and _matches(path, p, prefix_ok=False)
            for verb, p in calls
        )
        indirect = any(_matches(path, lit, prefix_ok=True) for lit in literals)
        if direct or indirect:
            hit.add((method, path))
    return hit


def _api_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for r in create_app().routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", "")
        if methods and path.startswith("/api"):
            routes.update((m, path) for m in methods - {"HEAD", "OPTIONS"})
    return routes


def test_every_api_route_has_a_frontend_caller_or_a_stated_reason() -> None:
    routes = _api_routes()
    reached = _reached(routes, *_calls_and_literals(_sources(_WEB)))
    silent = sorted(routes - reached - set(_NO_CALLER_ALLOWED))
    assert not silent, (
        "routes no browser code reaches and no _NO_CALLER_ALLOWED entry explains: "
        f"{silent}. Wire the UI, delete the route, or record WHY it is API-only."
    )


def test_allowlisted_routes_are_still_uncalled() -> None:
    """The presence half: an entry must not outlive the gap it records."""
    routes = _api_routes()
    reached = _reached(routes, *_calls_and_literals(_sources(_WEB)))
    unknown = sorted(set(_NO_CALLER_ALLOWED) - routes)
    assert not unknown, f"_NO_CALLER_ALLOWED names routes that no longer exist: {unknown}"
    stale = sorted(set(_NO_CALLER_ALLOWED) & reached)
    assert not stale, f"these routes gained a caller — delete their allowlist entry: {stale}"


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ("'/api/ledgers/transactions/' + id", "/api/ledgers/transactions/X"),
        ("'/api/insight-tasks/' + t.id + '/schedule'", "/api/insight-tasks/X/schedule"),
        ("'/api/insight-' + kind + '/official-pack'", "/api/insight-X/official-pack"),
        ("`/api/instruments/${sym}/archive`", "/api/instruments/X/archive"),
        ("'/api/cash/acq-rate?account_id=' + a", "/api/cash/acq-rate"),
        ("'/api/news' + qs()", "/api/news"),
        ("url", None),
    ],
)
def test_the_pattern_reader_handles_every_call_shape(expr: str, want: str | None) -> None:
    assert _pattern(expr) == want


def test_the_matcher_can_actually_fail(tmp_path: Path) -> None:
    """Detection power, both passes: a concatenated call reaches its param route, a
    helper-routed literal reaches the routes it names and extends, a verb mismatch does
    NOT count as direct, and a route nothing names stays unreached."""
    (tmp_path / "x.js").write_text(
        "pdApi.put('/api/ledgers/transactions/' + id, body);\n"
        "api.get(`/api/signals/${sym}`);\n"
        "pdApi.post('/api/insight-tasks/' + t.id + '/schedule', { cron: c });\n"
        "loadOne('div', '/api/ledgers/dividends', render);\n",
        encoding="utf-8",
    )
    (tmp_path / "page.html").write_text(
        "<script src=\"x.js\"></script><script>pdApi.post('/api/auth/login', f)</script>",
        encoding="utf-8",
    )
    routes = {
        ("PUT", "/api/ledgers/transactions/{txn_id}"),
        ("GET", "/api/signals/{symbol}"),
        ("GET", "/api/signals"),
        ("POST", "/api/insight-types/{insight_type_id}/schedule"),
        ("DELETE", "/api/insight-types/{insight_type_id}/schedule"),
        ("GET", "/api/ledgers/dividends"),
        ("DELETE", "/api/ledgers/dividends/{div_id}"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/news-prompt"),
    }
    assert _reached(routes, *_calls_and_literals(_sources(tmp_path))) == {
        ("PUT", "/api/ledgers/transactions/{txn_id}"),
        ("GET", "/api/signals/{symbol}"),
        ("POST", "/api/insight-types/{insight_type_id}/schedule"),  # via the twin
        ("GET", "/api/ledgers/dividends"),
        ("DELETE", "/api/ledgers/dividends/{div_id}"),  # helper literal, verb-agnostic
        ("POST", "/api/auth/login"),  # inline <script>
    }
