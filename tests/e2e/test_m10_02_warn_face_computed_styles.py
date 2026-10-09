"""E2E M10-02 tail: the browser paints the warn toast and the partial dot from the CLASS.

The contract test pins the source; this one asks Chromium. A bare ``.toast.toast-warn``
probe with no inline style must already compute to ``--amber`` text and the 0.5-alpha amber
border (red before the fix: no rule → the probe inherits the page text colour), the real
``window.toast(…, 'warn')`` element must carry NO ``style`` attribute and compute to exactly
the probe's values, and a job whose last run is ``partial`` must get a ``.run-dot.dot-warn``
whose background is that same amber — with no inline background.
"""

import json

from playwright.sync_api import Page, Route

_JOB = {"id": "probe_job", "desc": "probe", "cron": "0 9 * * *", "tz": "Asia/Taipei",
        "enabled": True, "next": None,
        "last": {"status": "partial", "at": "2026-09-01T08:00:00+08:00",
                 "detail": "probe", "duration_s": 1}}


def _partial_jobs(route: Route) -> None:
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"jobs": [_JOB]}))


def test_warn_toast_and_partial_dot_take_their_colour_from_the_class(
    live_server: str, browser_page: Page
) -> None:
    page = browser_page
    page.goto(live_server + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body")
    got = page.evaluate("""() => {
        const amberProbe = document.createElement('span');
        amberProbe.style.color = 'var(--amber)';
        document.body.appendChild(amberProbe);
        const amber = getComputedStyle(amberProbe).color;
        amberProbe.remove();

        const probe = document.createElement('div');
        probe.className = 'toast toast-warn';
        document.body.appendChild(probe);
        const pc = getComputedStyle(probe);
        const fromClass = { color: pc.color, borderColor: pc.borderColor };
        probe.remove();

        window.toast('probe', 'warn', 'sub');
        const t = document.querySelector('.toast-host .toast-warn');
        const cs = getComputedStyle(t);
        return { amber, fromClass, styleAttr: t.getAttribute('style'),
                 real: { color: cs.color, borderColor: cs.borderColor } };
    }""")
    assert got["fromClass"]["color"] == got["amber"], (
        f"a bare .toast-warn does not compute to --amber: {got!r}")
    assert got["fromClass"]["borderColor"] == "rgba(217, 161, 63, 0.5)", got
    assert not got["styleAttr"], f"the warn toast still carries an inline style: {got!r}"
    assert got["real"] == got["fromClass"], got

    page.route("**/api/scheduler/jobs", _partial_jobs)
    try:
        page.goto(live_server + "/settings.html", wait_until="load")
        # the 排程中心 tab is not the default tab, so the row is display:none — attached is
        # enough: getComputedStyle resolves the cascade for a hidden element too.
        page.wait_for_selector("#jobs-body tr .run-dot", state="attached")
        dot = page.evaluate("""() => {
            const d = document.querySelector('#jobs-body tr .run-dot');
            const amberProbe = document.createElement('span');
            amberProbe.style.color = 'var(--amber)';
            document.body.appendChild(amberProbe);
            const amber = getComputedStyle(amberProbe).color;
            amberProbe.remove();
            return { amber, className: d.className, styleAttr: d.getAttribute('style'),
                     background: getComputedStyle(d).backgroundColor };
        }""")
    finally:
        page.unroute("**/api/scheduler/jobs")   # session page: never leak a stub
    assert "dot-warn" in dot["className"].split(), dot
    assert not dot["styleAttr"], f"the partial dot still carries an inline style: {dot!r}"
    assert dot["background"] == dot["amber"], dot
