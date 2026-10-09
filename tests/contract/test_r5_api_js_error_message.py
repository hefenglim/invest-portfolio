"""R5 / QA-25: ``web/api.js`` must not invent a user-facing message.

``_toError`` opened with ``let message = resp.statusText || 'request failed';`` — a value
that is **always truthy**. Every caller in the app writes
``window.toast((err && err.message) || '中文預設', 'fail', err.code)``, so the Chinese
fallback could never be reached: a proxy 502 with an HTML body, a dropped connection, a
gateway timeout — all of them toasted ``Bad Gateway`` / ``Gateway Timeout`` in English, and
89 Chinese fallbacks across 28 files were dead code.

The English is not thrown away; it moves to ``err.statusText``, where diagnostics can read
it and no toast will.

Node comes from Playwright's bundled driver, exactly as
``tests/contract/test_web_js_parses.py`` does it — already a dev dependency, no new package,
and a skip (not a failure) when it is absent.
"""

import json
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_API_JS = _WEB / "api.js"

#: Drives the REAL `web/api.js` inside a `vm` context with a stubbed `fetch`, and prints
#: one JSON record per case describing the PdApiError each response produced.
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');

function response(status, statusText, body) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    statusText: statusText,
    headers: { get: function () { return ''; } },
    text: async function () { return body; },
    json: async function () { return JSON.parse(body); },
  };
}

const CASES = [
  { name: 'envelope_with_message', resp: response(400, 'Bad Request',
      JSON.stringify({ error: { code: 'validation_error',
                              message: '金額必須大於 0', field: 'amount' } })) },
  { name: 'html_error_page', resp: response(502, 'Bad Gateway',
      '<html><body><h1>502 Bad Gateway</h1></body></html>') },
  { name: 'envelope_without_message', resp: response(500, 'Internal Server Error',
      JSON.stringify({ error: { code: 'internal_error' } })) },
  { name: 'empty_body', resp: response(504, 'Gateway Timeout', '') },
];

const sandbox = {
  window: { location: { pathname: '/index.html', replace: function () {} } },
  document: { dispatchEvent: function () {}, createElement: function () { return {}; },
              body: { appendChild: function () {} } },
  CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
  AbortController: function () { this.signal = {}; this.abort = function () {}; },
  URLSearchParams: URLSearchParams,
  URL: URL,
  setTimeout: setTimeout,
  console: console,
  fetch: null,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

// Five REAL fallbacks, copied verbatim out of the pages that hold them. The expression
// `(err && err.message) || '<zh>'` below is character-for-character what those callers run,
// so evaluating it against the error api.js actually produces is the render check.
const SAMPLES = [
  { site: 'cash.js:403', zh: '匯出失敗' },
  { site: 'input.js:1124', zh: '解析失敗' },
  { site: 'inbox.js:49', zh: '操作失敗' },
  { site: 'login.html:93', zh: '登入失敗' },
  { site: 'instruments.js:78', zh: '請稍後再試' },
];

(async function () {
  const out = [];
  for (const c of CASES) {
    sandbox.fetch = async function () { return c.resp; };
    try {
      await sandbox.window.pdApi.get('/api/anything');
      out.push({ name: c.name, threw: false });
    } catch (err) {
      out.push({
        name: c.name, threw: true,
        message: err.message === undefined ? null : err.message,
        truthyMessage: !!err.message,
        code: err.code, status: err.status,
        statusText: err.statusText === undefined ? null : err.statusText,
        rendered: SAMPLES.map(function (s) {
          return { site: s.site, zh: s.zh, shown: (err && err.message) || s.zh };
        }),
      });
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node() -> Path | None:
    import playwright

    node = Path(playwright.__file__).parent / "driver" / "node.exe"
    if node.exists():
        return node
    node = Path(playwright.__file__).parent / "driver" / "node"
    return node if node.exists() else None


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path_factory.mktemp("r5") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(harness), str(_API_JS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    rows = json.loads(proc.stdout)
    return {str(r["name"]): r for r in rows}


def test_every_case_threw_a_structured_error(
    results: dict[str, dict[str, object]],
) -> None:
    """Guard the guard: a harness that silently resolved would pass every assertion below."""
    assert set(results) == {"envelope_with_message", "html_error_page",
                            "envelope_without_message", "empty_body"}
    assert all(r["threw"] for r in results.values()), results


def test_a_server_message_is_still_used(results: dict[str, dict[str, object]]) -> None:
    """The envelope's own zh message must reach the caller untouched — the common path."""
    row = results["envelope_with_message"]
    assert row["message"] == "金額必須大於 0"
    assert row["code"] == "validation_error"


@pytest.mark.parametrize("case", ["html_error_page", "envelope_without_message",
                                  "empty_body"])
def test_no_message_is_invented_when_the_envelope_supplied_none(
    results: dict[str, dict[str, object]], case: str,
) -> None:
    """The QA-25 reproduction: `statusText || 'request failed'` is ALWAYS truthy, so the
    caller's `(err && err.message) || '中文預設'` never reached its Chinese fallback."""
    row = results[case]
    assert not row["truthyMessage"], (
        f"{case}: api.js supplied a message of its own ({row['message']!r}), so every "
        f"caller's Chinese fallback stays dead code")


@pytest.mark.parametrize(("case", "expected"), [
    ("html_error_page", "Bad Gateway"),
    ("empty_body", "Gateway Timeout"),
])
def test_the_english_statustext_is_kept_for_diagnostics(
    results: dict[str, dict[str, object]], case: str, expected: str,
) -> None:
    """Dropped from the toast, not from the error object."""
    assert results[case]["statusText"] == expected


def test_the_code_still_comes_from_the_envelope_when_present(
    results: dict[str, dict[str, object]],
) -> None:
    """`code` drives the toast's own labelling, so it must survive independently."""
    assert results["envelope_without_message"]["code"] == "internal_error"
    assert results["html_error_page"]["code"] == "error"


@pytest.mark.parametrize("case", ["html_error_page", "empty_body"])
def test_a_sample_of_real_callers_now_renders_its_chinese(
    results: dict[str, dict[str, object]], case: str,
) -> None:
    """The point of the change: five fallbacks lifted verbatim from five pages.

    The harness evaluates ``(err && err.message) || '<zh>'`` — the exact expression those
    callers run — against the error ``api.js`` actually produced. Before the fix every one
    of them rendered ``Bad Gateway`` / ``Gateway Timeout``.
    """
    rendered = results[case]["rendered"]
    assert isinstance(rendered, list) and len(rendered) == 5, rendered
    for row in rendered:
        assert row["shown"] == row["zh"], (
            f"{row['site']}: the owner still reads {row['shown']!r} instead of "
            f"{row['zh']!r}")


def test_the_server_message_still_wins_over_a_caller_fallback(
    results: dict[str, dict[str, object]],
) -> None:
    """The other direction: a fallback must not shadow a message the server DID send."""
    rendered = results["envelope_with_message"]["rendered"]
    assert isinstance(rendered, list)
    assert {str(row["shown"]) for row in rendered} == {"金額必須大於 0"}
