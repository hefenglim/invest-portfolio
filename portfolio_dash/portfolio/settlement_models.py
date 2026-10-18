"""Settlement contracts (PERF, spec ``docs/spec/2026-10-18-settlement-store.md``).

The shapes every settlement package builds against, fixed in wave 0 of the plan
(``docs/superpowers/plans/2026-10-18-settlement-store.md`` §4 W0.5): the run request the parent
hands the worker (P12), the progress file (§9.5), the run report (§9.6) and its rows, the status
(§15), and a period close (§11.2 KPI block, §11.3 activity, holdings and cash at D).

Rules:

- Every money / quantity / rate field is :data:`CanonDecimal`: a ``Decimal`` in Python and the
  canonical fixed-point string (``shared/money.to_db``) in JSON — never a float, never ``5E-7``.
- Account ids travel as ids; text meant for the page carries ``{account:<id>}`` tokens, which
  the fetch layer resolves (``web/api.js``). zh text is server-owned and complete.
- A later package may ADD an optional field. Renaming or removing one is a contract change made
  by the main session, with every user updated in the same commit.

Imports nothing above ``shared/`` (the worker imports this module).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import to_db

#: A Decimal that serialises as its canonical string (``money.to_db``) in JSON mode.
CanonDecimal = Annotated[Decimal, PlainSerializer(to_db, return_type=str, when_used="json")]

# --- vocabularies ---------------------------------------------------------------------------

#: What a run settles (P12). ``dirty``: from the dirty day; ``full``: from the first ledger day;
#: ``nightly``: as ``dirty``, then extended through ``final_through``; ``selfcheck``: a full
#: recompute compared with the published generation (§9.1).
RunScope = Literal["dirty", "full", "nightly", "selfcheck"]

#: Why a run started — ``settle_run.reason`` (§6, §9.4). ``job:<id>`` is the one open form.
RUN_REASONS: frozenset[str] = frozenset(
    {"prompt", "button", "boot", "nightly", "chained", "resume", "selfcheck"}
)
JOB_REASON_PREFIX = "job:"


def is_run_reason(reason: str) -> bool:
    """``True`` for a reason in :data:`RUN_REASONS` or of the form ``job:<id>``."""
    if reason in RUN_REASONS:
        return True
    return reason.startswith(JOB_REASON_PREFIX) and len(reason) > len(JOB_REASON_PREFIX)


#: The zh trigger of each reason, for the report's trigger sentence (§9.6 Summary).
RUN_REASON_ZH: dict[str, str] = {
    "prompt": "確認提示後啟動",
    "button": "按下「重新結算」",
    "boot": "系統啟動時自動執行",
    "nightly": "每晚例行結算",
    "chained": "接續結算期間新增的變更",
    "resume": "續跑中斷的結算",
    "selfcheck": "每週自檢",
}
JOB_REASON_ZH = "排程工作後自動執行"

#: ``settle_run.status`` (§6).
RunStatus = Literal[
    "preparing", "running", "published", "partial", "superseded", "cancelled", "failed",
    "interrupted",
]
#: Statuses of a run that has not ended.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"preparing", "running"})

_RUN_STATUS_ZH: dict[str, str] = {
    "preparing": "準備中",
    "running": "進行中",
    "published": "完成",
    "partial": "部分完成，接續處理",
    "superseded": "已併入下一輪",
    "cancelled": "已取消",
    "failed": "失敗",
}


def run_status_text(status: str, *, resumed: bool, next_run_id: int | None = None) -> str:
    """The report summary's status words (§9.6). An interrupted run reads 「中斷，已續跑（#N）」
    only when a resume actually started (``resumed``); otherwise 「結算中斷（未續跑）」."""
    if status == "interrupted":
        if resumed:
            return f"中斷，已續跑（#{next_run_id}）" if next_run_id is not None else "中斷，已續跑"
        return "結算中斷（未續跑）"
    return _RUN_STATUS_ZH[status]


#: ``job_runs.status`` for each run status (§9.4, DEF-067): nothing was lost on a published,
#: partial or superseded run (``ok``); a cancel is ``skipped``; a failure or interruption is
#: ``error``; a self-check that found a difference is ``partial`` (reason ``selfcheck_diff``).
JOB_STATUS_FOR_RUN: dict[str, str] = {
    "published": "ok",
    "partial": "ok",
    "superseded": "ok",
    "cancelled": "skipped",
    "failed": "error",
    "interrupted": "error",
}
SELFCHECK_DIFF_REASON = "selfcheck_diff"


def job_status_for(status: str, *, selfcheck_diff: bool) -> str:
    """``job_runs.status`` of a finished run (raises for a run that has not ended)."""
    if selfcheck_diff and status in ("published", "partial"):
        return "partial"
    return JOB_STATUS_FOR_RUN[status]


#: The phases of a run (§9.3): key → zh label, in order.
PHASES: tuple[tuple[str, str], ...] = (
    ("load", "讀取帳本與行情"),
    ("check", "檢查帳本"),
    ("replay", "重播帳本"),
    ("valuate", "逐日估值"),
    ("close", "月結與年結"),
    ("bench", "基準比較"),
    ("swap", "比對與切換"),
    ("prewarm", "預熱"),
)
PHASE_LABELS: dict[str, str] = dict(PHASES)
PhaseStatus = Literal["pending", "running", "done", "failed", "skipped"]

#: The status chip's states (§15); ``off`` = ``PD_SETTLE=off`` (no chip, no prompt — P10).
SettleState = Literal[
    "clean", "pending", "preparing", "running", "interrupted", "failed", "selfcheck_diff", "off"
]

#: The change trail's operations and states (§6, §9.4).
ChangeOp = Literal["insert", "update", "delete"]
CHANGE_OP_ZH: dict[str, str] = {"insert": "新增", "update": "修改", "delete": "刪除"}
ChangeState = Literal["pending", "claimed", "settled"]

#: The ledger / reference tables the trail names, with their zh labels (the panel's lines).
CHANGE_TABLE_ZH: dict[str, str] = {
    "transactions": "交易",
    "dividends": "股利",
    "opening_inventory": "期初庫存",
    "cash_movements": "現金收支",
    "fx_conversions": "換匯",
    "corporate_actions": "公司行動",
    "prices": "價格",
    "fx_rates": "匯率",
    "instruments": "標的",
    "accounts": "帳戶",
    "account_market_rules": "帳戶市場規則",
}

# Actors (P14): who made a change. NULL is anything the application did not open.
ACTOR_GUEST = "user:guest"
ACTOR_BOOT = "system:boot"
ACTOR_USER_PREFIX = "user:"
ACTOR_JOB_PREFIX = "job:"
ACTOR_EXTERNAL_ZH = "外部程式／腳本"


def actor_text(actor: str | None, *, viewer: str | None = None) -> str:
    """The zh rendering of a change's actor (§9.6 Changes settled): 「你」 for the viewer's own
    login, the login name for another user, 「訪客」 in guest mode, 排程 / 系統（啟動）, and
    「外部程式／腳本」 for a NULL actor."""
    if actor is None:
        return ACTOR_EXTERNAL_ZH
    if actor == ACTOR_GUEST:
        return "訪客"
    if actor == ACTOR_BOOT:
        return "系統（啟動）"
    if actor.startswith(ACTOR_JOB_PREFIX):
        return "排程"
    if actor.startswith(ACTOR_USER_PREFIX):
        login = actor[len(ACTOR_USER_PREFIX):]
        return "你" if viewer is not None and login == viewer else login
    return actor


#: ``settle_run_diff`` fields and kinds (§6).
DiffField = Literal["total_value", "net_invested", "net_worth", "incomplete"]
DiffKind = Literal[
    "changed", "value_to_none", "none_to_value", "flag_flip", "new_day", "dropped_day"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- the run request (P12) ------------------------------------------------------------------


class RunRequest(_Model):
    """What the parent decided before the spawn; written into the ``preparing`` report's
    ``inputs`` and read back by the worker by ``run_id``. The child has no scheduler knowledge:
    every input that depends on the schedule or ``job_runs`` arrives here."""

    scope: RunScope
    reason: str
    started_by: str | None = None
    #: The last day this run may settle (``final_through`` for a nightly run, §9.1).
    through: date
    #: For each day settled by the 4-day rule, the held symbols whose close was never confirmed
    #: by an ``ok`` quote run (``job_runs.results.held_failed``) — ``notes.unconfirmed_closes``.
    unconfirmed_closes: dict[date, list[str]] = Field(default_factory=dict)


# --- the progress file (§9.5) and the report (§9.6) -----------------------------------------


class RunRange(_Model):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    days: int | None = None
    #: Days copied unchanged from the previous generation (before the start day).
    kept_before: int | None = None
    kind: Literal["dirty", "full"] | None = None
    #: A partial publish (§9.4): the published part and the part handed to the chained run.
    published_to: date | None = None
    handed_from: date | None = None


class RunVersions(_Model):
    generation_before: int | None = None
    generation_after: int | None = None
    settled_through_before: date | None = None
    settled_through_after: date | None = None
    calc_version_before: str | None = None
    #: The calc version the child computed when it STARTED (stamped at the swap, §9.4).
    calc_version: str | None = None
    reporting_ccy_before: Currency | None = None
    reporting_ccy: Currency | None = None


class ChangeTableCount(_Model):
    """One table's line in a change summary; ``keys`` for prices / FX (symbols or pairs)."""

    table: str
    label: str
    changes: int
    keys: int | None = None
    earliest: date | None = None
    latest: date | None = None


class RunChanges(_Model):
    tables: list[ChangeTableCount] = Field(default_factory=list)
    records_total: int = 0
    #: The paged records (``GET /api/settlement/changes?state=run:<id>``).
    records_url: str | None = None
    #: On a run that did not publish: 「本次未生效，已退回待結算」.
    handed_back: bool = False


class Finding(_Model):
    """One 待釐清 record of the tolerant replay (§9.3 phase 2), non-blocking wording."""

    kind: Literal[
        "oversold", "orphan_dividend", "unbookable_dividend", "unapplied_action", "refused_close",
        "unregistered_symbol",
    ]
    day: date | None = None
    account_id: str | None = None
    symbol: str | None = None
    text: str


class RunValidation(_Model):
    status: PhaseStatus = "pending"
    findings: list[Finding] = Field(default_factory=list)


class RunPhase(_Model):
    key: str
    label: str
    status: PhaseStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_s: float | None = None
    done: int | None = None
    total: int | None = None
    unit: str | None = None
    rate: str | None = None
    detail: str | None = None


class RunCurrent(_Model):
    """Live only: the day being valued and how many positions it holds."""

    day: date
    positions: int


class QueuedChange(_Model):
    """A change committed during the run: the chained follow-up (「完成後接著處理」)."""

    from_: date = Field(alias="from")
    changes: int
    reason: str


class RunProgress(_Model):
    """The progress file ``<data>/settlement-progress.json`` (§9.5), replaced atomically.

    The same structure, without :data:`LIVE_ONLY_FIELDS`, is frozen into the run's report.
    """

    run_id: int
    pid: int | None = None
    heartbeat: datetime | None = None
    status: RunStatus
    reason: str
    started_by: str | None = None
    trigger_text: str
    range: RunRange = Field(default_factory=RunRange)
    versions: RunVersions = Field(default_factory=RunVersions)
    started_at: datetime
    elapsed_s: float = 0.0
    percent: int = 0
    eta_s: float | None = None
    eta_initial_s: float | None = None
    eta_basis: str | None = None
    priority: str | None = None
    changes: RunChanges = Field(default_factory=RunChanges)
    validation: RunValidation = Field(default_factory=RunValidation)
    phases: list[RunPhase] = Field(default_factory=list)
    current: RunCurrent | None = None
    queued: list[QueuedChange] = Field(default_factory=list)
    on_cancel: str | None = None

    def frozen(self) -> dict[str, Any]:
        """The JSON dict for the stored report: the live-only fields dropped."""
        return self.model_dump(mode="json", by_alias=True, exclude=set(LIVE_ONLY_FIELDS))


#: Fields that describe a live child and are never frozen into a report (§9.5).
LIVE_ONLY_FIELDS: tuple[str, ...] = ("pid", "heartbeat", "eta_s", "current")


class RunSummary(_Model):
    """A report's summary — the ``/runs`` list row and the status's ``last_run`` (§9.6)."""

    run_id: int
    status: RunStatus
    status_text: str
    resumed: bool = False
    reason: str
    started_by: str | None = None
    cancelled_by: str | None = None
    trigger_text: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float | None = None
    eta_initial_s: float | None = None
    range_from: date | None = None
    range_to: date | None = None
    days: int | None = None
    generation_before: int | None = None
    generation_after: int | None = None
    settled_through_before: date | None = None
    settled_through_after: date | None = None
    previous_run_id: int | None = None
    next_run_id: int | None = None
    #: The run settled at least one change (``last_change_run``, ``only_changes``).
    had_changes: bool = False
    #: A self-check that found a difference (chip 自檢發現差異).
    selfcheck_diff: bool = False


class DayValue(_Model):
    """A day's figure before → after, in the reporting currency."""

    day: date
    before: CanonDecimal | None = None
    after: CanonDecimal | None = None


class HistoryResult(_Model):
    """Result — history (§9.6): only days both generations hold, same reporting currency,
    inside the published range. Per-day rows live in ``settle_run_diff``."""

    counts: dict[str, int] = Field(default_factory=dict)
    value_to_none: int = 0
    none_to_value: int = 0
    flag_flips: int = 0
    first_changed_day: date | None = None
    last_changed_day: date | None = None
    largest_net_worth_change: DayValue | None = None
    last_common_day: DayValue | None = None
    new_days: int = 0
    dropped_days: int = 0
    #: A reporting-currency change suppresses the value comparison.
    currency_changed: bool = False
    note: str | None = None


class StateResult(_Model):
    """Result — state (§9.6)."""

    stale_cleared: bool | None = None
    stale_reason: str | None = None
    pending_positions: list[Finding] = Field(default_factory=list)
    chained_from: date | None = None


class RunFailure(_Model):
    """Failure / cancel / interruption (§9.6): where it stopped, why, and the consequence
    derived from the actual state (``on_cancel``)."""

    phase: str | None = None
    reason_text: str
    cancelled_by: str | None = None
    consequence: str | None = None


class RunReport(_Model):
    """The full report of one run (§9.6), stored as ``settle_run.report``."""

    summary: RunSummary
    inputs: RunRequest
    range: RunRange = Field(default_factory=RunRange)
    versions: RunVersions = Field(default_factory=RunVersions)
    changes: RunChanges = Field(default_factory=RunChanges)
    validation: RunValidation = Field(default_factory=RunValidation)
    steps: list[RunPhase] = Field(default_factory=list)
    history: HistoryResult | None = None
    #: The restatements this run wrote (``settle_restatement``); empty: 「月結與年結無變動」.
    restatement_ids: list[int] = Field(default_factory=list)
    state: StateResult | None = None
    failure: RunFailure | None = None
    #: Q-P1 (a): a failed automatic run inside quiet hours keeps its push until they end.
    notify_pending: bool = False


class ChangeRecord(_Model):
    """One ``settle_change_log`` row, as the panel lists it (§9.6 Changes settled)."""

    id: int
    seq: int
    table: str
    op: ChangeOp
    row_key: str
    old_account_id: str | None = None
    new_account_id: str | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    old_day: date | None = None
    new_day: date | None = None
    import_batch_id: str | None = None
    actor: str | None = None
    written_at: datetime
    state: ChangeState
    claimed_run_id: int | None = None
    settled_run_id: int | None = None
    #: Runs that claimed it and did not publish it (「曾於 #411 嘗試」).
    attempted_run_ids: list[int] = Field(default_factory=list)


class ChangeCounter(_Model):
    """One ``settle_change_agg`` counter: prices per symbol, FX per pair."""

    id: int
    table: Literal["prices", "fx_rates"]
    key: str
    changes: int
    earliest: date
    latest: date
    state: ChangeState
    claimed_run_id: int | None = None
    settled_run_id: int | None = None
    attempted_run_ids: list[int] = Field(default_factory=list)


class DiffRow(_Model):
    """One ``settle_run_diff`` row: a changed day and field."""

    run_id: int
    day: date
    field: DiffField
    kind: DiffKind
    before: CanonDecimal | None = None
    after: CanonDecimal | None = None
    flags_before: dict[str, bool] = Field(default_factory=dict)
    flags_after: dict[str, bool] = Field(default_factory=dict)


class Restatement(_Model):
    """A closed period whose figure moved, and why (§14.6) — never pruned."""

    id: int
    kind: Literal["M", "Y"]
    period: str
    field: str
    label: str
    unit: str
    old_value: CanonDecimal | None = None
    new_value: CanonDecimal | None = None
    notes_before: dict[str, Any] | None = None
    notes_after: dict[str, Any] | None = None
    run_id: int
    reason: str
    source: str | None = None
    at: datetime
    cutover: bool = False


# --- the status (§15) -------------------------------------------------------------------------


class SettlePrompt(_Model):
    from_: date = Field(alias="from")
    days: int
    est_s: float | None = None


class PendingChanges(_Model):
    tables: list[ChangeTableCount] = Field(default_factory=list)
    records_total: int = 0


class SettlementStatus(_Model):
    """``GET /api/settlement/status`` — reads only, never starts anything."""

    state: SettleState
    generation: int = 0
    settled_through: date | None = None
    calc_ok: bool = False
    dirty_from: date | None = None
    live_from: date | None = None
    stale_from: date | None = None
    stale_to: date | None = None
    needs_prompt: bool = False
    prompt: SettlePrompt | None = None
    pending: PendingChanges = Field(default_factory=PendingChanges)
    #: Changes committed while a run is active (「完成後接著處理」).
    queued: PendingChanges | None = None
    final_through: date | None = None
    run: RunProgress | None = None
    #: 「進行緩慢」 after 60 s without progress (liveness is the OS lock, never the heartbeat).
    slow: bool = False
    last_run: RunSummary | None = None
    last_change_run: RunSummary | None = None


# --- a period close (§11.2, §11.3) ------------------------------------------------------------


class FxUsed(_Model):
    """A rate a close used, and its date (direct, else inverse — ``_fx_at``)."""

    pair: str
    rate: CanonDecimal
    as_of: date


class PriceDate(_Model):
    symbol: str
    price_date: date | None = None


class PairDate(_Model):
    pair: str
    rate_date: date | None = None


class PendingPosition(_Model):
    account_id: str
    symbol: str
    kind: Literal["oversold", "unapplied_action", "refused_dividend", "refused_close"]


class ReceivableDividend(_Model):
    """A non-STOCK dividend with ``ex_date ≤ D < date`` — shown, never counted."""

    account_id: str
    symbol: str
    ex_date: date
    pay_date: date
    ccy: Currency
    net: CanonDecimal


class PeriodNotes(_Model):
    """``notes`` of a close (§11.2): every reason a figure is absent or unconfirmed."""

    incomplete: bool = False
    missing_prices: list[str] = Field(default_factory=list)
    stale_prices: list[PriceDate] = Field(default_factory=list)
    unconfirmed_closes: list[PriceDate] = Field(default_factory=list)
    missing_fx: list[str] = Field(default_factory=list)
    stale_fx: list[PairDate] = Field(default_factory=list)
    pending_positions: list[PendingPosition] = Field(default_factory=list)
    options_value_excluded: bool = False
    options_past_expiry: list[str] = Field(default_factory=list)
    #: Computed when the close is READ, never stored; ``None`` on a stale row (§11.2).
    dividends_receivable: list[ReceivableDividend] | None = None


class PeriodKpi(_Model):
    """The KPI block of a period close (§11.2) — since inception, as at the period's end D."""

    reporting_ccy: Currency
    total_market_value: CanonDecimal | None = None
    total_return: CanonDecimal | None = None
    total_return_rate: CanonDecimal | None = None
    xirr: CanonDecimal | None = None
    xirr_window_days: int | None = None
    xirr_reason: str | None = None
    by_currency_value: dict[Currency, CanonDecimal] = Field(default_factory=dict)
    period_twr: CanonDecimal | None = None
    period_twr_reason: str | None = None
    #: The settled day D's trend figures (stocks only; a partial sum on an incomplete day).
    trend_total_value: CanonDecimal | None = None
    net_invested: CanonDecimal | None = None
    net_worth: CanonDecimal | None = None
    fx_used: list[FxUsed] = Field(default_factory=list)
    notes: PeriodNotes = Field(default_factory=PeriodNotes)


class Money2(_Model):
    """An amount in its native currency and in the reporting currency at its own date's FX."""

    ccy: Currency
    native: CanonDecimal
    reporting: CanonDecimal | None = None


class RealizedLine(_Model):
    """Realized in the period (§11.3): sales and short covers (``trade``), and dividends after
    a full close (``dividend_after_close``, also on the dividends line — not additive)."""

    kind: Literal["trade", "dividend_after_close"]
    asset_type: Literal["stock", "option"]
    rows: int
    #: vs adjusted cost (ties to 總報酬 and ``realized_gains_<year>.csv``'s adjusted column).
    realized: Money2
    #: vs original cost: ``proceeds_net − original_cost_removed`` (the capital gain).
    capital_gain: Money2 | None = None


class DividendLine(_Model):
    """Booked dividends by type and account (§11.3)."""

    type: Literal["CASH", "NET", "DRIP", "STOCK"]
    account_id: str
    rows: int
    gross: Money2 | None = None
    withholding: Money2 | None = None
    net: Money2 | None = None
    reinvested_shares: CanonDecimal | None = None
    stock_shares: CanonDecimal | None = None


class NotBookedLine(_Model):
    """Refused payouts in the period, labelled 未入帳（待釐清）."""

    rows: int
    net: Money2


class FeeLine(_Model):
    account_id: str
    fees: Money2
    tax: Money2


class CashMovementLine(_Model):
    account_id: str
    kind: str
    rows: int
    amount: Money2


class FxConversionLine(_Model):
    account_id: str
    rows: int
    from_amount: Money2
    to_amount: Money2


class FxRealizedLine(_Model):
    account_id: str
    realized: Money2


class OpeningLine(_Model):
    account_id: str
    symbol: str
    shares: CanonDecimal
    original_cost_total: Money2


class PeriodActivity(_Model):
    """Period activity (§11.3). The lines are NOT additive — no total of them is shown."""

    realized: list[RealizedLine] = Field(default_factory=list)
    dividends: list[DividendLine] = Field(default_factory=list)
    dividends_not_booked: list[NotBookedLine] = Field(default_factory=list)
    fees: list[FeeLine] = Field(default_factory=list)
    cash_movements: list[CashMovementLine] = Field(default_factory=list)
    fx_conversions: list[FxConversionLine] = Field(default_factory=list)
    fx_realized: list[FxRealizedLine] = Field(default_factory=list)
    openings: list[OpeningLine] = Field(default_factory=list)


class PeriodHolding(_Model):
    """A ``settled_period_holding`` row: one position at D, native currency."""

    account_id: str
    symbol: str
    asset_type: Literal["stock", "option"]
    quote_ccy: Currency
    shares: CanonDecimal
    original_cost_total: CanonDecimal
    adjusted_cost_total: CanonDecimal
    price: CanonDecimal | None = None
    price_as_of: date | None = None
    market_value: CanonDecimal | None = None
    unrealized: CanonDecimal | None = None
    flags: dict[str, bool] = Field(default_factory=dict)


class PeriodCash(_Model):
    """A ``settled_period_cash`` row: an account's end-of-day balance at D."""

    account_id: str
    ccy: Currency
    balance: CanonDecimal
    reporting_value: CanonDecimal | None = None
    fx_rate: CanonDecimal | None = None
    fx_as_of: date | None = None


class PeriodClose(_Model):
    """One close as ``/api/settlement/periods`` serves it."""

    kind: Literal["M", "Y"]
    period: str
    end_day: date
    kpi: PeriodKpi
    activity: PeriodActivity
    #: ``store`` (settled) or ``live`` (end day on or after ``live_from``).
    source: Literal["store", "live"]
    #: Served from the previous generation inside the stale range (更新前版本).
    stale: bool = False
    #: Restatement rows for this period exist (已重述 / 上線重述).
    restated: bool = False


class PeriodDetail(PeriodClose):
    """One close with its positions and cash (``/api/settlement/periods/{kind}/{period}``)."""

    holdings: list[PeriodHolding] = Field(default_factory=list)
    cash: list[PeriodCash] = Field(default_factory=list)


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ACTOR_BOOT",
    "ACTOR_EXTERNAL_ZH",
    "ACTOR_GUEST",
    "ACTOR_JOB_PREFIX",
    "ACTOR_USER_PREFIX",
    "CHANGE_OP_ZH",
    "CHANGE_TABLE_ZH",
    "JOB_REASON_PREFIX",
    "JOB_REASON_ZH",
    "JOB_STATUS_FOR_RUN",
    "LIVE_ONLY_FIELDS",
    "PHASES",
    "PHASE_LABELS",
    "RUN_REASONS",
    "RUN_REASON_ZH",
    "SELFCHECK_DIFF_REASON",
    "CanonDecimal",
    "ChangeCounter",
    "ChangeRecord",
    "DiffRow",
    "Finding",
    "PeriodActivity",
    "PeriodCash",
    "PeriodClose",
    "PeriodDetail",
    "PeriodHolding",
    "PeriodKpi",
    "PeriodNotes",
    "Restatement",
    "RunPhase",
    "RunProgress",
    "RunReport",
    "RunRequest",
    "RunSummary",
    "SettlementStatus",
    "actor_text",
    "is_run_reason",
    "job_status_for",
    "run_status_text",
]
