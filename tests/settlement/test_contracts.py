"""The settlement contracts (plan W0.5): serialisation, vocabularies, the worker's entry."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import portfolio_dash
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio.settlement_models import (
    LIVE_ONLY_FIELDS,
    PHASES,
    DiffRow,
    PeriodKpi,
    RunCurrent,
    RunProgress,
    RunRange,
    RunReport,
    RunRequest,
    RunSummary,
    SettlementStatus,
    actor_text,
    is_run_reason,
    job_status_for,
    run_status_text,
)
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 10, 18, 2, 32, tzinfo=UTC)


def test_decimals_serialise_as_canonical_strings() -> None:
    kpi = PeriodKpi(reporting_ccy=Currency.TWD, total_market_value=Decimal("1239600.00"),
                    xirr=Decimal("0.0000005"), by_currency_value={Currency.USD: Decimal("1E+3")})
    data = kpi.model_dump(mode="json")
    assert data["total_market_value"] == "1239600.00"
    assert data["xirr"] == "0.0000005"  # never 5E-7
    assert data["by_currency_value"] == {"USD": "1000"}
    assert PeriodKpi.model_validate(data).xirr == Decimal("0.0000005")


def test_a_diff_row_keeps_none_apart_from_zero() -> None:
    row = DiffRow(run_id=1, day=date(2026, 3, 2), field="total_value", kind="value_to_none",
                  before=Decimal("0"), after=None)
    assert row.model_dump(mode="json")["before"] == "0"
    assert row.model_dump(mode="json")["after"] is None


def test_the_progress_file_freezes_without_its_live_fields() -> None:
    progress = RunProgress(
        run_id=412, pid=2211, heartbeat=_NOW, status="running", reason="prompt",
        started_by="user:owner", trigger_text="{started_by} 於 10:32 啟動", started_at=_NOW,
        eta_s=8.0, current=RunCurrent(day=date(2026, 3, 24), positions=41),
        range=RunRange.model_validate(
            {"from": "2025-06-16", "to": "2026-10-17", "days": 489, "kind": "dirty"}
        ),
    )
    frozen = progress.frozen()
    assert not set(LIVE_ONLY_FIELDS) & set(frozen)
    assert frozen["range"]["from"] == "2025-06-16"  # the wire name, not from_
    assert RunProgress.model_validate(json.loads(json.dumps(frozen))).run_id == 412


def test_a_report_carries_its_run_request() -> None:
    request = RunRequest(scope="nightly", reason="nightly", through=date(2026, 10, 17),
                         unconfirmed_closes={date(2026, 10, 13): ["AAPL"]})
    summary = RunSummary(run_id=7, status="preparing", status_text=run_status_text(
        "preparing", resumed=False), reason="nightly", trigger_text="每晚例行結算",
        started_at=_NOW)
    report = RunReport(summary=summary, inputs=request)
    data = report.model_dump(mode="json", by_alias=True)
    assert data["inputs"]["unconfirmed_closes"] == {"2026-10-13": ["AAPL"]}
    assert RunReport.model_validate(data).inputs == request


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError):
        RunRequest.model_validate({"scope": "full", "reason": "button",
                                   "through": "2026-10-17", "extra": 1})


def test_the_status_off_state_exists() -> None:
    assert SettlementStatus(state="off").model_dump(mode="json")["state"] == "off"


def test_status_words() -> None:
    assert run_status_text("published", resumed=False) == "完成"
    assert run_status_text("interrupted", resumed=False) == "結算中斷（未續跑）"
    assert run_status_text("interrupted", resumed=True, next_run_id=413) == "中斷，已續跑（#413）"


def test_job_status_follows_def067() -> None:
    assert job_status_for("partial", selfcheck_diff=False) == "ok"
    assert job_status_for("superseded", selfcheck_diff=False) == "ok"
    assert job_status_for("cancelled", selfcheck_diff=False) == "skipped"
    assert job_status_for("interrupted", selfcheck_diff=False) == "error"
    assert job_status_for("published", selfcheck_diff=True) == "partial"
    with pytest.raises(KeyError):
        job_status_for("running", selfcheck_diff=False)


def test_actor_words() -> None:
    assert actor_text(None) == "外部程式／腳本"
    assert actor_text("user:guest") == "訪客"
    assert actor_text("system:boot") == "系統（啟動）"
    assert actor_text("job:quotes_tw") == "排程"
    assert actor_text("user:alice", viewer="alice") == "你"
    assert actor_text("user:alice", viewer="bob") == "alice"


def test_run_reasons() -> None:
    assert is_run_reason("prompt") and is_run_reason("job:quotes_us")
    assert not is_run_reason("job:") and not is_run_reason("manual")


def test_phases_are_the_eight_of_the_spec() -> None:
    assert [k for k, _ in PHASES] == [
        "load", "check", "replay", "valuate", "close", "bench", "swap", "prewarm"
    ]


def test_the_worker_entry_is_a_stub_until_wave_two(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        settle_worker.run(1, db_path=tmp_path / "x.db")


def test_the_worker_imports_no_web_llm_or_provider_module() -> None:
    """Spec §9.2: the child imports only portfolio / pricing / forex / data_ingestion / shared."""
    root = Path(portfolio_dash.__file__).resolve().parents[1]
    code = (
        "import sys, json; import portfolio_dash.portfolio.settle_worker; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                         text=True, check=True, timeout=120)
    modules = json.loads(out.stdout)
    banned = ("fastapi", "litellm", "yfinance", "pandas", "starlette")
    banned_pkgs = ("api", "scheduler", "export", "strategy", "news", "ops", "llm_insight")
    found = [m for m in modules if m.split(".")[0] in banned]
    found += [m for m in modules
              if m.startswith("portfolio_dash.") and m.split(".")[1] in banned_pkgs]
    assert not found, found
