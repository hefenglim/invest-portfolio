"""Batch B (Moomoo merge) — static-frontend content pins.

Deterministic SOURCE-SCAN tests (no DB, no network, no browser): they read the static
files under ``web/`` and assert the account-merge invariants the automated id-scan cannot
see. ``web/settings.html`` carries NO account ids in its static 帳戶 panel, so the id-scan
is blind to it — this file is the explicit pin.

Batch B merges ``moomoo_my_us`` + ``moomoo_my_my`` into ONE dual-currency ``moomoo_my``
account (settlement USD / funding MYR; markets US+MY; fee rule sets moomoo_us + moomoo_my;
dividends US=DRIP 30% withholding / MY=cash), shipped as ONE release with a boot-time data
migration, so this static content describes the POST-merge world.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/contract/this_file.py -> parents[2] == worktree root (web/ lives here).
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _WORKTREE_ROOT / "web"


def _read(name: str) -> str:
    return (_WEB_DIR / name).read_text(encoding="utf-8")


def _acct_grid(html: str) -> str:
    """The static 帳戶 panel's account-card grid (the file's only ``acct-grid`` block).

    The grid's closing ``</div>`` is the one immediately preceding ``</section>`` (a card's
    own inner ``</div>`` is never directly followed by a section close), so anchoring on
    ``</div>\\s*</section>`` isolates exactly the grid body.
    """
    m = re.search(r'<div class="acct-grid">(.*?)</div>\s*</section>', html, re.DOTALL)
    assert m, "acct-grid block not found in settings.html"
    # Strip HTML comments so descriptive prose (which mentions 'Moomoo MY', 'DRIP', the fee
    # set ids, …) can neither be miscounted as a card nor mask a real regression in the
    # actual card markup.
    return re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL)


def _cards(grid: str) -> list[str]:
    """Split the grid body into per-card content chunks (one per acct-card opening tag)."""
    return grid.split('<div class="acct-card">')[1:]


def test_account_panel_has_exactly_three_cards() -> None:
    """Post-merge the static 帳戶 panel shows THREE cards: tw_broker, schwab, and the one
    merged Moomoo MY (the two legacy Moomoo cards collapse into one)."""
    cards = _cards(_acct_grid(_read("settings.html")))
    assert len(cards) == 3, f"expected exactly 3 account cards after the merge, got {len(cards)}"


def test_merged_card_covers_both_rule_sets_and_both_dividend_models() -> None:
    """The single merged Moomoo MY card must name BOTH fee rule sets (moomoo_us + moomoo_my)
    and BOTH dividend models (US DRIP + MY cash), plus the dual-currency framing."""
    cards = _cards(_acct_grid(_read("settings.html")))
    merged = [c for c in cards if "Moomoo MY" in c]
    assert len(merged) == 1, f"expected exactly one merged 'Moomoo MY' card, got {len(merged)}"
    card = merged[0]
    # Both fee rule sets on the ONE card.
    assert "moomoo_us" in card, "merged card must name the moomoo_us fee rule set"
    assert "moomoo_my" in card, "merged card must name the moomoo_my fee rule set"
    # Both dividend models on the ONE card (US=DRIP / MY=cash).
    assert "DRIP" in card, "merged card must state the US DRIP dividend model"
    assert "現金" in card, "merged card must state the MY cash dividend model"
    # Dual-currency framing: settlement USD, funding MYR, dual market.
    assert "USD" in card and "MYR" in card, "merged card must show settlement USD / funding MYR"


def test_legacy_moomoo_card_labels_are_gone_from_the_panel() -> None:
    """The pre-merge per-market card labels ('Moomoo 美股' / 'Moomoo 馬股') no longer appear
    in the account panel — the collapse to one card actually happened."""
    grid = _acct_grid(_read("settings.html"))
    assert "Moomoo 美股" not in grid, "legacy 'Moomoo 美股' account card should be gone post-merge"
    assert "Moomoo 馬股" not in grid, "legacy 'Moomoo 馬股' account card should be gone post-merge"


def test_names_js_adds_merged_account_and_retains_legacy_ids() -> None:
    """web/names.js gains the merged ``moomoo_my`` display entry and RETAINS the two legacy
    ids (pre-migration snapshots still resolve to a name; T10 may drop them later)."""
    js = _read("names.js")
    assert re.search(r"moomoo_my:\s*\{[^}]*'Moomoo MY'", js), "names.js missing merged moomoo_my"
    assert "moomoo_my_us:" in js, "names.js should retain the legacy moomoo_my_us id"
    assert "moomoo_my_my:" in js, "names.js should retain the legacy moomoo_my_my id"


def test_fee_rule_labels_read_as_rule_sets() -> None:
    """web/settings-fees.js RS_NAMES labels read as RULE SETS (…規則), since one account now
    references two sets; both Moomoo sets remain distinct entries."""
    fees = _read("settings-fees.js")
    for set_id in ("moomoo_us", "moomoo_my"):
        pattern = set_id + r":\s*'[^']*規則'"
        assert re.search(pattern, fees), f"{set_id} RS_NAMES label should read as a rule set"
