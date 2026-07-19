"""Completeness + integrity of the site-wide prompt registry (FU-D30, 2026-07-18).

The registry (``official_templates.PROMPT_REGISTRY``) is the single authoritative index of
every prompt the app sends to an LLM. These tests guard the invariant "no feature hardcodes
a prompt outside the registry" two ways:

1. **Structural** — every registry entry is well-formed and its ``default_constant`` resolves
   to a real, non-empty constant in ``official_templates`` (code-owned defaults have a home;
   user-editable entries name their DB storage).
2. **Call-site pin** — a scan of ``portfolio_dash/`` for the FOUR LLM-completion call patterns
   discovers the set of files that talk to an LLM, and asserts it equals a pinned map. A NEW
   call site (or a removed one) fails this test, forcing the author to register the prompt in
   PROMPT_REGISTRY (or record a documented exemption) here. This is the honest, maintainable
   guard: it tracks the real call sites, not a hand-copied list that silently rots.

MAINTENANCE: when you add/remove an LLM call site, update ``EXPECTED_CALL_SITES`` below AND
add the prompt to ``PROMPT_REGISTRY`` (see the how-to at the top of official_templates.py).
"""

import re
from pathlib import Path

import portfolio_dash
from portfolio_dash.llm_insight import master
from portfolio_dash.llm_insight import official_templates as ot
from portfolio_dash.shared.sectors import GICS_SECTOR_KEYS

_PKG_DIR = Path(portfolio_dash.__file__).resolve().parent

# The names by which a prompt reaches an LLM in this codebase: the three ``shared.llm``
# completion helpers + the raw ``litellm.completion`` seam. We match the IDENTIFIER (not just
# a direct ``name(`` call) because a call site may reach the LLM through indirection — e.g.
# ``data_ingestion/agents.py`` injects ``complete_structured`` as a default ``completer`` and
# calls it as ``completer(...)``. A file referencing any of these names is treated as an LLM
# call site; each such file is pinned below to its registry key(s) or a documented exemption.
_CALL_RE = re.compile(
    r"\b(?:complete_text|complete_structured_meta|complete_structured|litellm\.completion)\b"
)

# Every file under portfolio_dash/ that issues an LLM completion, mapped to the registry
# key(s) whose prompt it sends — or a documented EXEMPT reason (no prompt content of record).
_EXEMPT = "EXEMPT"
EXPECTED_CALL_SITES: dict[str, list[str] | str] = {
    # the completion helpers themselves — definitions, carry no prompt content.
    "shared/llm.py": f"{_EXEMPT}: shared.llm completion-helper definitions (no prompt)",
    "data_ingestion/agents.py": ["ai_input"],
    "news/organizer.py": ["news_organizer"],
    # assemble.py composes system + strategy + calibration into generate.py's single call.
    "llm_insight/generate.py": [
        "insight_system",
        "insight_strategy",
        "insight_calibration",
        "insight_on_alert_note",
    ],
    "llm_insight/master.py": ["master_score", "master_calibrate", "master_validate"],
    "api/digest_service.py": ["digest_note"],
    # R6-B: unified 「AI 標的判讀」 — raw input + market → local code + name + GICS sector
    # (+ optional industry) in one reply; the real lookup re-verifies before any auto-fill.
    "api/routers/instruments.py": ["ai_instrument_resolve"],
    # prompt tester: user-supplied body + the (registered) insight_system system prompt.
    "api/routers/prompts.py": f"{_EXEMPT}: /prompts/test — user body + insight_system prompt",
    # model connectivity probe: a literal "ping", not a prompt of record.
    "api/routers/llm_settings.py": f"{_EXEMPT}: model-test connectivity ping ('ping')",
}


def _scan_call_site_files() -> set[str]:
    """Return the portfolio_dash-relative posix paths of every module issuing a completion."""
    found: set[str] = set()
    for path in _PKG_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _CALL_RE.search(text):
            found.add(path.relative_to(_PKG_DIR).as_posix())
    return found


def test_call_sites_match_pinned_set() -> None:
    """The real LLM call sites must exactly match the pinned map (the completeness guard)."""
    found = _scan_call_site_files()
    expected = set(EXPECTED_CALL_SITES)
    assert found == expected, (
        "LLM completion call sites changed. Register any NEW prompt in "
        "official_templates.PROMPT_REGISTRY and update EXPECTED_CALL_SITES here.\n"
        f"  unregistered (new): {sorted(found - expected)}\n"
        f"  stale (removed):    {sorted(expected - found)}"
    )


def test_every_call_site_key_is_registered() -> None:
    """Each non-exempt call site maps only to keys that exist in the registry."""
    reg_keys = {e["key"] for e in ot.PROMPT_REGISTRY}
    for path, mapped in EXPECTED_CALL_SITES.items():
        if isinstance(mapped, str):
            assert mapped.startswith(_EXEMPT), f"{path}: exemption must be documented"
            continue
        for key in mapped:
            assert key in reg_keys, f"{path} references unknown registry key {key!r}"


def test_every_registry_key_is_used_by_a_call_site() -> None:
    """No orphan entries: every registry key is referenced by at least one call site."""
    referenced: set[str] = set()
    for mapped in EXPECTED_CALL_SITES.values():
        if isinstance(mapped, list):
            referenced.update(mapped)
    reg_keys = {e["key"] for e in ot.PROMPT_REGISTRY}
    assert reg_keys == referenced, (
        f"registry keys not wired to a call site: {sorted(reg_keys - referenced)}; "
        f"call-site keys missing from registry: {sorted(referenced - reg_keys)}"
    )


def test_registry_entries_are_well_formed() -> None:
    """Keys unique; tiers valid; storage/default_constant consistent with the tier."""
    keys = [e["key"] for e in ot.PROMPT_REGISTRY]
    assert len(keys) == len(set(keys)), "duplicate registry key"
    for e in ot.PROMPT_REGISTRY:
        assert e["tier"] in ("code-owned", "user-editable", "runtime-generated")
        assert e["feature"] and e["version"] and e["agent"] and e["call_site"]
        if e["tier"] == "code-owned":
            assert e["storage"] == "", f"{e['key']}: code-owned prompt has no DB storage"
            assert e["default_constant"], f"{e['key']}: code-owned needs a default constant"
        elif e["tier"] == "user-editable":
            assert e["storage"], f"{e['key']}: user-editable prompt must name its DB storage"
            assert e["default_constant"], f"{e['key']}: user-editable needs a library default"
        else:  # runtime-generated
            assert e["storage"], f"{e['key']}: runtime prompt must name its DB storage"


def test_default_constants_resolve_to_nonempty_content() -> None:
    """Every named default_constant is a real, non-empty attribute of official_templates."""
    for e in ot.PROMPT_REGISTRY:
        name = e["default_constant"]
        if not name:
            continue
        assert hasattr(ot, name), f"{e['key']}: missing constant {name!r} in official_templates"
        value = getattr(ot, name)
        if isinstance(value, str):
            assert value.strip(), f"{name} is empty"
        elif isinstance(value, list):
            assert value, f"{name} is empty"  # STRATEGY_TEMPLATES
        else:  # pragma: no cover - defensive
            raise AssertionError(f"{name} is neither a str body nor a template list")


def test_code_owned_versions_pin_the_module_version_tags() -> None:
    """Each code-owned entry's version equals the module's sibling ``*_VERSION`` constant."""
    version_attr: dict[str, str] = {
        "AI_INPUT_PROMPT_BODY": "AI_INPUT_PROMPT_VERSION",
        "ON_ALERT_NOTE": "ON_ALERT_NOTE_VERSION",
        "MASTER_SCORE_SYSTEM": "MASTER_SCORE_PROMPT_VERSION",
        "MASTER_CALIBRATION_SYSTEM": "MASTER_CALIBRATION_PROMPT_VERSION",
        "MASTER_VALIDATE_SYSTEM": "MASTER_VALIDATE_PROMPT_VERSION",
        "DIGEST_NOTE_PROMPT_BODY": "DIGEST_NOTE_PROMPT_VERSION",
        "AI_INSTRUMENT_RESOLVE_PROMPT": "AI_INSTRUMENT_RESOLVE_PROMPT_VERSION",
    }
    for e in ot.PROMPT_REGISTRY:
        if e["tier"] != "code-owned":
            continue
        attr = version_attr[e["default_constant"]]
        assert e["version"] == getattr(ot, attr), f"{e['key']}: version != {attr}"


def test_migrated_prompts_are_byte_identical_at_their_call_sites() -> None:
    """The moved constants are the SAME objects the call sites use (no content drift)."""
    # master.py imports the three master prompts under its historical private names.
    # Direct access (per ruff B009) trips strict no_implicit_reexport — they are aliased
    # imports, not re-exports — so the targeted ignore is the honest resolution.
    assert master._SCORE_SYSTEM == ot.MASTER_SCORE_SYSTEM  # type: ignore[attr-defined]
    assert master._CALIBRATION_SYSTEM == ot.MASTER_CALIBRATION_SYSTEM  # type: ignore[attr-defined]
    assert master._VALIDATE_SYSTEM == ot.MASTER_VALIDATE_SYSTEM  # type: ignore[attr-defined]
    # the safety-lock phrasing survived the move (guarded by test_master too).
    assert "幣別混算" in ot.MASTER_CALIBRATION_SYSTEM
    assert "越權" in ot.MASTER_VALIDATE_SYSTEM


def test_digest_note_prompt_formats_without_stray_placeholders() -> None:
    """``DIGEST_NOTE_PROMPT_BODY`` has exactly one ``{numbers}`` slot and formats cleanly."""
    rendered = ot.DIGEST_NOTE_PROMPT_BODY.format(numbers='{"a": 1}')
    assert '{"a": 1}' in rendered  # a JSON value with braces substitutes safely
    assert "你是投資組合摘要助理" in rendered
    assert "{numbers}" not in rendered  # the only placeholder was consumed


def test_instrument_resolve_prompt_embeds_every_gics_sector_key() -> None:
    """Drift guard (R6-B): the unified resolve prompt embeds the GICS vocabulary from its
    single source (shared/sectors.GICS_SECTOR_KEYS), so EVERY key must appear VERBATIM in the
    prompt — a vocabulary change that did not propagate into the prompt fails here."""
    prompt = ot.AI_INSTRUMENT_RESOLVE_PROMPT
    for key in GICS_SECTOR_KEYS:
        assert key in prompt, f"GICS key {key!r} missing from AI_INSTRUMENT_RESOLVE_PROMPT"
    # only the {query}/{market} runtime placeholders interpolate; the example JSON braces stay
    # escaped, so .format renders cleanly with no stray placeholders.
    rendered = prompt.format(query="聯電", market="TW")
    assert "聯電" in rendered and "TW" in rendered
    assert '{{"symbol"' not in rendered


def test_code_owned_prompts_stay_out_of_user_facing_library_wire() -> None:
    """Code-owned prompts are NOT exposed in the editable ``library_wire`` payload."""
    wire_text = str(ot.library_wire())
    for constant in (
        ot.AI_INPUT_PROMPT_BODY,
        ot.DIGEST_NOTE_PROMPT_BODY,
        ot.MASTER_SCORE_SYSTEM,
    ):
        assert constant not in wire_text
