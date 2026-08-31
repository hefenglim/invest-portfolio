"""The scheduler cost block must speak the ONE canonical wire form (QA-26).

``shared/wire.py``'s :func:`decimal_str` is defined as ``format(value, "f")`` and
documented "NEVER scientific notation". ``api/routers/scheduler.py`` summed an LLM usage
window with ``Decimal`` and then serialized it with plain ``str()``, which emits Python's
scientific form for a small exponent: ``str(Decimal("0.00000003") + Decimal("0.00000006"))``
is ``'9E-8'``.

That is not merely ugly. ``web/format.js`` gates every incoming money string on

    PLAIN_DECIMAL = /^[+-]?(\\d+(\\.\\d*)?|\\.\\d+)$/

and a string that fails it falls through to the ``Number()`` FLOAT path — so the value
silently leaves the exact Decimal-string contract that the whole front/back seam is built
on. The regex is not restated here: it is READ OUT OF ``web/format.js`` so this test cannot
drift from the guard it is asserting against.

Both branches of ``_cost_block`` are covered: the usage-window SUM (where the defect was)
and the ``insight:*`` run-row passthrough (whose value comes from the DB and can carry the
same form, since the insight runner persists it with ``str(cost)``).
"""

import re
import sqlite3
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.shared.wire import decimal_str

_WEB_FORMAT_JS = Path(__file__).resolve().parents[2] / "web" / "format.js"


def _plain_decimal_re() -> re.Pattern[str]:
    """The literal ``PLAIN_DECIMAL`` from ``web/format.js``, translated to Python.

    Read from the file rather than copied, so a change to the frontend guard shows up here
    as a test failure instead of a silent divergence.
    """
    source = _WEB_FORMAT_JS.read_text(encoding="utf-8")
    match = re.search(r"const PLAIN_DECIMAL = /(.+?)/;", source)
    assert match, "PLAIN_DECIMAL is no longer declared in web/format.js"
    body = match.group(1)
    assert body.startswith("^") and body.endswith("$"), body
    return re.compile(body)


def _usage_row(conn: sqlite3.Connection, ts: str, agent: str, cost: str) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?,?,?,?,?,?)",
        (ts, "test-model", agent, 1, 1, cost),
    )


def test_the_source_values_really_do_produce_scientific_notation() -> None:
    """Pin the premise: this is a serialization choice, not an invented scenario."""
    total = Decimal("0.00000003") + Decimal("0.00000006")
    assert str(total) == "9E-8"
    assert decimal_str(total) == "0.00000009"
    plain = _plain_decimal_re()
    assert not plain.fullmatch(str(total))
    assert plain.fullmatch(decimal_str(total))


def test_usage_window_cost_is_plain_decimal_not_scientific(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('news_daily','2026-06-11T06:00:00+08:00',"
        "'2026-06-11T06:10:00+08:00','ok','news: organized 2')"
    )
    _usage_row(golden_db, "2026-06-11T06:02:00+08:00", "news_organize", "0.00000003")
    _usage_row(golden_db, "2026-06-11T06:08:30+08:00", "news_organize", "0.00000006")
    golden_db.commit()

    block = api_client.get("/api/scheduler/status").json()["jobs"]["news_daily"]["last_run"]["cost"]
    assert block is not None and block["source"] == "usage_window"
    assert block["cost_usd"] == "0.00000009", (
        f"the wire carries {block['cost_usd']!r}. shared/wire.py defines the ONE canonical "
        f"Decimal wire form as format(value, 'f') — NEVER scientific notation."
    )
    assert _plain_decimal_re().fullmatch(block["cost_usd"]), (
        f"{block['cost_usd']!r} fails web/format.js's own PLAIN_DECIMAL guard, so the "
        f"frontend drops this money value onto the Number() float path."
    )


def test_insight_run_row_cost_is_plain_decimal_not_scientific(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The ``insight:*`` branch reads its value from the DB — normalise it at the wire too.

    ``llm_insight/generate.py`` persists the run row with ``str(cost)``, so the stored TEXT
    can already be in scientific form. That module is not this wave's to change; the router
    is the endpoint's own last seam and must not emit what the contract forbids.
    """
    golden_db.execute(
        "INSERT INTO schedule_config (job_id, enabled, cron, timezone, kind, payload) "
        "VALUES ('insight:9', 1, '0 9 * * *', 'Asia/Taipei', 'insight', '9')"
    )
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, cost_usd) "
        "VALUES ('insight:9','2026-06-11T09:00:00+08:00',"
        "'2026-06-11T09:00:40+08:00','ok','1 card(s)','9E-8')"
    )
    golden_db.commit()

    block = api_client.get("/api/scheduler/status").json()["jobs"]["insight:9"]["last_run"]["cost"]
    assert block is not None and block["source"] == "run_row"
    assert block["cost_usd"] == "0.00000009", (
        f"the wire carries {block['cost_usd']!r} straight out of the run row; the router "
        f"must serialize it through decimal_str like every other Decimal on the wire."
    )
    assert _plain_decimal_re().fullmatch(block["cost_usd"])
