"""PERF spec 2026-10-18 §14 (plan §6.3, package F): web/settlement.js's static contract.

The chip, the panel and the prompt word a few things themselves (the panel's furniture) and
MIRROR five small vocabularies of ``portfolio/settlement_models.py`` (ledger tables,
operations, actors, run reasons, phases): a mirror that drifts names a table or a phase
differently on the page than in the report the server wrote, so every mirrored word is pinned
to Python's here. The file is loaded by shell.js on every page (P10) and has no HTML tag;
every status read is ``background``; every chip / banner / mark state is a class with a
stylesheet rule (DEF-077). The api.js transport it rides on is
``test_perf_web_transport.py``.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from portfolio_dash.portfolio import settlement_models as sm

_WEB = Path(__file__).resolve().parents[2] / "web"
_SETTLE = (_WEB / "settlement.js").read_text(encoding="utf-8")
_SHELL = (_WEB / "shell.js").read_text(encoding="utf-8")
_CSS = (_WEB / "styles.css").read_text(encoding="utf-8")


def _js_object(name: str) -> dict[str, str]:
    """The `key: 'value'` pairs of `const NAME = { … };` in settlement.js."""
    m = re.search(r"const " + name + r" = \{(.*?)\};", _SETTLE, re.S)
    assert m, f"{name} not found in settlement.js"
    return dict(re.findall(r"(\w+): '([^']*)'", m.group(1)))


# ------------------------------------------------------------------ mirrored vocabularies


def test_the_change_vocabularies_mirror_python() -> None:
    assert _js_object("CHANGE_TABLE_ZH") == sm.CHANGE_TABLE_ZH
    assert _js_object("CHANGE_OP_ZH") == sm.CHANGE_OP_ZH


def test_the_run_reasons_and_phases_mirror_python() -> None:
    assert _js_object("RUN_REASON_ZH") == sm.RUN_REASON_ZH
    assert f"const JOB_REASON_ZH = '{sm.JOB_REASON_ZH}';" in _SETTLE
    assert _js_object("PHASE_ZH") == dict(sm.PHASES)


def test_the_actor_words_mirror_actor_text() -> None:
    """settlement_models.actor_text is the owner; the JS twin uses the same four words."""
    words = {
        "ACTOR_EXTERNAL_ZH": sm.actor_text(None),
        "ACTOR_GUEST_ZH": sm.actor_text(sm.ACTOR_GUEST),
        "ACTOR_BOOT_ZH": sm.actor_text(sm.ACTOR_BOOT),
        "ACTOR_JOB_ZH": sm.actor_text("job:settlement_nightly"),
    }
    for const, zh in words.items():
        assert f"const {const} = '{zh}';" in _SETTLE, (const, zh)
    assert sm.actor_text("user:owner", viewer="owner") == "你"
    assert "viewer() === login ? '你' : login" in _SETTLE


def test_the_diff_vocabularies_name_every_field_and_kind() -> None:
    fields = set(typing.get_args(sm.DiffField))
    kinds = set(typing.get_args(sm.DiffKind))
    assert set(_js_object("DIFF_FIELD_ZH")) == fields
    assert set(_js_object("DIFF_KIND_ZH")) == kinds
    # §9.6: the trend value is 證券市值, never the KPI's 總市值
    assert _js_object("DIFF_FIELD_ZH")["total_value"] == "證券市值"


# ------------------------------------------------------------------ loading, chip, polling


def test_shell_loads_settlement_js_self_stamped_and_no_page_tags_it() -> None:
    """P10: every page with a topbar gets the chip from shell.js — no HTML tag, so the file
    stamps itself with shell.js's own ?v= (the stamper only rewrites HTML)."""
    assert "pdLoadScript('settlement.js' + (v ? '?v=' + v : ''))" in _SHELL
    assert "document.currentScript" in _SHELL
    assert "if (page !== 'login') pdEnsureSettlement();" in _SHELL
    for html in _WEB.glob("*.html"):
        assert not re.search(r"<script[^>]*settlement\.js", html.read_text(encoding="utf-8")), (
            html.name)


def test_the_chip_goes_right_after_the_header_spacer() -> None:
    assert "spacer.insertAdjacentElement('afterend', chip)" in _SETTLE


def test_every_settle_state_has_a_chip_model_and_off_has_none() -> None:
    states = set(typing.get_args(sm.SettleState))
    body = re.search(r"function chipModel\(s\) \{(.*?)\n  \}\n", _SETTLE, re.S)
    assert body
    cases = set(re.findall(r"case '(\w+)':", body.group(1)))
    assert cases == states - {"off"}, cases
    assert "if (!s || s.state === 'off') return null;" in body.group(1)


def test_every_settlement_read_is_background() -> None:
    reads = re.findall(r"api\.get\('/api/settlement/[^;]*?;", _SETTLE, re.S)
    assert len(reads) >= 6, reads
    bad = [r for r in reads if "BG)" not in r]
    assert not bad, f"a settlement read that would move the top progress bar: {bad}"


def test_polling_is_one_second_open_three_seconds_chip_and_none_idle() -> None:
    assert "const POLL_PANEL_MS = 1000;" in _SETTLE
    assert "const POLL_CHIP_MS = 3000;" in _SETTLE
    sched = re.search(r"function schedulePoll\(\) \{(.*?)\n  \}\n", _SETTLE, re.S)
    assert sched and "if (!isActive(status)) return;" in sched.group(1)


def test_the_prompt_is_debounced_asked_once_per_seq_and_never_a_native_dialog() -> None:
    assert "const MUTATED_WAIT_MS = 800;" in _SETTLE
    assert "document.addEventListener('pd-mutated'" in _SETTLE
    assert "sessionStorage" in _SETTLE and "'seq:' + s.seq" in _SETTLE
    assert "confirmLabel: '立即重新結算'" in _SETTLE and "cancelLabel: '稍後'" in _SETTLE
    assert "startRun('dirty')" in _SETTLE


def _missing_rules(classes: set[str]) -> list[str]:
    return sorted(c for c in classes if not re.search(r"\." + re.escape(c) + r"\b", _CSS))


def test_every_chip_banner_and_pill_state_is_a_class_with_a_rule() -> None:
    """DEF-077: the chip / banner / pill colour is a class with a stylesheet rule."""
    # the chip's states: every class chipModel hands out (`cls: '…'`)
    used = set(re.findall(r"st-chip-[a-z]+", " ".join(re.findall(r"cls: '([^']*)'", _SETTLE))))
    assert {"st-chip-clean", "st-chip-pending", "st-chip-run", "st-chip-fail",
            "st-chip-warn"} <= used, used
    used |= {"st-banner", "st-banner-twr", "st-pill-ok", "st-pill-warn", "st-pill-fail",
             "st-pill-run", "net-slow"}
    missing = _missing_rules(used)
    assert not missing, f"state classes with no stylesheet rule: {missing}"
    assert ".trend-panel[data-mode=\"twr\"] .st-banner-twr" in _CSS


def test_the_menu_and_the_panel_never_reload_the_page_for_a_run() -> None:
    start = re.search(r"\n  function startRun\(.*?\n  \}\n", _SETTLE, re.S)
    assert start and "location.reload" not in start.group(0)


def test_every_monthly_mark_is_a_class_with_a_rule() -> None:
    """DEF-077 for 月度成績 (§14.6): 暫定 / 更新前版本 / 已重述 / 上線重述 / ⚠ are classes."""
    app = (_WEB / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"'(snap-mark-[a-z]+)'", app)) | {"snap-row-stale", "snap-note"}
    assert {"snap-mark-stale", "snap-mark-restated", "snap-mark-cutover",
            "snap-mark-provisional", "snap-mark-note"} <= used, used
    missing = _missing_rules(used)
    assert not missing, f"state classes with no stylesheet rule: {missing}"


def test_a_trail_row_reads_the_servers_words() -> None:
    """Review F #7: a record's table, operation, account, actor and attempts are E's
    (`_record_row`); the
    two raw actor fields left (`cancelled_by`) read like E's `who_text`, guest mode included."""
    body = re.search(r"function changeRow\(r, resp\) \{(.*?)\n  \}\n", _SETTLE, re.S)
    assert body
    for field in ("r.table_label", "r.op_text", "r.account_text", "r.actor_text",
                  "r.attempted_text", "r.handed_back", "resp.handed_back_text"):
        assert field in body.group(1), field
    from portfolio_dash.api.settlement_service import who_text

    assert who_text(None, None) == "系統"
    assert who_text(sm.ACTOR_GUEST, sm.ACTOR_GUEST) == "你"
    assert "if (nil(actor)) return '系統';" in _SETTLE
    assert "if (a && a.isGuest && a.isGuest()) return 'user:guest';" in _SETTLE


def test_the_trail_shows_the_counters_and_the_run_shows_the_status_queue() -> None:
    """Review F #2 / #3: price / FX `counters` ride with the records; 「完成後接著處理」 is the
    STATUS's `queued` (the worker writes none into its progress file)."""
    assert "lead: (resp) => (resp && resp.counters) || []" in _SETTLE
    assert "(rep.changes && rep.changes.records_total) || tables.length" in _SETTLE
    assert "s.queued && (s.queued.tables || []).length" in _SETTLE
    assert "run.queued" not in _SETTLE


def test_the_scheduler_page_names_every_settlement_job() -> None:
    """Review F #8: every settlement run's job_runs row is `settlement` — never the raw id."""
    from portfolio_dash.scheduler import jobs

    src = (_WEB / "settings-scheduler.js").read_text(encoding="utf-8")
    m = re.search(r"const JOB_ZH = \{(.*?)\};", src, re.S)
    assert m
    named = set(re.findall(r"^\s*(\w+): '", m.group(1), re.M))
    wanted = {jobs.SETTLEMENT_JOB_ID} | set(jobs.WEB_PROCESS_JOB_IDS) | set(jobs.RETIRED_JOB_IDS)
    assert wanted <= named, sorted(wanted - named)


def test_the_corporate_action_form_reads_option_terms_in_one_request() -> None:
    """Review F #9: the recognition answer carries expiry and strike, so the 選擇權調整 source is
    one /api/input/option-symbol read — the symbol drawer is never asked."""
    src = (_WEB / "corp-action-form.js").read_text(encoding="utf-8")
    body = re.search(r"async function loadAdjSource\(\) \{(.*?)\n    \}\n", src, re.S)
    assert body, "loadAdjSource not found"
    assert "/api/input/option-symbol" in body.group(1)
    assert "/detail" not in body.group(1)
    assert "expiry: r.expiry" in body.group(1)
