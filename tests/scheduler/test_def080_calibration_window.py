"""DEF-080 (R6) — Loop 3 is triggered only by evidence no version was built from yet.

The trigger (resolved ≥ min_samples, then ≥3 consecutive misses or a miss rate over
``gap_alert_pp``) read the task's WHOLE active-lane history. A version never consumed the
misses that produced it, so the same old failures qualified again on the next Sunday and every
Sunday after: one bad week produced a new version per week for ever (the verifier's run made
v1 and v2 back to back from one batch of misses).

The window (``evaluations_store.calibration_window``) is now:

* the evaluations of cards generated with the ACTIVE version — ``calibration_version IS
  active``: a version is judged on its own cards, in either lane (its shadow period is its
  record too); no active version = the cards with no calibration layer;
* evaluated AFTER the task's newest calibration version (archived included) was created —
  the evidence that version was built from is spent, whether it was adopted, lost or archived.

And no new version is written while the newest one is still being shadow-evaluated or has
won and waits for 設為生效: a new version would take its place as the shadow and it would
never be judged.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.llm_insight import composer_store as cs
from tests.scheduler.test_def079_calibration_basis import (
    SUNDAY,
    T0,
    install_fake_master,
    make_conn,
    scored_card,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = make_conn()
    yield c
    c.close()


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return install_fake_master(monkeypatch)


def _task(conn: sqlite3.Connection) -> int:
    return cs.create_insight_type(
        conn, name="個股健檢", scope="per_symbol", self_correct=True, now=T0,
    ).id


def _versions(conn: sqlite3.Connection, tid: int) -> list[int]:
    return [c.version for c in cs.list_calibrations(conn, tid, include_archived=True)]


def _misses(conn: sqlite3.Connection, tid: int, tag: str, *, version: int | None,
            after: datetime, n: int = 8) -> None:
    for i in range(n):
        scored_card(conn, tid, f"{tag}{i}", version=version, miss=True,
                    evaluated=after + timedelta(hours=1 + i))


def test_the_same_old_misses_do_not_make_a_version_every_week(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    _misses(conn, tid, "第一週失誤", version=None, after=T0)
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY)
    assert _versions(conn, tid) == [1]

    nothing_new = insight_service.generate_calibrations_for_all(
        conn, now=SUNDAY + timedelta(days=7))
    assert _versions(conn, tid) == [1]  # the old run made v2 from the same 8 misses
    assert str(nothing_new) == "產生 0 版；略過 1 個任務（個股健檢 新樣本 0／門檻 8）"
    assert len(prompts) == 1


def test_fresh_misses_after_the_last_version_still_trigger(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    _misses(conn, tid, "第一週失誤", version=None, after=T0)
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY)
    _misses(conn, tid, "第二週失誤", version=None, after=SUNDAY)  # v1 not adopted yet
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY + timedelta(days=7))
    assert _versions(conn, tid) == [1, 2]
    assert "第二週失誤" in prompts[1] and "第一週失誤" not in prompts[1]


def test_after_adoption_only_the_active_versions_own_cards_count(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    _misses(conn, tid, "第一週失誤", version=None, after=T0)
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY)
    cs.set_active_calibration(conn, tid, 1)
    # cards made BEFORE adoption, maturing after v1 was written: not v1's performance
    _misses(conn, tid, "採用前的卡", version=None, after=SUNDAY)
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY + timedelta(days=7))
    assert _versions(conn, tid) == [1]

    _misses(conn, tid, "v1的失誤", version=1, after=SUNDAY + timedelta(days=7))
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY + timedelta(days=14))
    assert _versions(conn, tid) == [1, 2]
    assert "v1的失誤" in prompts[-1] and "採用前的卡" not in prompts[-1]


def test_an_archived_version_still_spends_its_evidence(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    _misses(conn, tid, "第一週失誤", version=None, after=T0)
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY)
    v1 = cs.list_calibrations(conn, tid)[0]
    cs.archive_calibration(conn, v1.id)  # the owner rejected it
    insight_service.generate_calibrations_for_all(conn, now=SUNDAY + timedelta(days=7))
    assert _versions(conn, tid) == [1]  # no instant retry from the same 8 misses


def test_no_new_version_while_the_newest_is_still_shadow_evaluated(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    cs.create_calibration(conn, tid, body="v1", cause=None, now=T0)
    cs.create_calibration(conn, tid, body="v2", cause=None, now=T0)
    cs.set_active_calibration(conn, tid, 1)
    scored_card(conn, tid, "影子v2命中", version=2, miss=False, shadow=True,
                evaluated=T0 + timedelta(days=1))  # 1 of 3: v2 is still being judged
    _misses(conn, tid, "v1的失誤", version=1, after=T0 + timedelta(days=1))

    summary = insight_service.generate_calibrations_for_all(conn, now=SUNDAY)

    assert _versions(conn, tid) == [1, 2]  # the old run wrote v3 and dropped v2 mid-trial
    assert prompts == []
    assert str(summary) == "產生 0 版；暫不產生 1 個任務（個股健檢 v2 影子評估中）"


def test_no_new_version_while_a_winner_waits_for_adoption(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    cs.create_calibration(conn, tid, body="v1", cause=None, now=T0)
    cs.create_calibration(conn, tid, body="v2", cause=None, now=T0)
    cs.set_active_calibration(conn, tid, 1)
    for i in range(3):
        scored_card(conn, tid, f"影子v2命中{i}", version=2, miss=False, shadow=True,
                    evaluated=T0 + timedelta(days=1, hours=i))
    _misses(conn, tid, "v1的失誤", version=1, after=T0 + timedelta(days=1))

    summary = insight_service.generate_calibrations_for_all(conn, now=SUNDAY)

    assert _versions(conn, tid) == [1, 2]
    assert str(summary) == "產生 0 版；暫不產生 1 個任務（個股健檢 v2 勝出待設為生效）"
