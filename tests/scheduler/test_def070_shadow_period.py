"""DEF-070 (R6, owner ruling ⑧ = A) — ``max_shadows`` caps TASKS in their shadow period.

The functional-test verifier reproduced G-08 on a live server with a frozen clock: the old
cap counted every shadow CARD a task had ever stored (``insights WHERE is_shadow = 1``), so a
per_symbol batch of 9 shadow cards sailed past a cap of 2, the next batch produced none, and
after the first promotion a new version could never be shadow-evaluated again. A portfolio
task (one card per batch) stopped at 2 shadow cards and could never reach ``shadow_batches``.

These scenarios replay that time-advance story at the service seam instead of on a server:
the clock is the ``now=`` every entry point already takes, the LLM is the monkeypatched
``litellm.completion`` every insight test uses, and the database is a real SQLite schema
(bootstrap + pricing + composer + evaluations). The fake model predicts UP when the prompt
carries a calibration layer (marker ``QA-CAL``) and DOWN otherwise — the verifier's fake —
and the market rises, so a calibrated shadow wins and an uncalibrated active misses.

Shadow period (ruling ⑧): the latest live calibration version is not the active one AND that
version has fewer than ``shadow_batches`` scored evaluations. A task in its period shadows
EVERY batch (a per_symbol batch of N cards is ONE slot); entering needs a free slot; a
promotion, archiving the shadow version or the active version catching up ends the period;
old shadow cards never hold a slot; and a version is judged on its OWN evaluations.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
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
from tests.conftest import init_golden_base

TZ = ZoneInfo("Asia/Taipei")
MON = datetime(2026, 9, 7, 14, 30, tzinfo=TZ)  # a Monday: every batch below is a weekday


def _day(n: int) -> datetime:
    """The n-th weekday batch after MON (the scheduler's daily run)."""
    d = MON
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


def _fake_llm(**kw: Any) -> _Resp:
    """The verifier's G-08 fake: a calibration layer in the prompt → UP, otherwise DOWN."""
    text = "".join(str(m.get("content")) for m in kw["messages"])
    direction = "up" if "QA-CAL" in text else "down"
    return _Resp(json.dumps({
        "title": f"預測{direction}", "summary": "s", "body_md": "b", "tags": [],
        "confidence": 70,
        "prediction": {"metric": "price_change", "direction": direction,
                       "target_pct": None, "horizon_days": 1},
    }))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_golden_base(c)  # the full empty schema every scenario test starts from
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    upsert_model(c, ModelConfig(
        id="def", model_alias="def", provider="openai", model_name="def",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(c, LLMRole.DEFAULT, "def")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", _fake_llm)
    # Portfolio scope measures a price_change card on the flow-adjusted TWR; the market
    # simply rose 3% over every window here (the per_symbol scenario uses real prices).
    monkeypatch.setattr(
        insight_service, "_portfolio_return",
        lambda conn, created, due, *, now, reporting: Decimal("0.03"),
    )


def _config(conn: sqlite3.Connection, *, max_shadows: int, auto: bool = True) -> None:
    cs.set_evolution_config(
        conn, auto_promote=auto, shadow_batches=3, min_samples=8, max_shadows=max_shadows,
        gap_alert_pp=Decimal("15"),
    )


def _task(conn: sqlite3.Connection, name: str, *, scope: str = "portfolio",
          universe: dict[str, Any] | list[Any] | str | None = None) -> int:
    sp = cs.create_strategy(conn, name=f"S-{name}", body="觀察走勢", now=MON)
    it = cs.create_insight_type(
        conn, name=name, scope=scope, self_correct=True, universe=universe, now=MON,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    # Two live versions, none active → v2 is the shadow (promote.shadow_version).
    cs.create_calibration(conn, it.id, body="QA-CAL v1 規則", cause=None, now=MON)
    cs.create_calibration(conn, it.id, body="QA-CAL v2 規則", cause=None, now=MON)
    return it.id


def _rising(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD, sector="科技", name=symbol,
    ))
    start = date(2026, 6, 1)
    px = Decimal("100")
    rows = []
    for i in range(140):
        rows.append(PriceRow(instrument=symbol, market=Market.US,
                             as_of=start + timedelta(days=i), close=px, source="test"))
        px = (px * Decimal("1.03")).quantize(Decimal("0.0001"))
    upsert_prices(conn, rows, fetched_at=MON)


def _shadow_cards(conn: sqlite3.Connection, tid: int, version: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM insights WHERE insight_type_id = ? AND is_shadow = 1"
    params: list[object] = [tid]
    if version is not None:
        sql += " AND calibration_version = ?"
        params.append(version)
    return int(conn.execute(sql, params).fetchone()["c"])


def _active_version(conn: sqlite3.Connection, tid: int) -> int | None:
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    return it.active_calibration_version


def _evaluate(conn: sqlite3.Connection, day: int) -> None:
    """The 18:00 evaluate pass of weekday batch ``day`` (every 1-day card due by then)."""
    insight_service.evaluate_due(conn, now=_day(day).replace(hour=18, minute=0))


# --- (a) per_symbol: one batch of N cards is ONE slot; a new version after a promotion ----


def test_a_per_symbol_batch_is_one_slot_and_a_version_after_promotion_is_shadowed(
    conn: sqlite3.Connection,
) -> None:
    for sym in ("AAA", "BBB"):
        _rising(conn, sym)
    _config(conn, max_shadows=1)
    tid = _task(conn, "個股健檢", scope="per_symbol",
                universe={"mode": "custom", "symbols": ["AAA", "BBB"]})

    insight_service.run_for_id(conn, tid, now=_day(0))
    # Both symbols shadowed in the SAME batch: one task = one slot, whatever its card count.
    assert _shadow_cards(conn, tid, 2) == 2
    insight_service.run_for_id(conn, tid, now=_day(1))
    # Still in the shadow period (0 scored < 3) → shadows EVERY batch. The old all-history
    # card count (2 >= 1) stopped here.
    assert _shadow_cards(conn, tid, 2) == 4

    _evaluate(conn, 2)  # 4 shadow hits (up, market rose) vs 4 active misses (down)
    assert _active_version(conn, tid) == 2  # auto-promoted on v2's OWN evidence

    cs.create_calibration(conn, tid, body="QA-CAL v3 規則", cause=None, now=_day(2))
    # (d) v3 has no evaluation of its own: v2's four shadow hits must not promote it.
    promoted = insight_service.promote_and_check(conn, now=_day(2))
    assert tid not in [p.insight_type_id for p in promoted]
    assert _active_version(conn, tid) == 2

    insight_service.run_for_id(conn, tid, now=_day(3))
    # v2's old shadow cards hold no slot: the new version enters its period at once.
    assert _shadow_cards(conn, tid, 3) == 2
    active_v2 = conn.execute(
        "SELECT COUNT(*) AS c FROM insights WHERE insight_type_id = ? AND is_shadow = 0 "
        "AND calibration_version = 2", (tid,),
    ).fetchone()["c"]
    assert active_v2 == 2  # the shown cards now carry the promoted layer
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    state = insight_service.shadow_state(conn, it)
    assert (state.version, state.phase, state.scored, state.needed) == (3, "running", 0, 3)


# --- (b) two tasks, cap 1: the second queues and enters when the first ends -------------


def test_b_second_task_queues_until_the_first_leaves_its_period(
    conn: sqlite3.Connection,
) -> None:
    _config(conn, max_shadows=1)
    a = _task(conn, "組合甲")
    b = _task(conn, "組合乙")

    insight_service.run_for_id(conn, a, now=_day(0))
    insight_service.run_for_id(conn, b, now=_day(0))
    assert _shadow_cards(conn, a) == 1
    assert _shadow_cards(conn, b) == 0  # queued: the one slot is 組合甲's
    it_b = cs.get_insight_type(conn, b)
    assert it_b is not None
    queued = insight_service.shadow_state(conn, it_b)
    assert (queued.phase, queued.slots_used, queued.max_shadows) == ("queued", 1, 1)
    set_role(conn, LLMRole.MASTER, "def")  # G7's master-missing warn would outrank the queue
    g7 = next(
        g for g in (insight_service.build_diagnose(conn, b, now=_day(0)) or {})["gates"]
        if g["id"] == "G7"
    )
    set_role(conn, LLMRole.MASTER, None)  # scoring below stays quant-only
    assert g7["msg"] == "有未套用的校正版本 v2；影子排隊中（目前 1／上限 1）"
    assert g7["fix"] == {"kind": "set_active_calibration"}

    for n in (1, 2):
        insight_service.run_for_id(conn, a, now=_day(n))
        insight_service.run_for_id(conn, b, now=_day(n))
    assert _shadow_cards(conn, a) == 3  # 組合甲 shadowed every batch of its period
    assert _shadow_cards(conn, b) == 0

    _evaluate(conn, 3)  # 組合甲's 3 shadow evaluations → promoted → leaves its period
    assert _active_version(conn, a) == 2

    insight_service.run_for_id(conn, a, now=_day(4))
    insight_service.run_for_id(conn, b, now=_day(4))
    assert _shadow_cards(conn, b) == 1  # the freed slot goes to 組合乙
    assert _shadow_cards(conn, a) == 3  # active == latest: no shadow, no slot
    it_b = cs.get_insight_type(conn, b)
    assert it_b is not None
    assert insight_service.shadow_state(conn, it_b).phase == "running"


@pytest.mark.parametrize("how", ["archive_shadow", "active_catches_up"])
def test_b_archiving_the_shadow_or_catching_up_frees_the_slot(
    conn: sqlite3.Connection, how: str,
) -> None:
    _config(conn, max_shadows=1)
    a = _task(conn, "組合甲")
    b = _task(conn, "組合乙")
    insight_service.run_for_id(conn, a, now=_day(0))
    insight_service.run_for_id(conn, b, now=_day(0))
    assert (_shadow_cards(conn, a), _shadow_cards(conn, b)) == (1, 0)

    if how == "archive_shadow":
        v2 = next(c for c in cs.list_calibrations(conn, a) if c.version == 2)
        cs.archive_calibration(conn, v2.id)  # only v1 left, none active → no shadow
    else:
        cs.set_active_calibration(conn, a, 2)  # active == latest → no shadow

    insight_service.run_for_id(conn, a, now=_day(1))
    insight_service.run_for_id(conn, b, now=_day(1))
    assert _shadow_cards(conn, a) == 1  # 組合甲 produced no further shadow
    assert _shadow_cards(conn, b) == 1  # and its slot went to 組合乙


# --- (c) portfolio: one card per batch reaches shadow_batches and promotes ---------------


def test_c_portfolio_task_accumulates_shadow_batches_and_promotes(
    conn: sqlite3.Connection,
) -> None:
    _config(conn, max_shadows=2)  # the default cap the old card count tripped at 2 cards
    tid = _task(conn, "組合週報")
    for n in (0, 1, 2):
        insight_service.run_for_id(conn, tid, now=_day(n))
    assert _shadow_cards(conn, tid, 2) == 3

    _evaluate(conn, 3)
    scored = conn.execute(
        "SELECT COUNT(*) AS c FROM insight_evaluations WHERE insight_type_id = ? "
        "AND is_shadow = 1 AND calibration_version = 2 AND status = 'scored'", (tid,),
    ).fetchone()["c"]
    assert scored == 3
    assert _active_version(conn, tid) == 2


# --- a version that LOSES leaves its period too (decided, not stuck) ---------------------


def test_a_losing_shadow_is_decided_and_stops_holding_a_slot(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The market FELL: the calibrated shadow (up) misses, the active (down) hits.
    monkeypatch.setattr(
        insight_service, "_portfolio_return",
        lambda conn, created, due, *, now, reporting: Decimal("-0.03"),
    )
    _config(conn, max_shadows=1)
    a = _task(conn, "組合甲")
    b = _task(conn, "組合乙")
    for n in (0, 1, 2):
        insight_service.run_for_id(conn, a, now=_day(n))
        insight_service.run_for_id(conn, b, now=_day(n))
    assert (_shadow_cards(conn, a), _shadow_cards(conn, b)) == (3, 0)
    _evaluate(conn, 3)
    assert _active_version(conn, a) is None  # held: the shadow was worse
    it_a = cs.get_insight_type(conn, a)
    assert it_a is not None
    assert insight_service.shadow_state(conn, it_a).phase == "lost"

    insight_service.run_for_id(conn, a, now=_day(4))
    insight_service.run_for_id(conn, b, now=_day(4))
    assert _shadow_cards(conn, a) == 3  # decided: no more shadow spend on a lost version
    assert _shadow_cards(conn, b) == 1  # its slot is free


def test_disabled_task_holds_no_slot(conn: sqlite3.Connection) -> None:
    _config(conn, max_shadows=1)
    a = _task(conn, "組合甲")
    b = _task(conn, "組合乙")
    insight_service.run_for_id(conn, a, now=_day(0))
    it_a = cs.get_insight_type(conn, a)
    assert it_a is not None
    cs.update_insight_type(
        conn, a, name=it_a.name, scope=it_a.scope, self_correct=True, enabled=False,
        now=_day(0),
    )
    insight_service.run_for_id(conn, b, now=_day(1))
    assert _shadow_cards(conn, b) == 1  # a disabled task cannot block the queue forever
