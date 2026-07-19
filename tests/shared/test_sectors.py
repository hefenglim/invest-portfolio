"""Unit table for shared.sectors (R6 GICS vocabulary): canonical list + normalization.

Covers every synonym mapping row, canonical-key round-trip, case/whitespace insensitivity,
the R6 fold-ins (Semiconductors → Information Technology, Shipping → Industrials), passthrough
of unrecognized values (NEVER silently rebucketed), blank/None → Unclassified, and the
GICS_SECTOR_KEYS export shape.
"""

import pytest

from portfolio_dash.shared.sectors import (
    _SYNONYMS,
    CANONICAL_KEYS,
    CANONICAL_SECTORS,
    GICS_SECTOR_KEYS,
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
    assert canonical_sector("  tech  ") == "Information Technology"
    assert canonical_sector("TECHNOLOGY") == "Information Technology"
    assert canonical_sector("Financial Services") == "Financials"
    assert canonical_sector("  金融 ") == "Financials"


def test_tech_and_technology_merge() -> None:
    """The original P1① bug: 'Tech' and 'Technology' collapse to ONE key (now GICS IT)."""
    assert (
        canonical_sector("Tech")
        == canonical_sector("Technology")
        == "Information Technology"
    )


def test_semiconductors_fold_into_information_technology() -> None:
    """R6 owner decision: Semiconductors is FOLDED into Information Technology (not a key)."""
    assert canonical_sector("Semiconductors") == "Information Technology"
    assert canonical_sector("Semis") == "Information Technology"
    assert canonical_sector("半導體") == "Information Technology"
    assert canonical_sector("Semiconductors") == canonical_sector("Technology")
    assert "Semiconductors" not in CANONICAL_KEYS


def test_shipping_folds_into_industrials() -> None:
    """R6 owner decision: Shipping is FOLDED into Industrials (not a key)."""
    assert canonical_sector("Shipping") == "Industrials"
    assert canonical_sector("marine") == "Industrials"
    assert canonical_sector("航運") == "Industrials"
    assert "Shipping" not in CANONICAL_KEYS


def test_legacy_healthcare_key_migrates_to_health_care() -> None:
    """The old FU-D31 key 'Healthcare' (no space) maps to the GICS 'Health Care'."""
    assert canonical_sector("Healthcare") == "Health Care"
    assert canonical_sector("醫療保健") == "Health Care"


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
    assert keys[0] == "Information Technology", "dropdown leads with Information Technology"
    assert keys[-1] == "Unclassified", "Unclassified must be the last (catch-all) option"
    assert all(s["key"] and s["zh"] for s in CANONICAL_SECTORS), "every row has key + zh"
    # R6 vocabulary: 11 GICS sectors + ETF + Unclassified = 13 rows.
    assert len(keys) == 13
    assert "Information Technology" in CANONICAL_KEYS
    assert "ETF" in CANONICAL_KEYS
    # The folded-away FU-D31 keys are no longer canonical options.
    assert "Semiconductors" not in CANONICAL_KEYS
    assert "Shipping" not in CANONICAL_KEYS
    assert "Technology" not in CANONICAL_KEYS  # renamed to Information Technology


def test_gics_sector_keys_export() -> None:
    """GICS_SECTOR_KEYS = the 11 GICS keys in dropdown order, excluding ETF + Unclassified."""
    assert GICS_SECTOR_KEYS == tuple(
        s["key"] for s in CANONICAL_SECTORS if s["key"] not in {"ETF", "Unclassified"}
    )
    assert len(GICS_SECTOR_KEYS) == 11
    assert "ETF" not in GICS_SECTOR_KEYS and "Unclassified" not in GICS_SECTOR_KEYS
    assert set(GICS_SECTOR_KEYS) <= CANONICAL_KEYS  # every GICS key is a canonical option
