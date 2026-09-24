"""DEF-059 (owner ruling 2026-09-24): a ``mode: all_registered`` insight task leaves ARCHIVED
instruments out — and every surface that counts or runs that universe agrees.

R3 developer decision ⑤: ``api/insight_service.py::_all_registered_symbols`` returned
``sorted({i.symbol for i in list_instruments(conn)})`` — every row, archived included
(``list_instruments`` leaves the archived filter to its caller), so a task opted into 「含觀察
標的」 kept paying for a card on a symbol the owner had stopped tracking. Ruling: exclude it,
with the symbol count, the cost estimate, the dry-run pre-check and the actual run agreeing.

This file drives each of those through its REAL door on one ledger: the pipeline card's
「N 檔標的」 (``GET /api/insight-tasks/status``), the saved-task and draft preflights (the
same resolver), the actual run (``run_for_id`` → cards created), and the page's own estimate
(``web/pipeline.js::ppUniverseSource`` executed under node on the REAL ``/api/dashboard`` +
``/api/instruments`` payloads). Archive one watch symbol → every one of them drops by one.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import insight_service
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW

_ROOT = Path(__file__).resolve().parents[2]


def _fake_llm(prompt: str, schema: type[InsightCard], **kw: object
              ) -> llm_mod.StructuredCompletion[InsightCard]:
    """The LLM seam, faked at ``complete_structured_meta`` (no provider, no litellm): every
    call returns one narrative card, so cards created == LLM targets + zero-LLM anomalies."""
    return llm_mod.StructuredCompletion[InsightCard](
        value=InsightCard(title="洞察", summary="s", body_md="b"), model="m",
        cost=Decimal("0.001"))


@pytest.fixture
def ledger(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
           ) -> Iterator[sqlite3.Connection]:
    """The golden ledger + two WATCH-ONLY symbols (TSLA, NFLX) + a funded LLM (mocked)."""
    for sym, name in (("TSLA", "Tesla"), ("NFLX", "Netflix")):
        upsert_instrument(golden_db, Instrument(
            symbol=sym, market=Market.US, quote_ccy=Currency.USD, sector="Tech", name=name))
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    monkeypatch.setattr(llm_mod, "complete_structured_meta", _fake_llm)
    yield golden_db


def _task(api_client: TestClient) -> int:
    sp = api_client.post("/api/strategy-prompts",
                         json={"name": "S", "body": "{{symbol}} 觀察"}).json()
    it = api_client.post("/api/insight-types", json={
        "name": "含觀察", "scope": "per_symbol", "strategy_ids": [sp["id"]],
        "universe": {"mode": "all_registered"},
    }).json()
    return int(it["id"])


def _held(conn: sqlite3.Connection) -> set[str]:
    return insight_service.held_in_book(build_dashboard(conn, now=GOLDEN_NOW,
                                                        reporting=Currency.TWD))


def _status_head(api_client: TestClient, tid: int) -> str:
    task = next(t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
                if t["id"] == tid)
    return str(task["nodes"]["input"]["text"])


def _universes(conn: sqlite3.Connection, tid: int) -> tuple[list[str], list[str]]:
    """(saved task resolver, draft preflight resolver) — the two the preflight reads."""
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    return (insight_service._resolve_universe(conn, it, data),
            insight_service._resolve_universe_raw(conn, {"mode": "all_registered"}, data))


def _page_universe(api_client: TestClient, tmp: Path) -> list[str]:
    """What the wizard / 編輯標的 dialog count (and the ~$ estimate multiplies): pipeline.js's
    own ``ppUniverseSymbols({mode:'all_registered'}, ppUniverseSource(dash, instruments))``,
    run under node on the REAL API payloads."""
    import playwright

    node = Path(playwright.__file__).parent / "driver" / "node.exe"
    if not node.exists():
        node = Path(playwright.__file__).parent / "driver" / "node"
    if not node.exists():
        pytest.skip("Playwright's bundled node is not installed in this venv")
    payload = {"dash": api_client.get("/api/dashboard").json(),
               "instruments": api_client.get("/api/instruments").json()}
    harness = r"""
const fs = require('fs'); const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const node = () => ({ className: '', dataset: {}, style: {}, appendChild() {},
                      addEventListener() {}, classList: { add() {}, remove() {} } });
const sb = { document: { readyState: 'complete', querySelector: () => null,
             querySelectorAll: () => [], createElement: node, addEventListener() {} },
             requestAnimationFrame: (f) => f(), console: console };
sb.window = sb; vm.createContext(sb); vm.runInContext(src, sb);
const s = sb.ppUniverseSource(input.dash, input.instruments);
process.stdout.write(JSON.stringify(sb.ppUniverseSymbols({ mode: 'all_registered' }, s)));
"""
    (tmp / "h.js").write_text(harness, encoding="utf-8")
    (tmp / "in.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(tmp / "h.js"), str(_ROOT / "web" / "pipeline.js"), str(tmp / "in.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    out: list[str] = json.loads(proc.stdout)
    return out


def test_archiving_a_watch_symbol_drops_it_from_every_all_registered_surface(
    api_client: TestClient, ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    tid = _task(api_client)
    held = _held(ledger)
    before = sorted(held | {"TSLA", "NFLX"})

    # --- before: all four surfaces see held + both watch symbols -------------------------
    assert _status_head(api_client, tid) == f"{len(before)} 檔標的"
    assert _universes(ledger, tid) == (before, before)
    assert _page_universe(api_client, tmp_path) == before
    run1 = insight_service.run_for_id(ledger, tid, now=GOLDEN_NOW)
    assert run1.cards_created == len(before)

    # --- archive NFLX (the owner's 停止追蹤) ------------------------------------------------
    r = api_client.put("/api/instruments/NFLX/archive", json={"archived": True})
    assert r.status_code == 200, r.text
    after = sorted(held | {"TSLA"})
    assert len(after) == len(before) - 1
    assert _status_head(api_client, tid) == f"{len(after)} 檔標的"
    assert _universes(ledger, tid) == (after, after)
    assert _page_universe(api_client, tmp_path) == after
    # the next day's run (a new cache key) makes one card fewer — none for NFLX
    run2 = insight_service.run_for_id(ledger, tid, now=GOLDEN_NOW + timedelta(days=1))
    assert run2.cards_created == len(after)
    day2 = ledger.execute(
        "SELECT symbol FROM insights WHERE insight_type_id = ? AND created_at = ?",
        (tid, (GOLDEN_NOW + timedelta(days=1)).isoformat()),
    ).fetchall()
    assert "NFLX" not in {row["symbol"] for row in day2}

    # --- restore (還原) brings it back: the rule is the flag, not a deletion ---------------
    api_client.put("/api/instruments/NFLX/archive", json={"archived": False})
    assert _universes(ledger, tid) == (before, before)


def test_a_held_symbol_is_never_dropped_and_other_modes_are_untouched(
    api_client: TestClient, ledger: sqlite3.Connection
) -> None:
    """Pre-existing-state variants: a held symbol whose row reads archived (the invariant is
    enforced at booking, so only direct SQL can produce it) stays in — ``all_registered`` is
    never narrower than ``all``; a CUSTOM list naming an archived symbol keeps it (the owner
    named it); ``mode:all`` is holdings only either way."""
    held = sorted(_held(ledger))
    some_held = held[0]
    ledger.execute("UPDATE instruments SET archived = 1 WHERE symbol IN (?, 'NFLX')",
                   (some_held,))
    ledger.commit()
    data = build_dashboard(ledger, now=GOLDEN_NOW, reporting=Currency.TWD)
    reg = insight_service._resolve_universe_raw(ledger, {"mode": "all_registered"}, data)
    assert some_held in reg and "NFLX" not in reg and "TSLA" in reg
    assert set(held) <= set(reg)
    assert insight_service._resolve_universe_raw(ledger, {"mode": "all"}, data) == held
    custom = insight_service._resolve_universe_raw(
        ledger, {"mode": "custom", "symbols": ["NFLX", "TSLA"]}, data)
    assert custom == ["NFLX", "TSLA"]
