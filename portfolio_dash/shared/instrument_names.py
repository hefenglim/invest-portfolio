"""The names an instrument goes by — its registered ``name`` plus its ``aliases``.

Owner ruling 2026-09-30 (item 8, 「登錄名稱＋中文別名」): a card may name an instrument by its
registered name or by any alias (3008 LARGAN is also 大立光), and nothing else. The registry is
the one source of which names are right; this module owns the rules for the alias list itself
(what a valid alias is, which aliases collide with another instrument) and which name a
correction writes. The card-side check lives in ``llm_insight/name_check.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: CJK Unified Ideographs (+ extension A and compatibility) — a name "reads Chinese".
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SPACES = re.compile(r"\s+")
MAX_ALIAS_LEN = 30


class AliasError(ValueError):
    """An alias list that cannot be stored; ``str(exc)`` is the owner-facing zh sentence."""


@dataclass(frozen=True)
class NamedInstrument:
    """The slice of a registry row the name rules need."""

    symbol: str
    name: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Every accepted name: the registered one first, then the aliases."""
        return tuple(n for n in (self.name, *self.aliases) if n.strip())


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def normalize_aliases(raw: Iterable[str], *, symbol: str, name: str) -> list[str]:
    """Trim, collapse spaces, drop blanks, the registered name and duplicates (case-blind).

    Raises :class:`AliasError` for an alias no card could be checked against: all digits (a
    code, not a name — TW/MY codes are digits), the instrument's own symbol, or longer than
    :data:`MAX_ALIAS_LEN`.
    """
    seen = {name.strip().casefold()} if name.strip() else set()
    out: list[str] = []
    for item in raw:
        alias = _SPACES.sub(" ", str(item)).strip()
        if not alias:
            continue
        key = alias.casefold()
        if key in seen:
            continue
        if alias.isdigit():
            raise AliasError(f"別名「{alias}」是數字，看起來是代號而不是名稱")
        if key == symbol.strip().casefold():
            raise AliasError(f"別名「{alias}」就是這檔標的的代號")
        if len(alias) > MAX_ALIAS_LEN:
            raise AliasError(f"別名「{alias}」超過 {MAX_ALIAS_LEN} 個字")
        seen.add(key)
        out.append(alias)
    return out


def alias_conflicts(symbol: str, aliases: Sequence[str],
                    registry: Iterable[NamedInstrument]) -> list[str]:
    """zh sentences for each alias that is ANOTHER instrument's symbol, name or alias.

    An alias shared by two instruments would make 「長榮 (2603)」 and 「長榮 (2618)」 both
    "right", so the check could no longer tell which pairing a card got wrong.
    """
    taken: dict[str, str] = {}
    for inst in registry:
        if inst.symbol == symbol:
            continue
        for label in (inst.symbol, *inst.names):
            taken.setdefault(label.strip().casefold(), f"{inst.symbol}（{inst.name}）")
    return [f"別名「{a}」已屬於 {taken[a.casefold()]}" for a in aliases
            if a.casefold() in taken]


def name_conflict(symbol: str, name: str,
                  registry: Iterable[NamedInstrument]) -> str | None:
    """The zh sentence when *name* is ANOTHER instrument's alias, else ``None``.

    The mirror of :func:`alias_conflicts`: renaming 2618 to 「長榮」 while 2603 answers to it
    would make both pairings "right". Another instrument's registered NAME is not a conflict
    — two share classes of one company (GOOG / GOOGL) really do share it — but an alias was
    given to one instrument on purpose, so a card writing it must mean that one.
    """
    key = name.strip().casefold()
    if not key:
        return None
    for inst in registry:
        if inst.symbol != symbol and any(a.strip().casefold() == key for a in inst.aliases):
            return f"名稱「{name.strip()}」已是 {inst.symbol}（{inst.name}）的別名"
    return None


def preferred_name(inst: NamedInstrument) -> str:
    """The name a correction writes: a Chinese alias when the registered name has no Chinese
    (3008 LARGAN → 大立光, what the cards around it are written in), else the registered name."""
    if not has_cjk(inst.name):
        for alias in inst.aliases:
            if has_cjk(alias):
                return alias
    return inst.name


def by_symbol(registry: Iterable[NamedInstrument]) -> Mapping[str, NamedInstrument]:
    return {inst.symbol: inst for inst in registry}
