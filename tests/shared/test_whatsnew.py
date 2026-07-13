"""Unit tests for shared/whatsnew: version ordering, visibility, per-feature seen-state,
legacy migration, catalog integrity, and CHANGELOG drift (WP-WN; round 3, 2026-07-13)."""

import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

import portfolio_dash
from portfolio_dash.shared.whatsnew import (
    _MAX_VERSIONS,
    CATALOG,
    VERSION_DATES,
    _version_key,
    all_visible_versions,
    ensure_whatsnew_seeded,
    get_seen_keys,
    is_valid_version,
    known_feature_keys,
    mark_seen,
    visible_versions,
)

_NOW = datetime(2026, 7, 13, 12, 0, 0)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _REPO_ROOT / "web"
# The settings page's own tab ids (mirrors the valid list in web/settings.html).
_SETTINGS_TABS = {"llm", "prompts", "scheduler", "accounts", "datasources", "alerts", "exports"}


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# --- version ordering -------------------------------------------------------


def test_version_key_orders_double_digit_after_single() -> None:
    # The classic trap: lexical "0.1.9" > "0.1.10"; numeric key must invert that.
    assert _version_key("0.1.9") < _version_key("0.1.10")
    assert _version_key("0.1.17") < _version_key("0.1.18")
    assert _version_key("0") < _version_key("0.1.12")


def test_is_valid_version() -> None:
    assert is_valid_version("0.1.17")
    assert is_valid_version("0")
    assert is_valid_version("10.20.30")
    assert not is_valid_version("v0.1.17")
    assert not is_valid_version("0.1.x")
    assert not is_valid_version("")
    assert not is_valid_version("0.1.")


# --- visibility -------------------------------------------------------------


def test_visible_versions_hides_unshipped_and_orders_newest_first() -> None:
    out = visible_versions("0.1.17")
    # The v0.1.18 catalog entry stays hidden until __version__ is bumped.
    assert "0.1.18" not in out
    assert out[0] == "0.1.17"  # newest first
    assert out == sorted(out, key=_version_key, reverse=True)


def test_visible_versions_bounded_to_max() -> None:
    # With a high current, every catalog version qualifies but the list caps at 6.
    out = visible_versions("9.9.9")
    assert len(out) <= _MAX_VERSIONS
    assert len(out) == _MAX_VERSIONS  # the catalog seeds >6 versions
    # The 6 NEWEST are kept; the older versions drop off.
    assert "0.1.18" in out
    assert "0.1.12" not in out


def test_all_visible_versions_uncapped_and_newest_first() -> None:
    current = portfolio_dash.__version__
    out = all_visible_versions(current)
    # UNCAPPED (feeds the history browser): backfill added the older versions, so the
    # list is strictly longer than the panel cap.
    assert len(out) > _MAX_VERSIONS
    assert out == sorted(out, key=_version_key, reverse=True)
    # v0.1.18 (unshipped) stays hidden; the oldest backfilled version is present.
    assert "0.1.18" not in out
    assert "0.1.0" in out
    assert out[0] == "0.1.17"


# --- per-feature seen-state -------------------------------------------------


def test_get_seen_keys_default_empty() -> None:
    conn = _mem_conn()
    assert get_seen_keys(conn) == set()


def test_mark_seen_records_and_returns_full_set() -> None:
    conn = _mem_conn()
    keys = ["0.1.17:market-risk-alerts", "0.1.17:target-weights"]
    out = mark_seen(conn, keys, now=_NOW)
    assert set(keys) <= out
    assert get_seen_keys(conn) == out


def test_mark_seen_is_idempotent() -> None:
    conn = _mem_conn()
    keys = ["0.1.17:market-risk-alerts"]
    first = mark_seen(conn, keys, now=_NOW)
    # Re-marking the same key returns the same set — no duplicates, no error.
    second = mark_seen(conn, keys, now=_NOW)
    assert second == first
    # A partial new add extends the set without disturbing the existing keys.
    third = mark_seen(conn, ["0.1.16:channel-setup-guides"], now=_NOW)
    assert "0.1.16:channel-setup-guides" in third
    assert set(keys) <= third


def test_mark_seen_empty_is_noop() -> None:
    conn = _mem_conn()
    assert mark_seen(conn, [], now=_NOW) == set()


def test_known_feature_keys_are_exactly_the_visible_window() -> None:
    current = portfolio_dash.__version__
    known = known_feature_keys(current)
    visible = set(visible_versions(current))
    expected = {f"{f.version}:{f.id}" for f in CATALOG if f.version in visible}
    assert known == expected
    # Every known key belongs to a visible version.
    for key in known:
        assert key.split(":")[0] in visible
    # A backfilled (out-of-window) feature key is NOT known — it can never be POSTed seen.
    assert "0.1.0:dashboard-launch" not in known


def test_migration_from_legacy_seen_version() -> None:
    # A pre-round-3 install acknowledged up to a version via the legacy single-row table;
    # the one-time migration folds that into per-feature seen rows on first access.
    conn = _mem_conn()
    ensure_whatsnew_seeded(conn)  # creates both tables + seeds the legacy "0" row
    conn.execute("UPDATE whatsnew_config SET seen_version = '0.1.14' WHERE id = 1")
    conn.commit()
    keys = get_seen_keys(conn)  # triggers the migration
    at_or_below = {
        f"{f.version}:{f.id}"
        for f in CATALOG
        if _version_key(f.version) <= _version_key("0.1.14")
    }
    above = {
        f"{f.version}:{f.id}"
        for f in CATALOG
        if _version_key(f.version) > _version_key("0.1.14")
    }
    assert at_or_below  # sanity: the migration had something to do
    assert at_or_below <= keys  # everything up to the legacy ack is now seen
    assert not (above & keys)  # newer features stay unseen
    # Idempotent: re-running does not change the set.
    assert get_seen_keys(conn) == keys


def test_migration_skips_fresh_and_corrupt_legacy_rows() -> None:
    # Fresh install (legacy "0") migrates nothing.
    fresh = _mem_conn()
    ensure_whatsnew_seeded(fresh)
    assert get_seen_keys(fresh) == set()
    # A hand-corrupted non-version legacy row must not crash — it just migrates nothing.
    corrupt = _mem_conn()
    ensure_whatsnew_seeded(corrupt)
    corrupt.execute("UPDATE whatsnew_config SET seen_version = 'not-a-version' WHERE id = 1")
    corrupt.commit()
    assert get_seen_keys(corrupt) == set()


# --- catalog integrity ------------------------------------------------------


def test_catalog_versions_are_valid_format() -> None:
    for feature in CATALOG:
        assert is_valid_version(feature.version), feature.version


def test_catalog_ids_unique_within_version() -> None:
    by_version: dict[str, list[str]] = {}
    for feature in CATALOG:
        by_version.setdefault(feature.version, []).append(feature.id)
    for version, ids in by_version.items():
        dupes = [i for i, n in Counter(ids).items() if n > 1]
        assert not dupes, f"duplicate ids in v{version}: {dupes}"


def test_catalog_targets_nonempty_and_require_href() -> None:
    # target is an optional CSS selector for the in-page callout/flash anchor. When set it
    # must be a non-empty string, and it only ever accompanies an href (it points WHERE a
    # feature lives on its page — meaningless without a page to arrive on).
    for feature in CATALOG:
        if feature.target is not None:
            assert isinstance(feature.target, str) and feature.target.strip(), feature.id
            assert feature.href is not None, feature.id


def test_catalog_href_requires_target() -> None:
    # The guarantee (round 4): every feature with an href MUST also carry a non-empty
    # target, so the 前往 jump lands a precise in-page callout + flash on its own page.
    # (href=None backfilled features are exempt — they only surface in 版本發佈資訊.)
    for feature in CATALOG:
        if feature.href is not None:
            assert feature.target is not None and feature.target.strip(), feature.id


def test_v0_1_18_entries_valid_and_hidden_at_current_version() -> None:
    # This release's own catalog entries: each is well-formed (href + target) and stays
    # HIDDEN from the ✦ panel while __version__ is still 0.1.17 (visible_versions filters
    # a version newer than current). They surface only once the ship bump lands.
    entries = [f for f in CATALOG if f.version == "0.1.18"]
    assert len(entries) >= 5  # what's-new panel + history browser + 3 report exports (+rebalance)
    ids = {f.id for f in entries}
    for expected in (
        "version-history-browser",
        "rebalance-combined",
        "rebalance-report-export",
        "holdings-report-export",
        "ledger-report-export",
    ):
        assert expected in ids
    # Every entry with an href carries a non-empty target (the round-4 guarantee).
    for f in entries:
        if f.href is not None:
            assert f.target and f.target.strip(), f.id
    # Hidden at the current shipped version.
    assert "0.1.18" not in visible_versions("0.1.17")
    assert "0.1.18" not in all_visible_versions("0.1.17")


def test_catalog_version_dates_present_for_shipped_versions() -> None:
    current_key = _version_key(portfolio_dash.__version__)
    for feature in CATALOG:
        if _version_key(feature.version) <= current_key:
            assert feature.version in VERSION_DATES, feature.version


def test_catalog_hrefs_point_at_existing_pages_and_valid_tabs() -> None:
    for feature in CATALOG:
        if feature.href is None:
            continue
        page, _, frag = feature.href.partition("#")
        assert page, f"{feature.id}: href has no page part"
        assert (_WEB_DIR / page).is_file(), f"{feature.id}: missing web/{page}"
        if page == "settings.html" and frag:
            assert frag in _SETTINGS_TABS, f"{feature.id}: unknown settings tab #{frag}"


def test_catalog_versions_have_changelog_headings() -> None:
    """Every catalog version <= current __version__ must have a CHANGELOG heading.

    Direction 1 (catalog -> changelog): keeps the catalog honest and stays green while the
    unshipped v0.1.18 entry exists (it is > current, so no heading is required yet).
    """
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    current_key = _version_key(portfolio_dash.__version__)
    catalog_versions = {f.version for f in CATALOG}
    for version in catalog_versions:
        if _version_key(version) <= current_key:
            assert f"## [v{version}]" in changelog, f"no CHANGELOG heading for v{version}"


def test_every_changelog_version_has_a_catalog_entry() -> None:
    """Full-coverage drift (direction 2: changelog -> catalog).

    Every shipped CHANGELOG version with 0.1.0 <= v <= current must have a CATALOG entry,
    so the history browser tells the complete release story. v0.0.0 (2026-06-05 pre-release
    scaffold) is intentionally below the 0.1.0 floor and excluded.
    """
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading_versions = set(re.findall(r"^## \[v([0-9]+(?:\.[0-9]+)*)\]", changelog, re.M))
    current_key = _version_key(portfolio_dash.__version__)
    floor_key = _version_key("0.1.0")
    catalog_versions = {f.version for f in CATALOG}
    for version in heading_versions:
        if floor_key <= _version_key(version) <= current_key:
            assert version in catalog_versions, f"no CATALOG entry for shipped v{version}"
