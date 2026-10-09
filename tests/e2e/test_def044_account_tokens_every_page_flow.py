"""E2E (real server + real browser) — DEF-044: on EVERY page that loads the fetch layer, a
backend ``{account:<id>}`` token reaches the screen as the zh display name.

Five pages (settings / insights / instruments / news / data-center) loaded ``api.js`` without
``names.js``; the verifier saw 排程中心 print 「fx_drift 帳戶 moomoo_my」. This drives each page
in a browser, answers one probe request with a token-bearing body (a 200 and a 422 envelope),
and reads what ``window.pdApi`` hands the page — the same path every real response takes. It
also asserts ``api.js``'s load-time check stayed silent: a page that breaks the order logs
「names.js must be loaded before api.js」, which this test reports by page name.
"""

import json
from pathlib import Path

from playwright.sync_api import Page, Route

_WEB = Path(__file__).resolve().parents[2] / "web"
#: Every page with an api.js tag (the static guard keeps the order; this proves the result).
_PAGES = sorted(p.name for p in _WEB.glob("*.html")
                if 'src="api.js' in p.read_text(encoding="utf-8"))

_OK = json.dumps({"detail": "略過 1 條非個股預警：fx_drift 帳戶 {account:moomoo_my}",
                  "rows": ["{account:tw_broker}", "{account:schwab}"]})
_ERR = json.dumps({"error": {"code": "x", "message": "出金超過 {account:schwab} 的餘額"}})

_PROBE = """
async () => {
  const ok = await window.pdApi.get('/api/__names_probe_ok');
  let msg = null;
  try { await window.pdApi.get('/api/__names_probe_err'); } catch (e) { msg = e.message; }
  return { ok: ok, err: msg };
}
"""


def _answer(route: Route) -> None:
    if route.request.url.endswith("_err"):
        route.fulfill(status=422, content_type="application/json", body=_ERR)
    else:
        route.fulfill(status=200, content_type="application/json", body=_OK)


def test_account_tokens_resolve_on_every_page(live_server: str, fresh_page: Page) -> None:
    page = fresh_page
    load_errors: dict[str, list[str]] = {}
    current = {"name": ""}
    page.on("console", lambda m: load_errors.setdefault(current["name"], []).append(m.text)
            if m.type == "error" and "names.js" in m.text else None)
    page.route("**/api/__names_probe_*", _answer)
    assert len(_PAGES) >= 11, _PAGES   # every page that loads the fetch layer
    wrong: dict[str, object] = {}
    for name in _PAGES:
        current["name"] = name
        page.goto(live_server + "/" + name, wait_until="load")
        page.wait_for_function("() => !!window.pdApi")
        got = page.evaluate(_PROBE)
        want = {"ok": {"detail": "略過 1 條非個股預警：fx_drift 帳戶 Moomoo MY",
                       "rows": ["台灣券商", "嘉信 Schwab"]},
                "err": "出金超過 嘉信 Schwab 的餘額"}
        if got != want:
            wrong[name] = got
    shown = json.dumps(wrong, ensure_ascii=False)
    assert not wrong, f"account tokens degraded to raw ids on: {shown}"
    assert not load_errors, f"api.js reported a missing names.js on: {load_errors}"
