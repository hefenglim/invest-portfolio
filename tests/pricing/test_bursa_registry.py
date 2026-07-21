"""Unit tests for the baked Bursa Malaysia code -> official-short-name registry (W1 batch-A).

The registry lets a valid 4-digit Bursa code verify OFFLINE (the only live MY verifier,
yfinance ``.KL``, lacks many counters), which is what restores ``status:"resolved"`` for the
weak-MY-resolve fix. These tests pin normalization, leading-zero significance, hit/miss, and a
table-sanity floor that proves the baked data is the real directory, not a stub.
"""

import re

from portfolio_dash.pricing import bursa_registry as br


def test_known_code_hit() -> None:
    assert br.bursa_name("1155") == "MAYBANK"
    assert br.bursa_name("5347") == "TENAGA"
    assert br.bursa_name("5249") == "IOIPG"


def test_leading_zero_is_significant() -> None:
    # ACE-market codes KEEP the leading zero: "0166" is Inari; "166" is not a valid code.
    assert br.bursa_name("0166") == "INARI"
    assert br.bursa_name("166") is None


def test_normalizes_strip_and_upper() -> None:
    assert br.bursa_name("  1155  ") == "MAYBANK"
    assert br.bursa_name("0166\n") == "INARI"
    # upper() is applied uniformly (numeric codes are unaffected but must not crash).
    assert br.bursa_name(" 1155 ") == br.bursa_name("1155")


def test_unknown_code_miss() -> None:
    assert br.bursa_name("9999") is None   # 4-digit but unlisted
    assert br.bursa_name("") is None
    assert br.bursa_name("   ") is None
    assert br.bursa_name("ABCD") is None
    assert br.bursa_name("AAPL") is None


def test_table_sanity_is_the_real_list_not_a_stub() -> None:
    table = br.BURSA_COMPANIES
    # the real Main + ACE equity universe is ~1000+; the floor is far above any stub.
    assert len(table) > 800
    for code, name in table.items():
        assert re.fullmatch(r"\d{4}", code), f"non-4-digit key {code!r}"
        assert name and name.strip() == name, f"bad short name for {code}: {name!r}"


def test_provenance_exemplars_all_present_with_official_short_names() -> None:
    # the 7 counters cross-verified byte-identical to Bursa when the mirror was baked; the
    # resolve prompt cites the same code set, so a drift here is a data-source regression.
    for code, short in [
        ("1155", "MAYBANK"), ("1295", "PBBANK"), ("5347", "TENAGA"), ("1023", "CIMB"),
        ("0166", "INARI"), ("1961", "IOICORP"), ("5249", "IOIPG"),
    ]:
        assert br.BURSA_COMPANIES[code] == short
        assert br.bursa_name(code) == short
