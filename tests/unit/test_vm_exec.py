"""The VM operation log is an append-only audit trail, so its writer is tested.

A wrong entry number silently overwrites the sequence's meaning, and a leaked credential
cannot be un-written — both are failures you would only notice long after the fact.
"""

from scripts.vm_exec import next_entry_number, redact, render_entry, truncate


def test_entry_number_continues_the_sequence() -> None:
    log = "### 27. x\n\n### 28. y\n\n### 29. z\n"
    assert next_entry_number(log) == 30


def test_entry_number_ignores_prose_that_merely_mentions_a_heading() -> None:
    """`###` inside a fenced block or a sentence must not shift the count."""
    log = "### 5. real\n\ntext about ### 99. something\n"
    assert next_entry_number(log) == 6


def test_entry_number_starts_at_one_on_an_unnumbered_log() -> None:
    assert next_entry_number("# Header only\n") == 1


def test_secrets_are_redacted_before_they_reach_the_file() -> None:
    out = redact("DB_PATH=/home/x/db\nOPENROUTER_API_KEY=sk-abc123\nFINMIND_TOKEN: zzz")
    assert "sk-abc123" not in out and "zzz" not in out
    assert "DB_PATH=/home/x/db" in out          # non-secret lines survive verbatim
    assert out.count("<redacted>") == 2


def test_truncate_keeps_both_ends() -> None:
    text = "S" + "m" * 5000 + "E"
    out = truncate(text)
    assert out.startswith("S") and out.endswith("E")
    assert "chars omitted" in out and len(out) < len(text)


def test_short_output_is_not_truncated() -> None:
    assert truncate("hello") == "hello"


def test_render_entry_records_kind_command_and_outcome() -> None:
    entry = render_entry(number=30, when="2026-08-01 13:20 UTC", action="restart demo",
                         write=True, command="systemctl restart x", output="active",
                         rc=0, seconds=2.5, executed=True)
    assert entry.startswith("\n### 30. 2026-08-01 13:20 UTC — restart demo  *(WRITE)*")
    assert "$ systemctl restart x" in entry
    assert "rc=0 · 2.5s · via scripts/vm_exec.py" in entry


def test_log_only_entry_does_not_claim_the_wrapper_ran_it() -> None:
    entry = render_entry(number=31, when="2026-08-01 13:40 UTC", action="console reboot",
                         write=True, command="GCP console: reset instance", output="", rc=0,
                         seconds=0.0, executed=False)
    assert "logged only" in entry
    assert "(no output)" in entry
    assert "*(WRITE)*" in entry


def test_log_only_entry_reports_no_exit_code_or_duration() -> None:
    """It measured nothing, so `rc=0 · 0.0s` would be a fabricated measurement."""
    entry = render_entry(number=31, when="2026-08-01 13:40 UTC", action="hand-run", write=False,
                         command="x", output="ok", rc=0, seconds=0.0, executed=False)
    assert "rc=" not in entry and "0.0s" not in entry
    assert "rc/duration not measured" in entry
