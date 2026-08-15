"""No file states how many ledgers there are — the registry does that.

`shared/ledger_registry.py` exists to remove hand-maintained ledger enumerations from the
CODE (spec §6.0, Owner 3). It succeeded there and never reached the **copy**: when corporate
actions became the 5th ledger, **nine** sentences across five files went on stating four, and
one of them told the owner the export zip contained four CSVs while the route built five.

Nine, not the eight found by reading: the ninth was found by this test on its first run, in
`ledger_registry.py` itself — the file whose whole existence is the argument against counting
ledgers by hand.

That class of defect has a property worth naming: **nothing can catch it**. A count typed
into a sentence is not wrong in any way a type checker, a route test or a golden payload can
see — it is only wrong against a fact stated somewhere else entirely. So the guard is the
crude one that actually works: the sentence may not carry the count at all.

Escape hatch: a line containing ``ledger-count-ok`` is exempt, for the comments that quote
the old wording as history. It is deliberately conspicuous — copying it into a live UI string
takes a decision, not a slip.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

#: 「四帳本」「5 帳本」「六本帳本」… Digits and the CJK numerals, immediately before 帳本.
_ZH_COUNT = re.compile(r"[一二三四五六七八九十兩零〇\d]\s*本?帳本")

#: "four-ledger zip", "five-ledger" — the same claim in the Python docstrings.
_EN_COUNT = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|\d+)-ledger\b", re.IGNORECASE
)

_ESCAPE = "ledger-count-ok"


def _web_files() -> list[Path]:
    web = _ROOT / "web"
    return sorted(
        p for p in [*web.glob("*.js"), *web.glob("*.html")]
        # Vendored third-party bundle: not ours to police, and minified besides.
        if p.name != "echarts.min.js"
    )


def _offending(path: Path, pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _ESCAPE in line:
            continue
        if pattern.search(line):
            hits.append(f"{path.relative_to(_ROOT).as_posix()}:{n}  {line.strip()}")
    return hits


def test_no_frontend_string_states_a_ledger_count() -> None:
    hits = [h for p in _web_files() for h in _offending(p, _ZH_COUNT)]
    assert not hits, (
        "帳本數量不可寫死在文案裡 — 請改由 GET /api/export/ledgers 或改寫成不帶數量的說法：\n"
        + "\n".join(hits)
    )


def test_no_python_docstring_states_a_ledger_count() -> None:
    """The same claim in English. `export/ledgers.py` carried it for a week after it broke."""
    hits = [
        h
        for p in sorted((_ROOT / "portfolio_dash").rglob("*.py"))
        for h in _offending(p, _EN_COUNT)
    ]
    assert not hits, "a ledger count belongs in the registry, not in prose:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    ("text", "should_match"),
    [
        ("<span>所有交易・四帳本・更正以全帳重播檢核</span>", True),   # the real 2026-08-16 defect
        ("desc: '四帳本（期初/交易/股利/換匯）各一份 CSV'", True),
        ("'由 5 帳本完整重建'", True),
        ("'六本帳本'", True),
        ("'所有交易・各帳本・更正以全帳重播檢核'", False),             # the fix
        ("'帳本為 append-only'", False),
        ("'重複的列已略過，帳本沒有變成兩筆。'", False),               # ★ 兩 is not before 帳本
    ],
)
def test_the_pattern_matches_what_it_claims_to(text: str, should_match: bool) -> None:
    """A guard nobody has watched fail is a comment.

    The last case is the one worth having: a sentence containing a CJK numeral and the word
    帳本 in the *other* order is ordinary copy, and a pattern that flagged it would be turned
    off within a week.
    """
    assert bool(_ZH_COUNT.search(text)) is should_match
