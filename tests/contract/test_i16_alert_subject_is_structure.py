"""I-16 (DEF-037's residue): no consumer recovers an alert's subject from its id any more.

DEF-037 made ``Alert.scope`` + ``Alert.subject`` required and gave ``alert_events`` a ``scope``
column, and removed the scheduler's ``_alert_symbol``. The sweep for the same shape found five
more places, each fixed here:

1. ``web/alerts.js::symbolOf`` fell back to the id's ``rule:`` suffix — so the bell listed
   ``fx_drift``'s ACCOUNT, ``sector_weight``'s SECTOR and ``currency_weight``'s CURRENCY as
   「檔」. The wire now carries ``scope`` / ``subject`` (``api/wire.py::alerts_wire``, additive)
   and the bell reads them; a non-symbol group reads 「帳戶／產業／幣別：…」 (accounts via
   pdNames). ``fx_drift``'s title also named the account by ``accounts.name`` — the English
   label — and now carries the account token like every backend sentence.
2. ``ops/notify.py::format_event`` printed ``alert_events.symbol`` as a ticker (「schwab 匯率
   偏離成本」); it now composes by scope, and an account is named 「帳戶 <id>」 — a push leaves
   through an external channel where no fetch layer resolves the token.
3. ``api/digest_service.py``: ``_drift_symbols`` parsed the id; ``_alerts_today`` put every
   subject into ``symbols``. Both read the structure now.
4. ``api/signals_service.py`` recorded its events without ``scope`` — now ``"symbol"``.
5. ``api/routers/news.py`` wrote the bare ``str(exc)`` into ``job_runs.detail`` — now
   ``scheduler.jobs.failure_detail``.

Scan (2026-09-23): 5 id-suffix / subject-as-symbol sites (alerts.js ×1, digest_service ×2,
notify ×1, signals_service ×1) + 1 raw-exception job detail (news.py) — 6 found, 6 fixed, 0
allowlisted; 3 ``record_event`` call sites, all now pass ``scope=``.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import digest_service as ds
from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.ops import notify
from portfolio_dash.strategy.alerts import Alert

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "portfolio_dash"
NOW = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)


# ------------------------------------------------------------------- the class guards

#: Recovering a subject from an id's ``rule:`` suffix, in any spelling seen so far.
_SUFFIX = re.compile(
    r"\bid\.indexOf\(':'\)|\.id\.(?:split|partition|rpartition)\(\s*['\"]:|"
    r"\.id\[len\(|\bid\.slice\(i \+ 1\)")


def test_no_code_parses_a_subject_out_of_an_alert_id() -> None:
    hits = []
    for path in [*_PKG.rglob("*.py"), *(_ROOT / "web").glob("*.js")]:
        if path.name.endswith(".min.js"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _SUFFIX.search(line):
                hits.append(f"{path.relative_to(_ROOT)}:{n}: {line.strip()}")
    assert not hits, hits


def test_the_suffix_guard_bites() -> None:
    for src in ("const i = id.indexOf(':');", "out.append(a.id[len(prefix):])",
                "rule, _, sym = a.id.partition(':')"):
        assert _SUFFIX.search(src), src


def test_every_recorded_event_says_what_its_subject_is() -> None:
    calls = []
    for path in _PKG.rglob("*.py"):
        if path.name == "alerts_bridge.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("record_event", "record_event_ex")):
                calls.append((path.name, node.lineno,
                              any(k.arg == "scope" for k in node.keywords)))
    assert len(calls) >= 3, calls
    assert all(ok for *_rest, ok in calls), [c for c in calls if not c[2]]


# ------------------------------------------------------------------------- the wire


def test_the_alert_wire_carries_scope_and_subject(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    alerts = api_client.get("/api/alerts").json()["alerts"]
    assert alerts
    for a in alerts:
        assert a["scope"] in ("symbol", "sector", "account", "currency", "portfolio"), a
        assert (a["subject"] is None) == (a["scope"] == "portfolio"), a
    fx = [a for a in alerts if a["rule"] == "fx_drift"]
    for a in fx:
        assert a["scope"] == "account"
        assert a["title"].startswith("{account:" + a["subject"] + "}"), a


# ------------------------------------------------------------------------- the push


@pytest.mark.parametrize(("subject", "scope", "lead"), [
    ("2330", "symbol", "2330"),
    ("2330", None, "2330"),                         # a legacy row: read as it always was
    ("schwab", "account", "帳戶 schwab"),
    ("Information Technology", "sector", "產業 Information Technology"),
    ("USD", "currency", "幣別 USD"),
])
def test_the_push_names_the_subject_by_what_it_is(
    subject: str, scope: str | None, lead: str
) -> None:
    title, body, _sev = notify.format_event("fx_drift", subject, scope=scope, linked=True)
    assert f"· {lead} " in title, title
    assert body.startswith(f"{lead}：觸發"), body


def test_the_dispatcher_hands_the_scope_to_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from portfolio_dash.ops.notify_dispatch import dispatch_notifications

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    alerts_bridge.ensure_tables(c)
    alerts_bridge.record_event(c, rule_id="fx_drift", symbol="schwab", now=NOW,
                               scope="account")
    sent: list[tuple[str, str]] = []

    def _sender(channels: Any, title: str, body: str, sev: str, link: Any) -> dict[str, str]:
        sent.append((title, body))
        return {"x": "ok"}

    monkeypatch.setattr(notify, "load_config", lambda _c: notify.NotifyConfig())
    monkeypatch.setattr(notify, "build_enabled_channels", lambda _cfg: [object()])
    dispatch_notifications(c, now=NOW, sender=_sender)
    assert sent and "帳戶 schwab" in sent[0][0] and sent[0][1].startswith("帳戶 schwab：")


# ----------------------------------------------------------------------- the digest


def test_only_symbol_subjects_reach_the_digest_symbol_lists() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    alerts_bridge.ensure_tables(c)
    c.execute("CREATE TABLE IF NOT EXISTS llm_models (alias TEXT)")
    for rule, subject, scope in [("single_weight", "2330", "symbol"),
                                 ("fx_drift", "schwab", "account"),
                                 ("sector_weight", "Information Technology", "sector")]:
        alerts_bridge.record_event(c, rule_id=rule, symbol=subject, now=NOW, scope=scope)
    today = {g["rule_id"]: g["symbols"] for g in ds._alerts_today(c, NOW)}
    assert today == {"single_weight": ["2330"], "fx_drift": [], "sector_weight": []}

    drift = [
        Alert(id="rebalance_drift:2330", sev="risk", rule="rebalance_drift", title="",
              detail="", scope="symbol", subject="2330"),
        Alert(id="rebalance_drift", sev="risk", rule="rebalance_drift", title="", detail="",
              scope="portfolio"),
    ]
    assert ds._drift_symbols(drift) == ["2330"]


# ------------------------------------------------------------------------- the news job


def test_the_news_worker_writes_the_schedulers_failure_sentence() -> None:
    src = (_PKG / "api/routers/news.py").read_text(encoding="utf-8")
    assert "failure_detail(exc)" in src
    assert not re.search(r"detail,\s*status\s*=\s*str\(exc\)", src)


# --------------------------------------------------------------------------- the bell


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
}
const NAMES = { schwab: '嘉信 Schwab', moomoo_my: 'Moomoo MY' };
const window = { pdNames: { account: (id) => NAMES[id] || id } };
const start = src.indexOf('const SCOPE_ZH');
eval(src.slice(start, src.indexOf(';', start) + 1).replace('const ', 'var '));
eval(grab('mapAlertHref') + grab('symbolOf') + grab('subjectName') + grab('ruleNoun')
     + grab('groupDetail'));
const fx = [
  { id: 'fx_drift:schwab', rule: 'fx_drift', scope: 'account', subject: 'schwab',
    title: '嘉信 Schwab 匯率偏離成本', href: 'cash.html#fx' },
  { id: 'fx_drift:moomoo_my', rule: 'fx_drift', scope: 'account', subject: 'moomoo_my',
    title: 'Moomoo MY 匯率偏離成本', href: 'cash.html#fx' },
];
const sec = [{ id: 'sector_weight:Tech', rule: 'sector_weight', scope: 'sector',
               subject: 'Tech', title: 'Tech 產業權重偏高' },
             { id: 'sector_weight:Fin', rule: 'sector_weight', scope: 'sector',
               subject: 'Fin', title: 'Fin 產業權重偏高' }];
const sym = [{ id: 'stale_price:2330', rule: 'stale_price', scope: 'symbol', subject: '2330',
               title: '2330 報價過期', href: '/symbol/2330' },
             { id: 'stale_price:AAPL', rule: 'stale_price', scope: 'symbol', subject: 'AAPL',
               title: 'AAPL 報價過期', href: '/symbol/AAPL' }];
const legacy = { id: 'fx_drift:schwab', rule: 'fx_drift', title: 'x', href: 'cash.html#fx' };
process.stdout.write(JSON.stringify({
  fx: [groupDetail(fx), ruleNoun(fx[0]), fx.map(symbolOf)],
  sec: [groupDetail(sec), ruleNoun(sec[0])],
  sym: [groupDetail(sym), ruleNoun(sym[0])],
  legacy: symbolOf(legacy),
}));
"""


def test_the_bell_groups_by_what_each_alert_is_about(tmp_path: Path) -> None:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run([str(node), str(harness), str(_ROOT / "web/alerts.js")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["fx"] == ["帳戶：嘉信 Schwab、Moomoo MY", "匯率偏離成本", ["", ""]]
    assert out["sec"] == ["產業：Tech、Fin", "產業權重偏高"]
    assert out["sym"] == ["2330、AAPL", "報價過期"]
    assert out["legacy"] == ""          # no scope on the wire: never read off the id
