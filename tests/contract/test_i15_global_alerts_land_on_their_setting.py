"""I-15 (DEF-038 class, backend half): the two portfolio-wide alerts land ON their setting.

``strategy/alerts.py`` sent ``href="/settings"`` for ``quota_low`` and ``calib_gap``;
``web/alerts.js`` (and ``ops/notify.py``'s push deep link, its mirror) mapped that to bare
``settings.html`` — the default 帳戶與費率 tab, where neither setting lives. DEF-038 fixed the
frontend's own settings links and whitelisted this one as cross-package; the whitelist entry
is gone now and ``test_def038_residual_copy_and_settings_links.py`` guards alerts.js too.

quota_low → ``/settings#llm`` (設定 › AI 模型, where the quota lives); calib_gap →
``/settings#prompts/evolution`` (the 自我進化設定 block). Both mappers keep the hash; the real
``mapAlertHref`` is run in Node and must agree with ``notify._frontend_path`` case for case.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.ops import notify
from portfolio_dash.strategy.alerts import compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES
from tests.strategy.test_alerts import _minimal_data

_WEB = Path(__file__).resolve().parents[2] / "web"
_CASES = ["/settings#llm", "/settings#prompts/evolution", "/settings", "/symbol/2330",
          "/insights", "/pipeline", "settings.html#alerts"]


def test_the_two_global_alerts_name_their_block() -> None:
    alerts = compute_alerts_from(
        _minimal_data(fx=None, calendar=[]), DEFAULT_RULES,
        quota_remaining=Decimal("0"), quota_threshold=Decimal("1"), calib_gap=Decimal("20"))
    by_id = {a.id: a.href for a in alerts}
    assert by_id["quota_low"] == "/settings#llm"
    assert by_id["calib_gap"] == "/settings#prompts/evolution"


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def test_the_bell_and_the_push_map_every_href_the_same_way(tmp_path: Path) -> None:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path / "h.js"
    harness.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const i = src.indexOf('function mapAlertHref(');
let d = 0, j = src.indexOf('{', i), end = -1;
for (let k = j; k < src.length; k++) {
  if (src[k] === '{') d++;
  else if (src[k] === '}') { d--; if (d === 0) { end = k + 1; break; } }
}
eval(src.slice(i, end));
const cases = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(cases.map((h) => mapAlertHref(h).href)));
""", encoding="utf-8")
    proc = subprocess.run([str(node), str(harness), str(_WEB / "alerts.js"), json.dumps(_CASES)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    bell = json.loads(proc.stdout)
    push = [notify._frontend_path(h) for h in _CASES]
    assert bell[:2] == ["settings.html#llm", "settings.html#prompts/evolution"]
    assert bell[2] == "settings.html#alerts"              # legacy bare href: never 帳戶與費率
    assert bell == push
