"""Does an insight card pair each registered code with one of that instrument's names?

Owner ruling 2026-09-30 (item 8, 「登錄名稱＋中文別名」): a card names an instrument by its
registered name or one of its aliases; anything else beside its code is a wrong pairing. The
demo held 5 provable ones on 224 cards (「台積電 (3008)」, 「聯發科技 (2412)」) and more that
only an alias list can tell from a correct translation (「聯詠 (3008)」 wrong, 「大立光 (3008)」
right — both beside a code registered as LARGAN).

``generate`` sends the registry's names with every prompt (``INSIGHT_NAMING_NOTE``), checks the
reply here, asks once more on a mismatch, and does not store a card that still mismatches. The
DEF-082 figure check answers a different question — "does this code exist?" — and runs as
before.

Two shapes are read, both with half- or full-width parentheses:

* 「名稱 (代號)」 — the text right before the parenthesis must END with an accepted name form;
* 「代號 (名稱)」 — the parenthesised text must BE an accepted name form.

Accepted forms, per instrument: every name (registered + aliases); for a Chinese name also any
leading part of two characters or more (「玉山金」 for 玉山金控, 「群聯」 for 群聯電子 — how people
shorten Chinese company names); for a Latin name the DEF-082 word forms (「IHH」 for IHH
Healthcare), matched whole, case-blind, never by prefix (R9: 「(LARGA)」 is not LARGAN). A
generic referent before the code (「該標的 (2323)」) or no name at all (「…，(2330)」) is not a
pairing and passes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from portfolio_dash.llm_insight.figure_check import _INDICATOR_RE, _NOT_A_TICKER, _name_forms
from portfolio_dash.shared.instrument_names import NamedInstrument, has_cjk

_CODE = r"\d{4,6}|[A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,3})?"
#: 「… (代號)」: the code alone inside the parentheses.
# Full-width brackets are written as escapes (\uff08 / \uff09), not characters: these are
# patterns, not copy, and the zh punctuation guard reads every string literal.
_NAME_THEN_CODE = re.compile(rf"[(\uff08]\s*({_CODE})\s*[)\uff09]")
#: 「代號 (…)」: a code token, then a parenthesised name.
_CODE_THEN_NAME = re.compile(
    rf"(?<![A-Za-z0-9.])({_CODE})\s*[(\uff08]\s*([^()\uff08\uff09\n]{{1,30}}?)\s*[)\uff09]")
#: A bare code followed by spaces: 「2603 陽明」 (the check reads what follows).
_BARE_CODE = re.compile(rf"(?<![A-Za-z0-9.,\uff08(])({_CODE})(?![A-Za-z0-9])[ \t]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+$")
_LATIN_TAIL = re.compile(r"[A-Za-z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z][A-Za-z0-9&.\-]*){0,4}$")
_QUOTES = "「」『』\"'“”‘’《》〈〉*_ "
#: Nouns that stand before a code (or inside the parentheses after one) without naming a
#: company — measured on the demo's 224 cards: 「台股現貨資料 (2330)」, 「單一檔案 (1155)」,
#: 「美股 (NVDA)」.
_GENERIC = ("標的", "個股", "本檔", "該檔", "此檔", "該股", "此股", "本股", "股票", "公司",
            "代號", "代碼", "持股", "部位", "基金", "資料", "數據", "檔案", "報價", "股價",
            "價格", "市場", "指數", "美股", "台股", "馬股", "現股", "ETF", "股", "張", "檔")
_MIN_CJK_PREFIX = 2


@dataclass(frozen=True)
class Mismatch:
    """One wrong pairing. ``written`` is the name as found — exact when ``span`` is known
    (the parenthesised name, or another instrument's name), else the run of text before the
    code, which may carry words before the name (「持股以聯詠」). ``belongs_to`` is the symbol
    whose name ``written`` is, when it is one."""

    code: str
    written: str
    names: tuple[str, ...]
    belongs_to: str | None
    span: tuple[int, int] | None


def _registry_index(registry: Iterable[NamedInstrument]) -> dict[str, NamedInstrument]:
    index: dict[str, NamedInstrument] = {}
    for inst in registry:
        upper = inst.symbol.strip().upper()
        index.setdefault(upper, inst)
        index.setdefault(upper.split(".")[0], inst)
    return index


def _forms(inst: NamedInstrument) -> tuple[list[str], set[str]]:
    """(Chinese names, upper-cased Latin forms) the instrument may be called."""
    cjk = [n for n in inst.names if has_cjk(n)]
    latin = {n.strip().upper() for n in inst.names if not has_cjk(n) and len(n.strip()) > 1}
    latin |= _name_forms(n for n in inst.names if not has_cjk(n))
    return cjk, latin


def _ends_with_name(before: str, inst: NamedInstrument) -> bool:
    cjk, latin = _forms(inst)
    for name in cjk:
        for k in range(len(name), _MIN_CJK_PREFIX - 1, -1):
            if before.endswith(name[:k]):
                return True
    upper = before.upper()
    for form in latin:
        if upper.endswith(form):
            head = upper[: len(upper) - len(form)]
            if not head or not (head[-1].isalnum() and head[-1].isascii()):
                return True
    return False


def _is_name(text: str, inst: NamedInstrument) -> bool:
    cjk, latin = _forms(inst)
    if text.upper() in latin:
        return True
    return any(text == name[:k] for name in cjk
               for k in range(len(name), _MIN_CJK_PREFIX - 1, -1))


def accepts(written: str, inst: NamedInstrument) -> bool:
    """True when *written* is a name *inst* may be called by (a name, an alias, a Chinese
    leading part of 2+ characters, a Latin word form) — the rule every check here applies."""
    return _is_name(written, inst)


def _owner_of_tail(before: str, index: Mapping[str, NamedInstrument],
                   code_inst: NamedInstrument) -> tuple[str, str] | None:
    """(name, symbol) when *before* ends with ANOTHER instrument's whole name or alias."""
    best: tuple[str, str] | None = None
    for inst in {id(i): i for i in index.values()}.values():
        if inst.symbol == code_inst.symbol:
            continue
        for name in inst.names:
            if len(name) >= _MIN_CJK_PREFIX and before.upper().endswith(name.upper()) and (
                    best is None or len(name) > len(best[0])):
                best = (before[len(before) - len(name):], inst.symbol)
    return best


def _looks_like_a_name(text: str) -> bool:
    """A parenthesised text worth checking as a name (not a unit, ratio, number, date or
    note: 「AAPL (基準日2026-07-02)」 is a note beside the code, not what it is called)."""
    if any(ch.isdigit() for ch in text) or len(text) > 16:
        return False
    if has_cjk(text):
        return not text.endswith(_GENERIC)
    word = text.strip().upper()
    return (bool(re.fullmatch(r"[A-Z][A-Z0-9&.\- ]{1,29}", word))
            and word not in _NOT_A_TICKER and not _INDICATOR_RE.match(word))


def mismatches(text: str, registry: Iterable[NamedInstrument]) -> list[Mismatch]:
    """Every wrong 「name / code」 pairing in *text* for a REGISTERED code, in text order."""
    index = _registry_index(registry)
    found: list[Mismatch] = []
    for m in _NAME_THEN_CODE.finditer(text):
        inst = index.get(m.group(1).upper())
        if inst is None:
            continue
        raw_before = text[: m.start()]
        before = raw_before.rstrip(_QUOTES)
        if not before or not (has_cjk(before[-1]) or before[-1].isalpha()):
            continue                      # nothing names a company here: 「，(2330)」
        if before.endswith(_GENERIC) or _ends_with_name(before, inst):
            continue
        last_word = before.rsplit(None, 1)[-1].upper()
        if last_word in _NOT_A_TICKER:
            continue                      # a unit, not a name: 「215.3 USD (AAPL)」
        owner = _owner_of_tail(before, index, inst)
        if owner is not None:
            end = len(before)
            found.append(Mismatch(m.group(1), owner[0], inst.names, owner[1],
                                  (end - len(owner[0]), end)))
            continue
        tail = _CJK_RUN.search(before) or _LATIN_TAIL.search(before)
        written = tail.group(0) if tail else before[-8:]
        found.append(Mismatch(m.group(1), written[-12:], inst.names, None, None))
    for m in _CODE_THEN_NAME.finditer(text):
        inst = index.get(m.group(1).upper())
        inner = m.group(2).strip()
        if inst is None or not _looks_like_a_name(inner) or _is_name(inner, inst):
            continue
        if index.get(inner.upper()) is not None:
            continue                      # 「3008 (2330)」 is two codes, not a name
        belongs = next((i.symbol for i in {id(x): x for x in index.values()}.values()
                        if i.symbol != inst.symbol and _is_name(inner, i)), None)
        start = m.start(2) + (len(m.group(2)) - len(m.group(2).lstrip()))
        found.append(Mismatch(m.group(1), inner, inst.names, belongs,
                              (start, start + len(inner))))
    # 「代號 名稱」 without brackets (a title's 「2603 陽明：多方格局」). The words after a bare
    # code are mostly not a name (「3008 權重過高警示」), so only the provable case counts:
    # the code followed directly by ANOTHER registered instrument's name.
    others = {id(x): x for x in index.values()}.values()
    for m in _BARE_CODE.finditer(text):
        inst = index.get(m.group(1).upper())
        if inst is None:
            continue
        after = text[m.end():]
        if any(after.upper().startswith(n.upper()) for n in inst.names if n):
            continue
        hit = max(((n, i.symbol) for i in others if i.symbol != inst.symbol
                   for n in i.names if len(n) >= _MIN_CJK_PREFIX
                   and after.upper().startswith(n.upper())),
                  key=lambda t: len(t[0]), default=None)
        if hit is not None:
            found.append(Mismatch(m.group(1), after[:len(hit[0])], inst.names, hit[1],
                                  (m.end(), m.end() + len(hit[0]))))
    found.sort(key=lambda x: (x.span or (10**9, 0))[0])
    return found


def feedback_lines(found: Iterable[Mismatch]) -> str:
    """The retry note's lines: 「『聯詠（3008）』——3008 的名稱是 LARGAN、大立光」."""
    lines: list[str] = []
    for mm in found:
        names = "、".join(mm.names)
        line = f"・『{mm.written}（{mm.code}）』——{mm.code} 的名稱是 {names}"
        if mm.belongs_to:
            line += f"；{mm.written} 是 {mm.belongs_to}"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def naming_table(registry: Iterable[NamedInstrument], *, within: str) -> str:
    """The prompt's table — one line per instrument whose code appears in *within* (the
    assembled prompt), 「2330：台積電、TSMC」. Scoped to the prompt so a per-market card's
    input still holds no other market's symbols (the per-market isolation guard); the REPLY
    is checked against the whole registry regardless."""
    lines: list[str] = []
    for inst in sorted(registry, key=lambda i: i.symbol):
        codes = {inst.symbol, inst.symbol.split(".")[0]}
        if inst.names and any(
                re.search(rf"(?<![A-Za-z0-9.]){re.escape(c)}(?![A-Za-z0-9])", within)
                for c in codes):
            lines.append(f"{inst.symbol}：{'、'.join(inst.names)}")
    return "\n".join(lines)


def registry_from_db(conn: sqlite3.Connection) -> list[NamedInstrument]:
    """Every registered instrument's names, read straight from the ``instruments`` table.

    A cross-layer TABLE read by SQL on the shared connection (architecture.md): the table
    is ``data_ingestion``'s, and ``llm_insight`` may not import it. Archived instruments are
    included — a card may still name a company the ledger once held. A database without the
    table, or without the ``aliases`` column yet, reads as names only / no names.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instruments)")}
        if not cols:
            return []
        alias_sql = "aliases" if "aliases" in cols else "'[]'"
        rows = conn.execute(
            f"SELECT symbol, COALESCE(name, '') AS name, {alias_sql} AS aliases "
            "FROM instruments").fetchall()
    except sqlite3.Error:
        return []
    out: list[NamedInstrument] = []
    for symbol, name, raw in rows:
        try:
            aliases = json.loads(raw or "[]")
        except ValueError:
            aliases = []
        out.append(NamedInstrument(
            str(symbol), str(name),
            tuple(str(a) for a in aliases if isinstance(a, str) and a.strip())
            if isinstance(aliases, list) else ()))
    return out
