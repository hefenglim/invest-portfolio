"""Unit tests for the shared secret masker (review I-2).

One masker, used by both pricing/datasources_store and api/routers/llm_settings.
The short-key guard (``len <= 6 -> "•••"``) is the bug the old ``mask_token`` lacked:
a 5-char key would have produced ``"abc•••cde"`` (overlapping/re-exposed chars).
"""

import pytest

from portfolio_dash.shared.masking import mask_secret


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),  # null contract
        ("", None),  # empty -> None (same as null)
        ("abcde", "•••"),  # len 5: short-key guard (the datasources bug)
        ("abcdef", "•••"),  # len 6: boundary still fully masked
        ("abcdefg", "abc•••efg"),  # len 7: prefix3 + ••• + suffix3
        ("sk-1234567890", "sk-•••890"),  # typical key
    ],
)
def test_mask_secret(value: str | None, expected: str | None) -> None:
    assert mask_secret(value) == expected
