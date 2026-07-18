"""Unit table for shared.sectors (FU-D31): canonical vocabulary + read-time normalization.

Covers every synonym mapping row, canonical-key round-trip, case/whitespace insensitivity,
passthrough of unrecognized values (NEVER silently rebucketed), and blank/None → Unclassified.
"""

import pytest

from portfolio_dash.shared.sectors import (
    _SYNONYMS,
    CANONICAL_KEYS,
    CANONICAL_SECTORS,
    UNCLASSIFIED,
    canonical_sector,
)


@pytest.mark.parametrize(("raw", "expected"), sorted(_SYNONYMS.items()))
def test_every_synonym_maps_to_its_canonical_key(raw: str, expected: str) -> None:
    assert canonical_sector(raw) == expected
    # the target of every synonym is itself a real canonical key
    assert expected in CANONICAL_KEYS


@pytest.mark.parametrize("entry", CANONICAL_SECTORS)
def test_canonical_keys_round_trip_to_themselves(entry: dict[str, str]) -> None:
    """Exact canonical input is stable (idempotent)."""
    assert canonical_sector(entry["key"]) == entry["key"]


def test_case_and_whitespace_insensitive() -> None:
    assert canonical_sector("  tech  ") == "Technology"
    assert canonical_sector("TECHNOLOGY") == "Technology"
    assert canonical_sector("Financial Services") == "Financials"
    assert canonical_sector("  金融 ") == "Financials"


def test_tech_and_technology_merge() -> None:
    """The exact P1① bug: 'Tech' and 'Technology' must collapse to ONE key."""
    assert canonical_sector("Tech") == canonical_sector("Technology") == "Technology"


def test_semiconductors_stay_separate_from_technology() -> None:
    """Deliberate distinct category — NOT folded into Technology."""
    assert canonical_sector("Semiconductors") == "Semiconductors"
    assert canonical_sector("Semis") == "Semiconductors"
    assert canonical_sector("Technology") != canonical_sector("Semiconductors")


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
def test_blank_or_none_is_unclassified(blank: str | None) -> None:
    assert canonical_sector(blank) == UNCLASSIFIED == "Unclassified"


@pytest.mark.parametrize("unknown", ["Electronics", "Optoelectronics", "光電", "MegaCorp42"])
def test_unknown_values_pass_through_unchanged(unknown: str) -> None:
    """An unrecognized non-empty sector is preserved verbatim (never rebucketed)."""
    assert canonical_sector(unknown) == unknown
    assert unknown not in CANONICAL_KEYS  # and it is NOT a canonical option


def test_unknown_value_is_trimmed_but_not_altered() -> None:
    assert canonical_sector("  Electronics  ") == "Electronics"


def test_vocabulary_shape_and_ordering() -> None:
    keys = [s["key"] for s in CANONICAL_SECTORS]
    assert len(keys) == len(set(keys)), "duplicate canonical key"
    assert keys[-1] == "Unclassified", "Unclassified must be the last (catch-all) option"
    assert all(s["key"] and s["zh"] for s in CANONICAL_SECTORS), "every row has key + zh"
    assert "Technology" in CANONICAL_KEYS and "Semiconductors" in CANONICAL_KEYS
