"""Unit tests for shared/whatsnew: version ordering, visibility, monotonic seen-state,
catalog integrity, and CHANGELOG drift (WP-WN, 2026-07-13)."""

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
    get_seen_version,
    is_valid_version,
    set_seen_version,
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
    # The 6 NEWEST are kept; the oldest (0.1.12) drops off.
    assert "0.1.18" in out
    assert "0.1.12" not in out


# --- monotonic seen-state ---------------------------------------------------


def test_seen_version_defaults_to_seed() -> None:
    conn = _mem_conn()
    assert get_seen_version(conn) == "0"


def test_set_seen_version_advances_and_persists() -> None:
    conn = _mem_conn()
    assert set_seen_version(conn, "0.1.17", now=_NOW) == "0.1.17"
    assert get_seen_version(conn) == "0.1.17"


def test_set_seen_version_is_monotonic() -> None:
    conn = _mem_conn()
    set_seen_version(conn, "0.1.17", now=_NOW)
    # A regressing (lower) write keeps the higher stored value.
    assert set_seen_version(conn, "0.1.13", now=_NOW) == "0.1.17"
    assert get_seen_version(conn) == "0.1.17"
    # Equal is a no-op; a strictly higher value advances.
    assert set_seen_version(conn, "0.1.17", now=_NOW) == "0.1.17"
    assert set_seen_version(conn, "0.1.18", now=_NOW) == "0.1.18"
    assert get_seen_version(conn) == "0.1.18"


def test_corrupt_stored_seen_version_degrades_to_seed() -> None:
    # A legacy/hand-edited row holding a non-version string must not 500 the panel:
    # read degrades to the never-seen seed, and a subsequent write recovers cleanly.
    conn = _mem_conn()
    set_seen_version(conn, "0.1.17", now=_NOW)  # creates + seeds the table
    conn.execute("UPDATE whatsnew_config SET seen_version = 'not-a-version' WHERE id = 1")
    conn.commit()
    assert get_seen_version(conn) == "0"
    assert set_seen_version(conn, "0.1.17", now=_NOW) == "0.1.17"
    assert get_seen_version(conn) == "0.1.17"


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

    This keeps the catalog honest and stays green while the unshipped v0.1.18 entry
    exists (it is > current, so no heading is required yet).
    """
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    current_key = _version_key(portfolio_dash.__version__)
    catalog_versions = {f.version for f in CATALOG}
    for version in catalog_versions:
        if _version_key(version) <= current_key:
            assert f"## [v{version}]" in changelog, f"no CHANGELOG heading for v{version}"
