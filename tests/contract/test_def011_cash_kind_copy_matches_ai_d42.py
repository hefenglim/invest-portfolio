"""DEF-011 (functional test manual A-16, 2026-09-23): the copy says what AI-D42 does.

收件匣 › 待確認退款 told the owner a confirmed rebate is 「不計入成本／損益／XIRR」. The verifier
confirmed 620 TWD and measured the opposite: ``kpis.xirr`` 10.22406575… → 10.22472258…,
``total_return_fx_complete`` +620, ``trading_financing_cost`` 191 → 811 (``total_return``
unchanged). The behaviour is the ruling (AI-D42, 2026-08-24 — ``REBATE`` / ``INTEREST_EXPENSE``
/ ``BROKER_FEE`` are costs of trading and financing and therefore return; ``DEPOSIT`` /
``WITHDRAW`` / ``OPENING`` / ``INTEREST`` are not). The copy was written before it and never
followed.

The class is every rendered sentence that states what a cash kind does to the return
figures. Measured on 2026-09-23 by scanning every rendered web string and every non-docstring
backend string for a negation next to XIRR / 損益 / 報酬: 20 hits, 7 of them about a cash kind —
5 wrong (the rebate panel's sub-line and note, the rebate list's rule line, the what's-new
rebate entry, the what's-new cash-kinds entry 「三種都不列入 XIRR」) and 2 right (the
reorganisation fee is a WITHDRAW and stays out of XIRR; 「入金／出金／期初與閒置現金利息仍然不
算」). The other 13 are not about a cash kind (A/B labels, FX 取得成本, the index-return
caption, prompts).

The guard is keyed on AI-D42's own table (``portfolio/returns.py::XIRR_CASH_KINDS``), so a
future ruling that moves a kind out of XIRR fails HERE first and asks for the copy to move
with it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from portfolio_dash.portfolio.returns import XIRR_CASH_KINDS
from tests.contract.test_zh_punctuation_fullwidth import html_strings, js_strings

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / "web"

#: A negation that claims something is left OUT of a figure.
_NEG = r"(?:不計入|不列入|不影響|不進入|不會進入|不會計入|絕不計入|不算入)"
#: …followed, inside one clause, by XIRR, or by a 損益 that is not the asset P&L (A).
_FORBIDDEN = re.compile(_NEG + r"[^。；]*?(?:XIRR|(?<!資產)損益)")
#: Rebate sentences: the kind's label, its billing model, or the refund it books.
_REBATE = re.compile(r"折讓|先收後退|退款")
#: AI-D42's cost kinds by their zh labels (web/cash.js KIND_LABEL).
_COST_LABELS = ("融資利息", "券商費用")


def _rendered() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in sorted(_WEB.glob("*.js")) + sorted(_WEB.glob("*.html")):
        if p.name == "echarts.min.js":
            continue
        src = p.read_text(encoding="utf-8")
        strings = html_strings(src) if p.suffix == ".html" else js_strings(src)
        out.extend((f"web/{p.name}:{ln}", t) for ln, t in strings)
        if p.suffix == ".html":
            # A sentence split by inline markup (「…現金池，<b>不計入成本／損益／XIRR</b>」) is
            # two text nodes, and neither alone says both halves — so each source line is
            # also read with its tags removed, the way the owner reads it.
            plain = re.sub(r"<!--.*?-->", "", src, flags=re.S)
            for ln, line in enumerate(plain.splitlines(), 1):
                text = re.sub(r"<[^>]+>", "", line)
                if text.strip():
                    out.append((f"web/{p.name}:{ln}", text))
    for p in sorted((_ROOT / "portfolio_dash").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        rel = p.relative_to(_ROOT).as_posix()
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                out.append((f"{rel}:{n.lineno}", n.value))
    return out


def test_the_ruling_this_copy_describes_is_still_the_ruling() -> None:
    assert {"REBATE", "INTEREST_EXPENSE", "BROKER_FEE"} <= XIRR_CASH_KINDS
    assert not ({"DEPOSIT", "WITHDRAW", "OPENING", "INTEREST"} & XIRR_CASH_KINDS)


def test_the_detector_sees_the_audited_sentence() -> None:
    for bad in ("確認實際入帳後記入該帳戶現金池，不計入成本／損益／XIRR",
                "確認後記入該帳戶現金池，絕不計入成本／損益。",
                "三種都不列入 XIRR（它們不是投資決策的現金流）"):
        assert _FORBIDDEN.search(bad), bad
    for good in ("不改變持倉成本與資產損益，但會計入 XIRR 與含匯兌總損益",
                 "確認後記入該帳戶現金池，不影響成本。", "預估次月折讓 +109（不計入成本）"):
        assert not _FORBIDDEN.search(good), good


def test_no_rebate_sentence_says_it_stays_out_of_xirr_or_pnl() -> None:
    bad = [(where, text.strip()[:90]) for where, text in _rendered()
           if _REBATE.search(text) and _FORBIDDEN.search(text)]
    assert not bad, f"a rebate sentence contradicts AI-D42: {bad}"


def test_no_cost_kind_sentence_says_it_stays_out_of_xirr() -> None:
    """A string that names 融資利息／券商費用 and XIRR, and negates XIRR somewhere, must also
    affirm 「會計入 XIRR」 — the what's-new entry said 「三種都不列入 XIRR」 of all three new
    kinds, two of which AI-D42 puts IN. (Whole-string, not per clause: that entry named the
    kinds in one sentence and excluded them in the next.)"""
    bad = [
        (where, text.strip()[:90]) for where, text in _rendered()
        if "XIRR" in text and any(label in text for label in _COST_LABELS)
        and re.search(_NEG + r"[^，]*?XIRR", text) and "會計入 XIRR" not in text
    ]
    assert not bad, f"a 融資利息／券商費用 sentence contradicts AI-D42: {bad}"


def test_the_rebate_panel_says_what_a_confirmation_does() -> None:
    """The three places the owner reads before confirming name BOTH figures it moves."""
    html = (_WEB / "dividend-inbox.html").read_text(encoding="utf-8")
    section = html[html.index('id="rebate-section"'):]
    section = section[:section.index("</section>")]
    rebate_js = (_WEB / "rebate-inbox.js").read_text(encoding="utf-8")
    for where, text in (("panel", section), ("rebate-inbox.js", rebate_js)):
        assert text.count("會計入 XIRR 與含匯兌總損益") >= 2, where
        assert "資產損益" in text, where
