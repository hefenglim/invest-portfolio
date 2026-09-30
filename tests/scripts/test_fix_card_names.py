"""scripts/fix_card_names.py — the one-off correction of stored cards (item 8, owner 2026-09-30:
「舊有資料就進行修正」). A wrong name beside a registered code becomes the code's preferred name;
a name whose extent the text cannot show is replaced only when reviewed (``--wrong-name``) and
is otherwise reported, never guessed; a dry run writes nothing."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from portfolio_dash.shared.instrument_names import NamedInstrument

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fix_card_names.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fix_card_names", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REG = [NamedInstrument("3008", "LARGAN", ("大立光",)), NamedInstrument("2330", "台積電", ()),
       NamedInstrument("2412", "中華電信", ()), NamedInstrument("2609", "Yang Ming", ("陽明",)),
       NamedInstrument("2603", "Evergreen", ("長榮",))]


def test_a_known_extent_is_replaced_by_the_preferred_name() -> None:
    fix = _load()
    text, changes, left = fix.fix_field("台積電(3008) 偏多；2603 陽明：多方", REG, [])
    assert text == "大立光(3008) 偏多；2603 長榮：多方"
    assert [(c["from"], c["to"]) for c in changes] == [("台積電", "大立光"), ("陽明", "長榮")]
    assert left == []


def test_an_unknown_extent_needs_a_reviewed_name() -> None:
    fix = _load()
    text, changes, left = fix.fix_field("持股以聯詠 (3008) 為最", REG, [])
    assert (text, changes) == ("持股以聯詠 (3008) 為最", [])
    assert left == [{"code": "3008", "written": "持股以聯詠"}]
    text, changes, left = fix.fix_field("持股以聯詠 (3008) 為最", REG, ["聯詠"])
    assert text == "持股以大立光 (3008) 為最" and left == []


def test_a_reviewed_wrong_name_after_a_bare_code_is_replaced() -> None:
    """Demo card #231 (2026-09-28): title 「2603 萬海：多頭趨勢延續」. 萬海 is not registered, so
    the checker cannot read the bare form — a reviewed ``--wrong-name`` can. A reviewed name
    that IS a name of the code it follows is left alone."""
    fix = _load()
    text, changes, left = fix.fix_field("2603 萬海：多頭趨勢延續", REG, [])
    assert (text, changes, left) == ("2603 萬海：多頭趨勢延續", [], [])
    text, changes, left = fix.fix_field("2603 萬海：多頭趨勢延續；2609 陽明", REG, ["萬海", "陽明"])
    assert text == "2603 長榮：多頭趨勢延續；2609 陽明"
    assert [(c["code"], c["from"], c["to"]) for c in changes] == [("2603", "萬海", "長榮")]
    assert left == []


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "cards.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE instruments (symbol TEXT, name TEXT, aliases TEXT)")
    c.executemany("INSERT INTO instruments VALUES (?,?,?)",
                  [(i.symbol, i.name, json.dumps(list(i.aliases), ensure_ascii=False))
                   for i in REG])
    c.execute("CREATE TABLE insights (id INTEGER, title TEXT, summary TEXT, body_md TEXT)")
    c.execute("INSERT INTO insights VALUES (1, '台積電(3008) 偏多', 'ok', '| 2412（聯詠） |')")
    c.commit()
    c.close()
    return path


@pytest.mark.parametrize("apply", [False, True])
def test_a_dry_run_writes_nothing_and_apply_writes_the_fix(tmp_path: Path, apply: bool,
                                                           capsys: pytest.CaptureFixture[str]
                                                           ) -> None:
    fix = _load()
    path = _db(tmp_path)
    assert fix.main(["--db", str(path)] + (["--apply"] if apply else [])) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"cards_changed": 1, "corrections": 2, "needs_review": 0}
    row = sqlite3.connect(path).execute("SELECT title, body_md FROM insights").fetchone()
    assert row == (("大立光(3008) 偏多", "| 2412（中華電信） |") if apply
                   else ("台積電(3008) 偏多", "| 2412（聯詠） |"))
