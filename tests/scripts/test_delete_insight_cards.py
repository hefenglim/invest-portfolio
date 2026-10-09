"""DEF-046 (owner ruling 2026-09-24): delete the R1 bad card, its scoring rows, with an audit.

The card the verifier saw (symbol ``moomoo_my`` — an ACCOUNT id — reading 「Moomoo 交易商
警示」) was produced by DEF-037 and stays in the append-only ``insights`` table after the code
fix. ``scripts/delete_insight_cards.py`` is the one-off data door the lead runs on the demo
after a backup. Everything it promises is exercised here on a temp database, including every
way it says NO — the id in the R2 handoff (#207) turned out to be a legitimate 2884 card, so
the refusals are the half of this script that matters most.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from scripts import delete_insight_cards as script

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _card(conn: sqlite3.Connection, symbol: str, title: str, fp: str) -> int:
    rec = istore.add_card(
        conn, insight_type_id=9, fingerprint=fp, calibration_version=None,
        card=InsightCard(title=title, summary="s", body_md="b", symbol=symbol, confidence=40,
                         prediction=Prediction(metric="price_change", direction="down",
                                               horizon_days=3)),
        horizon_days=3, input_snapshot="x", model="m", cost_usd=Decimal("0.01"), now=NOW,
    )
    return rec.id


def _eval(conn: sqlite3.Connection, insight_id: int) -> None:
    es.add_evaluation(
        conn, insight_id=insight_id, insight_type_id=9, calibration_version=None,
        is_shadow=False, status="scored", quant_hit=False, narrative_score=None, miss=True,
        actual_value=Decimal("0.02"), confidence=40, now=NOW,
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, dict[str, int]]]:
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)  # moomoo_my is an ACCOUNT here
    conn.execute("INSERT INTO instruments (symbol, market, quote_ccy) VALUES ('2884','TW','TWD')")
    cs.ensure_seeded(conn)
    istore.ensure_tables(conn)
    es.ensure_tables(conn)
    ids = {
        "bad": _card(conn, "moomoo_my", "Moomoo 交易商警示", "fp-bad"),
        "legit": _card(conn, "2884", "玉山金(2884) - RSI過熱警示", "fp-2884"),
        "other": _card(conn, "SPCX", "SPCX 回撤", "fp-spcx"),
    }
    _eval(conn, ids["bad"])
    _eval(conn, ids["bad"])
    _eval(conn, ids["legit"])
    conn.commit()
    conn.close()
    yield path, ids


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _counts(path: Path) -> dict[str, int]:
    c = _open(path)
    try:
        out = {}
        for t in ("insights", "insight_evaluations", "ledger_audit"):
            out[t] = int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        has_log = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_log'"
        ).fetchone()
        out["action_log"] = (
            int(c.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]) if has_log else 0
        )
        return out
    finally:
        c.close()


def test_dry_run_prints_the_card_and_its_dependents_and_writes_nothing(
    db: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = db
    before = _counts(path)
    assert script.main(["--db", str(path), "--id", str(ids["bad"])]) == 0
    out = capsys.readouterr().out
    assert f"卡片 #{ids['bad']}" in out
    assert "symbol='moomoo_my'（帳戶代號（不是標的））" in out
    assert "Moomoo 交易商警示" in out
    assert "依附列 insight_evaluations：2 筆" in out
    assert "（試跑）將刪除 3 列" in out
    assert _counts(path) == before


def test_apply_deletes_the_card_and_its_scoring_rows_and_leaves_an_audit(
    db: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = db
    before = _counts(path)
    assert script.main(["--db", str(path), "--id", str(ids["bad"]), "--apply",
                        "--reason", "DEF-046"]) == 0
    after = _counts(path)
    assert after["insights"] == before["insights"] - 1
    assert after["insight_evaluations"] == before["insight_evaluations"] - 2
    assert after["ledger_audit"] == before["ledger_audit"] + 3
    assert after["action_log"] == before["action_log"] + 1
    c = _open(path)
    try:
        assert c.execute("SELECT 1 FROM insights WHERE id=?", (ids["bad"],)).fetchone() is None
        # the other cards — and the legitimate card's scoring row — are untouched
        assert c.execute("SELECT COUNT(*) FROM insights WHERE id IN (?, ?)",
                         (ids["legit"], ids["other"])).fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM insight_evaluations WHERE insight_id=?",
                         (ids["legit"],)).fetchone()[0] == 1
        audit = c.execute(
            "SELECT table_name, row_id, action, before_json FROM ledger_audit ORDER BY id"
        ).fetchall()
        assert [(a["table_name"], a["action"]) for a in audit[-3:]] == [
            ("insight_evaluations", "delete"), ("insight_evaluations", "delete"),
            ("insights", "delete"),
        ]
        card_before = json.loads(audit[-1]["before_json"])
        assert card_before["id"] == ids["bad"] and card_before["symbol"] == "moomoo_my"
        assert card_before["title"] == "Moomoo 交易商警示"  # the content is recoverable
        log = c.execute("SELECT * FROM action_log ORDER BY id DESC LIMIT 1").fetchone()
        assert log["action"] == f"洞察卡刪除（資料清理）#{ids['bad']}：DEF-046"
        assert log["method"] == "SCRIPT" and log["status"] == 200
    finally:
        c.close()
    out = capsys.readouterr().out
    assert "刪除前筆數" in out and "刪除後筆數" in out


def test_a_second_run_is_a_no_op(
    db: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = db
    assert script.main(["--db", str(path), "--id", str(ids["bad"]), "--apply"]) == 0
    once = _counts(path)
    assert script.main(["--db", str(path), "--id", str(ids["bad"]), "--apply"]) == 0
    assert _counts(path) == once
    assert "已刪除（ledger_audit 有刪除紀錄），略過" in capsys.readouterr().out


def test_an_id_that_never_existed_is_refused_and_nothing_is_written(
    db: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = db
    before = _counts(path)
    # all-or-nothing: the valid id in the same call is NOT deleted either
    assert script.main(["--db", str(path), "--id", str(ids["bad"]), "--id", "999",
                        "--apply"]) == 2
    assert _counts(path) == before
    assert "#999 不存在" in capsys.readouterr().err


def test_a_card_on_a_registered_symbol_needs_an_explicit_flag(
    db: tuple[Path, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The R2 handoff named #207 — a legitimate 2884 card. That mix-up must stop here."""
    path, ids = db
    before = _counts(path)
    assert script.main(["--db", str(path), "--id", str(ids["legit"]), "--apply"]) == 2
    assert _counts(path) == before
    assert "已註冊標的" in capsys.readouterr().err
    assert script.main(["--db", str(path), "--id", str(ids["legit"]), "--apply",
                        "--allow-registered-symbol"]) == 0
    assert _counts(path)["insights"] == before["insights"] - 1


def test_a_missing_database_is_refused_and_never_created(tmp_path: Path) -> None:
    ghost = tmp_path / "nope.db"
    assert script.main(["--db", str(ghost), "--id", "1"]) == 2
    assert not ghost.exists()


def test_db_and_id_are_required() -> None:
    with pytest.raises(SystemExit) as exc:
        script.main(["--id", "1"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        script.main(["--db", "x.db"])


def test_a_table_added_later_with_an_insight_id_is_covered(
    db: tuple[Path, dict[str, int]]
) -> None:
    """Dependents are read from the SCHEMA, so a future reference table needs no edit."""
    path, ids = db
    c = _open(path)
    c.execute("CREATE TABLE future_ref (id INTEGER PRIMARY KEY, insight_id INTEGER)")
    c.execute("INSERT INTO future_ref (insight_id) VALUES (?)", (ids["bad"],))
    c.execute("INSERT INTO future_ref (insight_id) VALUES (?)", (ids["other"],))
    c.commit()
    c.close()
    probe = _open(path)
    try:
        assert "future_ref" in script.dependent_tables(probe)
    finally:
        probe.close()
    assert script.main(["--db", str(path), "--id", str(ids["bad"]), "--apply"]) == 0
    c = _open(path)
    try:
        left = [r[0] for r in c.execute("SELECT insight_id FROM future_ref")]
        assert left == [ids["other"]]
    finally:
        c.close()


def test_a_failure_mid_transaction_rolls_everything_back(
    db: tuple[Path, dict[str, int]]
) -> None:
    path, ids = db
    c = _open(path)
    c.execute(
        "CREATE TRIGGER boom BEFORE DELETE ON insights BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    c.commit()
    c.close()
    before = _counts(path)
    with pytest.raises(sqlite3.DatabaseError):
        script.main(["--db", str(path), "--id", str(ids["bad"]), "--apply"])
    after = _counts(path)
    # the evaluations were deleted BEFORE the card in the same transaction — all rolled back
    assert after["insights"] == before["insights"]
    assert after["insight_evaluations"] == before["insight_evaluations"]
    assert after["ledger_audit"] == before["ledger_audit"]
