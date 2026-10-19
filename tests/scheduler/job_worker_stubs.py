"""Test stubs for the job worker (``api/job_worker.py``), in a child AND in-process.

Every network-bound step of every job answers the way DEF-073's success branch stubs it
(``tests/contract/test_def073_job_details_speak_chinese.py``): the quote / history / dividend
refreshes report every key fetched, the snapshot ingests write two rows, the Alpha Vantage
leg and the news runner return fixed counts, the backup writes nothing. Everything else is
the real job on the real database. Sockets are banned on top (``pytest_socket``), so a step
nobody stubbed fails loudly instead of reaching the network.

:func:`install` applies the stubs through ``patch(owner, name, value)``: the test passes
``monkeypatch.setattr`` for the in-process run; a CHILD imports this module through
``PD_TEST_STUBS`` (``shared/child_runner.apply_test_stubs_from_env``, which needs
``PD_TEST_MODE=1``) and the import installs them with plain ``setattr`` — before the worker
binds its runtime, so the runners the composition root reads off their modules are these.

The model provider is stubbed one level below the accounting: ``shared/llm._call_provider``
answers every call with one valid insight card and a usage block (1,000 prompt and 100
completion tokens), and nothing reports structured-output support, so a run's usage is
metered, priced, booked through ``log_usage`` into ``llm_usage`` and counted by the run's
``usage_tally`` exactly as a real reply would be — without importing LiteLLM.

``PD_STUB_JOB`` picks one job (``PD_STUB_JOB_ID``) and replaces its func:

- ``pid``: returns 「程序 <os.getpid()>」 — proves which process ran it;
- ``slow``: reports progress, then sleeps ``PD_STUB_SLEEP_S`` seconds (a run to kill);
- ``die``: reports progress, then ends the process with code 3 (a worker dying mid-run);
- ``write``: writes one cash movement (a ledger row the settlement's change trail records,
  with the writing connection's actor) and returns 「寫入 1 筆現金異動」.

The worker module itself is never patched here; were it, its functions live in ``__main__``
in a ``python -m`` child (see ``tests/settlement/worker_stubs.py``).
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from portfolio_dash.api import dividend_inbox as inbox
from portfolio_dash.api import fundamentals_service, news_service
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.pricing import ingest
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm
from portfolio_dash.shared.enums import Currency

Patch = Callable[[object, str, Any], None]

#: The progress message the ``slow`` / ``die`` jobs report before they stop.
STUB_PROGRESS = "測試：工作進行中"
#: The day and account of the ``write`` job's cash movement (the golden ledger's account).
WRITE_DAY = date(2026, 1, 2)
WRITE_ACCOUNT = "schwab"
#: The one card every stubbed model call answers with (``tests/scheduler/
#: test_run_records_its_spend.py``'s card).
CARD = ('{"title":"洞察","summary":"量縮","body_md":"**2330** 量縮整理。","tags":["TW"],'
        '"symbol":null,"confidence":70,"prediction":null}')


class _Usage:
    prompt_tokens = 1000
    completion_tokens = 100


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Reply:
    """A provider reply in the shape ``shared/llm`` reads (``choices[0].message.content``,
    ``usage.prompt_tokens`` / ``completion_tokens``)."""

    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


def _call_provider(model: Any, messages: Any, extra: Any) -> _Reply:
    return _Reply(CARD)


def _no_response_format(model: Any) -> bool:
    return False

_SWEEP = ingest.SnapshotSweep(written=2, empty=1)
_NEWS = {"organized": 2, "headline_only": 1, "skipped_existing": 3, "refetched": 1,
         "stopped_budget": False}


def _ok_for(conn: Any, registry: Any, refs: list[Any], *a: Any, **kw: Any) -> RefreshSummary:
    keys = {getattr(r, "symbol", None) or f"{r.base.value}{r.quote.value}" for r in refs}
    return RefreshSummary(ok={k: "yfinance" for k in keys}, failed=[], fetched_at=kw["now"])


def _quotes(conn: Any, registry: Any, refs: list[Any], pairs: list[Any],
            **kw: Any) -> RefreshSummary:
    return _ok_for(conn, registry, [*refs, *pairs], **kw)


def _no_registry(conn: Any = None) -> None:
    return None


def _ingest_count(conn: Any, *, now: Any, **kw: Any) -> int:
    return 2


def _ingest_sweep(conn: Any, *, now: Any, **kw: Any) -> ingest.SnapshotSweep:
    return _SWEEP


def _fundamentals_av(conn: Any, *, now: Any) -> ingest.SnapshotSweep:
    return _SWEEP


def _news_daily(conn: Any, *, now: Any) -> dict[str, Any]:
    return dict(_NEWS)


def _integrity() -> tuple[bool, str]:
    return True, "ok"


def _backup(*, now: Any) -> Path:
    return Path(f"portfolio_{now.date().isoformat()}.db.gz")


def _replaced_func(mode: str, job_id: str) -> jobs.JobFunc:
    sleep_s = float(os.environ.get("PD_STUB_SLEEP_S", "60"))

    def func(conn: Any, *, now: Any) -> str:
        if mode == "pid":
            return f"程序 {os.getpid()}"
        if mode == "write":
            insert_cash_movement(conn, account_id=WRITE_ACCOUNT, move_date=WRITE_DAY,
                                 kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1"))
            return "寫入 1 筆現金異動"
        jobs.set_progress(job_id, STUB_PROGRESS)
        if mode == "die":
            os._exit(3)
        time.sleep(sleep_s)
        return "完成"

    return func


def install(patch: Patch, *, mode: str = "", job_id: str = "") -> None:
    """Apply the stubs through *patch*; with *mode*, also replace *job_id*'s func."""
    patch(jobs, "refresh_quotes", _quotes)
    patch(jobs, "refresh_history", _ok_for)
    patch(jobs, "refresh_dividends", _ok_for)
    patch(jobs, "default_registry", _no_registry)
    patch(inbox, "refresh_dividends", _ok_for)
    patch(inbox, "default_registry", _no_registry)
    for name in ("ingest_chips", "ingest_valuation", "ingest_fundamentals",
                 "ingest_sentiment", "ingest_index"):
        patch(ingest, name, _ingest_count)
    for name in ("ingest_consensus", "ingest_fundamentals_union"):
        patch(ingest, name, _ingest_sweep)
    patch(fundamentals_service, "run_fundamentals_av", _fundamentals_av)
    patch(news_service, "run_news_daily", _news_daily)
    patch(jobs.backup_ops, "check_integrity", _integrity)
    patch(jobs.backup_ops, "backup_database", _backup)
    patch(llm, "_call_provider", _call_provider)
    patch(llm, "_supports_response_format", _no_response_format)
    if mode:
        by_id = {spec.id: spec for spec in jobs.JOBS}
        by_id[job_id] = dataclasses.replace(by_id[job_id], func=_replaced_func(mode, job_id))
        patch(jobs, "_jobs_by_id", lambda: dict(by_id))


def env(*, mode: str = "", job_id: str = "", sleep_s: float = 60.0) -> dict[str, str]:
    """The environment that makes a child import this module (and pick *mode*)."""
    return {"PD_TEST_MODE": "1", "PD_TEST_STUBS": __name__, "PD_STUB_JOB": mode,
            "PD_STUB_JOB_ID": job_id, "PD_STUB_SLEEP_S": str(sleep_s)}


if os.environ.get("PD_TEST_STUBS") == __name__:
    # In the child: no network at all (in-process, pytest's --disable-socket does this).
    from pytest_socket import disable_socket

    disable_socket(allow_unix_socket=True)
    install(setattr, mode=os.environ.get("PD_STUB_JOB", ""),
            job_id=os.environ.get("PD_STUB_JOB_ID", ""))
