"""A prompt one-shot must never be a corpus case verbatim (AI-D68).

Found 2026-08-28 while measuring a prompt change: the `daytrade` negative one-shot added
in W4 was, character for character, the input of corpus case
`tw-sameday-roundtrip-NOT-daytrade`. The prompt contained the answer to the test, so the
recorded "daytrade 0/2 -> 11/11" measured MEMORISATION, not generalisation. The STOCK
one-shot reverted in the same round had the same defect against `div-tw-stock`.

This is worse than an ordinary measurement error because it is self-reinforcing: the
obvious way to "fix" a failing corpus case is to paste it into the prompt as an example,
and doing so makes the case pass while teaching the model nothing about cases like it.
The corpus then reports a fix that does not exist, which is precisely the blindness the
corpus was built to remove.

An example is still the right tool -- it just has to be a DIFFERENT instance of the same
shape, so that passing the case requires generalising to it.
"""

import json
import re
from pathlib import Path

from portfolio_dash.llm_insight.official_templates import AI_INPUT_PROMPT_BODY

_CASES = Path(__file__).resolve().parents[1] / "golden" / "ai_extraction" / "cases.json"


def _normalise(text: str) -> str:
    """Collapse whitespace so a reflow cannot smuggle a duplicate past this guard."""
    return " ".join(text.split())


def _example_inputs() -> list[str]:
    body = AI_INPUT_PROMPT_BODY.format(accounts="X", today="2026-08-18", text="T")
    return [_normalise(m) for m in
            re.findall(r"<example_input>(.*?)</example_input>", body, re.S)]


def test_no_one_shot_duplicates_a_corpus_case_input() -> None:
    doc = json.loads(_CASES.read_text(encoding="utf-8"))
    by_input = {_normalise(c["input"]): c["id"] for c in doc["cases"]}
    clashes = [(by_input[e], e) for e in _example_inputs() if e in by_input]
    assert not clashes, (
        "A prompt one-shot is a corpus case verbatim, so that case measures memorisation "
        "rather than extraction. Change the EXAMPLE (different symbol / date / amount, "
        "same shape) -- never the case.\n"
        + "\n".join(f"  case {cid!r} == example {ex!r}" for cid, ex in clashes)
    )


def test_the_guard_can_actually_see_a_duplicate() -> None:
    """The guard's own detection power: a real corpus input must be recognised as one.

    Without this, a bug in `_normalise` or the regex would make the test above pass by
    finding nothing at all -- green because it is blind, the failure mode this whole
    exercise exists to catch.
    """
    doc = json.loads(_CASES.read_text(encoding="utf-8"))
    by_input = {_normalise(c["input"]): c["id"] for c in doc["cases"]}
    sample = doc["cases"][0]
    assert _normalise(sample["input"]) in by_input
    assert _example_inputs(), "no <example_input> blocks found — the regex stopped matching"
