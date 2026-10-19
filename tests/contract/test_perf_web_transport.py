"""PERF spec 2026-10-18 §13 a–d (plan §6.3, package F): the web/api.js transport, driven for
real inside a node ``vm`` (Playwright's bundled node, as ``test_r5_api_js_error_message.py``
does) with a stubbed ``fetch`` and fake timers:

* a ``{background: true}`` request stays out of ``pd-net`` (the top progress bar) and never
  arms the 15 s timer — the bell, status polls and pre-fetches use it;
* identical concurrent GETs share ONE fetch, but every caller parses its own copy;
* a write closes the shares, so a GET issued after it never receives an answer from before;
* ``pd-mutated`` follows exactly the successful non-GETs (the settlement prompt's one seam);
* a tracked request still pending after 15 s raises ``slow`` (「仍在計算中…」), nothing aborts;
* ``held()`` parks an early failure of a parallel boot read until it is awaited.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');

const events = [];
const timers = [];
let fetches = [];
const sandbox = {
  window: { location: { pathname: '/index.html', replace: function () {} } },
  document: { dispatchEvent: function (e) { events.push({ type: e.type, detail: e.detail }); },
              createElement: function () { return {}; }, body: { appendChild: function () {} } },
  CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
  AbortController: function () { this.signal = {}; this.abort = function () {}; },
  URLSearchParams: URLSearchParams, URL: URL, console: console,
  setTimeout: function (fn, ms) {
    timers.push({ fn: fn, ms: ms, live: true });
    return timers.length - 1;
  },
  clearTimeout: function (id) { if (timers[id]) timers[id].live = false; },
  fetch: function (url, init) {
    let resolve;
    const p = new Promise(function (r) { resolve = r; });
    fetches.push({ url: url, method: (init && init.method) || 'GET', resolve: resolve });
    return p;
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const api = sandbox.window.pdApi;

function answer(i, status, body) {
  fetches[i].resolve({ ok: status >= 200 && status < 300, status: status, statusText: 'x',
    headers: { get: function () { return ''; } },
    text: async function () { return body; } });
}
const net = () => events.filter(function (e) { return e.type === 'pd-net'; });
const mutated = () => events.filter(function (e) { return e.type === 'pd-mutated'; });
const tick = () => new Promise(function (r) { setImmediate(r); });

(async function () {
  const out = {};

  // (a) background: no pd-net at all
  events.length = 0; fetches = [];
  const bg = api.get('/api/settlement/status', null, { background: true });
  answer(0, 200, '{"state":"clean"}');
  await bg;
  out.background_net_events = net().length;

  // (b) identical concurrent GETs: one fetch, two separate objects
  events.length = 0; fetches = [];
  const g1 = api.get('/api/x', { a: 1 });
  const g2 = api.get('/api/x', { a: 1 });
  const g3 = api.get('/api/x', { a: 2 });
  out.shared_fetches = fetches.length;
  answer(0, 200, '{"rows":[1]}');
  answer(1, 200, '{"rows":[2]}');
  const r1 = await g1; const r2 = await g2; await g3;
  out.same_body = JSON.stringify(r1) === JSON.stringify(r2);
  out.same_object = r1 === r2;
  out.pending_after = api.pending();

  // a write closes the shares: a GET issued after it is a new fetch
  events.length = 0; fetches = [];
  const before = api.get('/api/y');
  const w = api.post('/api/z', { v: 1 });
  const after = api.get('/api/y');
  out.fetches_around_write = fetches.map(function (f) { return f.method + ' ' + f.url; });
  answer(0, 200, '{}'); answer(1, 200, '{"ok":true}'); answer(2, 200, '{}');
  await before; await w; await after;

  // (d) pd-mutated: after the successful POST only (not the GETs)
  out.mutated = mutated().map(function (e) { return e.detail.method + ' ' + e.detail.path; });

  // a failed write announces nothing
  events.length = 0; fetches = [];
  const bad = api.put('/api/z', {});
  answer(0, 422, '{"error":{"code":"validation_error","message":"no"}}');
  try { await bad; } catch (e) { out.failed_write_code = e.code; }
  out.mutated_after_failure = mutated().length;

  // (c) slow: a tracked request pending past 15 s raises `slow`, its end clears it
  events.length = 0; fetches = [];
  timers.length = 0;
  const slow = api.get('/api/slow');
  const t = timers.find(function (x) { return x.ms === 15000; });
  out.slow_timer = !!t;
  t.fn();
  out.slow_raised = net().some(function (e) { return e.detail.slow === true; });
  answer(0, 200, '{}');
  await slow;
  const last = net()[net().length - 1];
  out.slow_cleared = last.detail.slow === false && last.detail.pending === 0;

  // a background request never arms the slow timer
  timers.length = 0; fetches = [];
  const bg2 = api.get('/api/settlement/status', null, { background: true });
  out.background_timers = timers.filter(function (x) { return x.ms === 15000; }).length;
  answer(0, 200, '{}');
  await bg2;

  // held(): an early failure is parked, then rethrown where it is awaited
  fetches = [];
  const h = api.held(api.get('/api/early'));
  answer(0, 500, '{"error":{"code":"internal_error","message":"boom"}}');
  await tick(); await tick();
  try { await h(); out.held = 'resolved'; } catch (e) { out.held = e.code; }

  process.stdout.write(JSON.stringify(out));
})();
"""


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


@pytest.fixture(scope="module")
def transport(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path_factory.mktemp("transport") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(harness), str(_WEB / "api.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    out: dict[str, object] = json.loads(proc.stdout)
    return out


def test_a_background_request_never_moves_the_top_bar(transport: dict[str, object]) -> None:
    assert transport["background_net_events"] == 0
    assert transport["background_timers"] == 0


def test_identical_concurrent_gets_share_one_fetch_but_not_one_object(
    transport: dict[str, object],
) -> None:
    assert transport["shared_fetches"] == 2       # /api/x?a=1 once, /api/x?a=2 once
    assert transport["same_body"] is True
    assert transport["same_object"] is False      # each caller parses its own copy
    assert transport["pending_after"] == 0


def test_a_write_closes_the_shares_and_announces_itself(transport: dict[str, object]) -> None:
    assert transport["fetches_around_write"] == ["GET /api/y", "POST /api/z", "GET /api/y"]
    assert transport["mutated"] == ["POST /api/z"]
    assert transport["failed_write_code"] == "validation_error"
    assert transport["mutated_after_failure"] == 0


def test_a_request_pending_for_15_s_says_so_and_is_not_aborted(
    transport: dict[str, object],
) -> None:
    assert transport["slow_timer"] is True
    assert transport["slow_raised"] is True
    assert transport["slow_cleared"] is True


def test_held_parks_an_early_failure_until_it_is_awaited(transport: dict[str, object]) -> None:
    assert transport["held"] == "internal_error"
