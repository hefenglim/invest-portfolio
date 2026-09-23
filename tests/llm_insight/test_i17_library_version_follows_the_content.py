"""I-17: ``LIBRARY_VERSION`` moves whenever a shipped prompt's version moves.

DEF-037 raised ``ON_ALERT_NOTE_VERSION`` v1 → v2 (the addendum now names WHICH alert fired) and
the library tag stayed ``official-v25`` — so the 「官方有新版本」 signal, which reads the library
tag, never told the owner the default content had changed. ``official_templates.py`` has always
said 「Bump LIBRARY_VERSION when default CONTENT changes」; nothing checked it.

The guard: every per-prompt version tag the library ships (the registry's entries and the
strategy templates) is snapshotted HERE against the library tag it shipped under. Changing any
of them without moving ``LIBRARY_VERSION`` fails; moving ``LIBRARY_VERSION`` fails until the
snapshot below is updated in the same change — so the two can never drift apart again.
"""

from __future__ import annotations

from portfolio_dash.llm_insight import official_templates as ot

#: The library tag and the per-prompt versions it shipped. Update BOTH together.
_SHIPPED: tuple[str, dict[str, str]] = (
    "official-v26 (2026-09-23)",
    {
        "ai_input": "v8", "news_organizer": "v2", "insight_system": "v2",
        "insight_on_alert_note": "v2", "master_score": "v2", "master_calibrate": "v1",
        "master_validate": "v1", "digest_note": "digest-daily-note-v1",
        "ai_instrument_resolve": "v2",
        "strategy:持倉週報策略": "v2.5", "strategy:個股健檢策略": "v2.9",
        "strategy:市場週報策略": "v1.2", "strategy:持倉建議與提點策略": "v3.2",
        "strategy:持倉提點策略": "v1",
    },
)


def _versions() -> dict[str, str]:
    out = {e["key"]: str(e["version"]) for e in ot.PROMPT_REGISTRY
           if not str(e["version"]).startswith("(")}          # "(per …)" = delegated below
    strategies = ot.library_wire()["strategies"]
    assert isinstance(strategies, list)
    for s in strategies:
        out[f"strategy:{s['name']}"] = str(s["version"])
    return out


def test_the_library_tag_moves_with_the_content() -> None:
    tag, shipped = _SHIPPED
    current = _versions()
    if ot.LIBRARY_VERSION == tag:
        changed = {k: (shipped.get(k), v) for k, v in current.items() if shipped.get(k) != v}
        gone = sorted(set(shipped) - set(current))
        assert not changed and not gone, (
            f"a shipped prompt changed version under {tag!r} without a LIBRARY_VERSION bump: "
            f"{changed or gone} — bump official_templates.LIBRARY_VERSION and update _SHIPPED")
    else:
        raise AssertionError(
            f"LIBRARY_VERSION is now {ot.LIBRARY_VERSION!r}: update _SHIPPED to it and to "
            f"the current per-prompt versions {current}")


def test_the_guard_sees_the_on_alert_note() -> None:
    """The measured miss: the on-alert addendum's version is part of what is snapshotted."""
    assert _versions()["insight_on_alert_note"] == ot.ON_ALERT_NOTE_VERSION
