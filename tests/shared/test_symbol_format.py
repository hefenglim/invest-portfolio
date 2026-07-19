"""Unit tests for the single-source per-market code SHAPE patterns (R6-A)."""

from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.symbol_format import (
    looks_like_market_code,
    matches_market_format,
)


def test_tw_code_shapes() -> None:
    assert matches_market_format("2330", Market.TW)  # ordinary 4-digit
    assert matches_market_format("00878B", Market.TW)  # 5-digit ETF + preferred/bond suffix
    assert not matches_market_format("AAPL", Market.TW)  # a US ticker is not TW-shaped


def test_us_code_shapes() -> None:
    assert matches_market_format("AAPL", Market.US)
    assert matches_market_format("BRK.B", Market.US)  # class-share dotted form
    assert not matches_market_format("2330", Market.US)  # digits are not US-shaped


def test_my_code_shapes() -> None:
    assert matches_market_format("5225", Market.MY)  # 4-digit Bursa code
    assert not matches_market_format("00878B", Market.MY)  # 6-char is not MY-shaped


def test_case_and_whitespace_normalization() -> None:
    # lower-case + surrounding whitespace normalize before matching.
    assert matches_market_format("aapl", Market.US)
    assert matches_market_format("  aapl  ", Market.US)
    assert matches_market_format("brk.b", Market.US)


def test_looks_like_market_code_true_for_any_market_shape() -> None:
    assert looks_like_market_code("2330")  # TW (and MY) code
    assert looks_like_market_code("aapl")  # US ticker (case-normalized)
    assert looks_like_market_code("00878B")  # TW ETF code


def test_looks_like_market_code_apple_word_is_a_us_false_positive() -> None:
    # "APPLE" is 5 letters -> matches the US ticker shape. This false positive is BY DESIGN:
    # it lands in the register-first / (next-wave) AI-resolve flow rather than a fuzzy coercion.
    assert looks_like_market_code("APPLE")


def test_looks_like_market_code_false_for_a_name() -> None:
    # A CJK company name is NOT code-shaped -> routes to name-suggestion handling.
    assert not looks_like_market_code("聯華電子")
    # A multi-word English name is likewise not a code shape.
    assert not looks_like_market_code("ZZ Unknown Corp")


def test_5225_matches_both_tw_and_my() -> None:
    # A bare 4-digit code fits both TW and MY shapes — fine: looks_like_market_code is any-market
    # (the account's market disambiguates downstream; this is only a shape gate).
    assert matches_market_format("5225", Market.TW)
    assert matches_market_format("5225", Market.MY)
    assert looks_like_market_code("5225")
