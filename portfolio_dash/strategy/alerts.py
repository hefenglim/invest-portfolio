"""Risk-alert rule engine (spec 03 §3.1). Pure over DashboardData — the SINGLE source
for both GET /api/alerts and the dashboard payload's embedded alerts. Seven market/quota
rules plus ``calib_gap`` (spec 03/04 I1, the AI calibration-error rule). Degrades
silently when an input is absent (never raises on missing market data).

``calib_gap`` is the one rule whose signal lives outside DashboardData: the portfolio-
wide AI calibration error in PERCENTAGE POINTS is FED IN as ``calib_gap: Decimal | None``
(the gate-and-compute lives in ``api.insight_service.calibration_gap`` — strategy/ stays
pure and never imports llm_insight). The threshold (``rules.calib_gap.value``) is also in
pp, so the comparison is pp-vs-pp. None means "below the global min_samples gate" → the
rule degrades silently. ``calibration_regression`` (spec 04c) is a separate EVENT in
``alert_events``; it is intentionally NOT a rule here."""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import ai_active, budget_remaining, get_alert_threshold
from portfolio_dash.strategy.rules_config import AlertRules, get_alert_rules

Severity = Literal["risk", "warn", "info"]
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HALF = Decimal("0.5")

# Swedroe 5/25 relative leg: the drift band is min(absolute_band, 0.25 × target) — the
# TIGHTER band governs (whichever leg is crossed first); the relative leg's BASE is the
# TARGET weight (a named constant, not editable — the "25" in 5/25).
_REBALANCE_REL = Decimal("0.25")
# consensus_change price leg: a mean-target-price CUT of ≥ 10% (base = the older mean) — the
# fixed second leg alongside the editable rating-score worsening threshold.
_CONSENSUS_PRICE_CUT = Decimal("0.10")
# drawdown_from_peak minimum honest window: below ~30 sessions a "52-week high" is just the
# highest of a handful of points — too thin to call a peak (deep review 2026-07-13).
_DRAWDOWN_MIN_WINDOW = 30


class Alert(BaseModel):
    id: str
    sev: Severity
    rule: str
    title: str
    detail: str
    href: str | None = None


# --- fed market-risk inputs (P3 batch 2) --------------------------------------
# strategy/ is PURE: it never reads pricing/consensus. The api/scheduler seam
# (api.alert_inputs) computes these per-symbol metrics from stored prices + consensus
# snapshots and FEEDS them in, exactly as ``calib_gap`` is fed from llm_insight. Every
# value is already display-clean Decimal / None (None = insufficient data → the rule stays
# honestly silent for that symbol; it never fabricates).


class SymbolMetric(BaseModel):
    """Per-symbol price-derived inputs for drawdown ① + vol_spike ② (fed, not computed here).

    ``pct_from_52w_high`` is a ratio ``<= 0`` (``technicals.week52_position``); ``window_days``
    is the ACTUAL trailing window used (honest for an under-a-year listing). ``vol_30d`` /
    ``vol_90d`` are annualized volatilities (fed only for HELD symbols — vol_spike is held-only).
    """

    held: bool
    pct_from_52w_high: Decimal | None = None
    window_days: int = 0
    vol_30d: Decimal | None = None
    vol_90d: Decimal | None = None


class ConsensusDelta(BaseModel):
    """Per-symbol analyst-consensus change for rule ④ (latest snapshot vs ≥7-day-older one).

    Rating score is on a 1=best..5=worst scale, so "worse" = an INCREASE. Any missing leg
    (``None``) makes that leg non-firing; both legs missing → the rule is silent for the symbol.
    """

    score_now: Decimal | None = None
    score_then: Decimal | None = None
    target_mean_now: Decimal | None = None
    target_mean_then: Decimal | None = None
    days_apart: int | None = None


# --- display formatting (FH2 fix, 2026-07-07) ----------------------------------
# Alert title/detail are USER-FACING strings rendered verbatim by the topbar bell —
# the app's first attention surface. They must read as product copy (中文, display-
# quantized percentages, account display names), never as a debug log
# ("weight 0.7528455359… > 0.30" / "moomoo_my_us" / "ex-date in 3d").
# Quantization here is DISPLAY-ONLY: every comparison above it stays full precision.


def _pct(x: Decimal) -> str:
    """A ratio as a display percentage: 0.7528455… → ``75.3%`` (display only)."""
    return f"{(x * Decimal('100')).quantize(Decimal('0.1'))}%"


def _usd(x: Decimal) -> str:
    """A USD amount for display: ``$1.50`` (2 dp, display only)."""
    return f"${x.quantize(Decimal('0.01'))}"


def _pp(x: Decimal) -> str:
    """Percentage points for display: 20 → ``20pp``, 12.34 → ``12.3pp`` (display only)."""
    q = x.quantize(Decimal("0.1"))
    if q == q.to_integral_value():
        return f"{q.quantize(Decimal('1'))}pp"  # never exponent notation (2E+1)
    return f"{q}pp"


def _mult(x: Decimal) -> str:
    """A multiple for display: 1.8 → ``1.80x`` (2 dp, display only)."""
    return f"{x.quantize(Decimal('0.01'))}x"


def _score(x: Decimal) -> str:
    """A rating score for display: 3.2 → ``3.20`` (2 dp, display only)."""
    return f"{x.quantize(Decimal('0.01'))}"


def _exdiv_phrase(delta: int) -> str:
    return "今日除息" if delta == 0 else f"{delta} 天後除息"


def compute_alerts_from(
    data: DashboardData, rules: AlertRules, *,
    quota_remaining: Decimal, quota_threshold: Decimal,
    ai_active: bool = True,
    calib_gap: Decimal | None = None,
    account_names: dict[str, str] | None = None,
    symbol_metrics: dict[str, SymbolMetric] | None = None,
    target_weights: dict[str, Decimal] | None = None,
    consensus_deltas: dict[str, ConsensusDelta] | None = None,
) -> list[Alert]:
    """Run the rule engine over the fed inputs; returns display-ready alerts.

    ``ai_active`` (P3 batch 3 · 3B) gates ``quota_low``: a low LLM budget is only worth
    alerting on when AI is actually usable (≥1 role → an enabled model). It is FED IN as a
    plain bool by the conn-bearing seams (``shared.llm_config.ai_active``) — strategy/ stays
    pure and never imports the model registry itself. Defaults to ``True`` so callers that
    predate the gate keep firing quota_low.

    ``account_names`` maps account_id → the accounts table's display name (fed by the
    conn-bearing wrapper); an unknown/absent id falls back to the raw id.

    The P3-batch-2 market-risk rules read three FED maps (assembled at the api/scheduler
    seam, never here — strategy/ cannot import pricing): ``symbol_metrics`` (per-symbol
    52-week drawdown + 30d/90d annualized vol), ``target_weights`` (symbol → ratio), and
    ``consensus_deltas`` (latest-vs-7-day-older analyst consensus). An absent map / None
    field means "insufficient data" → the rule is honestly silent for that symbol.
    """
    alerts: list[Alert] = []
    as_of = data.as_of.date()
    names = account_names or {}
    metrics = symbol_metrics or {}
    targets = target_weights or {}
    consensus = consensus_deltas or {}

    if rules.single_weight.enabled and rules.single_weight.value is not None:
        thr = rules.single_weight.value
        for h in data.holdings:
            if h.weight is not None and h.weight > thr:
                alerts.append(Alert(
                    id=f"single_weight:{h.symbol}", sev="risk", rule="single_weight",
                    title=f"{h.symbol} 單一持股權重偏高",
                    detail=f"單一持股權重 {_pct(h.weight)}＞門檻 {_pct(thr)}",
                    href=f"/symbol/{h.symbol}"))

    if rules.sector_weight.enabled and rules.sector_weight.value is not None and data.allocation:
        thr = rules.sector_weight.value
        for sector, w in data.allocation.weights.items():
            if w > thr:
                alerts.append(Alert(
                    id=f"sector_weight:{sector}", sev="risk", rule="sector_weight",
                    title=f"{sector} 產業權重偏高",
                    detail=f"產業權重 {_pct(w)}＞門檻 {_pct(thr)}"))

    if rules.stale_price.enabled:
        for p in data.freshness.prices:
            if p.stale:
                alerts.append(Alert(
                    id=f"stale_price:{p.symbol}", sev="warn", rule="stale_price",
                    title=f"{p.symbol} 報價過期", detail="庫存報價已過期，尚未更新",
                    href=f"/symbol/{p.symbol}"))

    if rules.missing_price.enabled:
        for sym in data.freshness.missing_prices:
            alerts.append(Alert(
                id=f"missing_price:{sym}", sev="warn", rule="missing_price",
                title=f"{sym} 無報價", detail="無庫存報價，無法評價",
                href=f"/symbol/{sym}"))

    if rules.fx_drift.enabled and rules.fx_drift.value is not None and data.fx:
        thr = rules.fx_drift.value
        for acct_id, res in data.fx.by_account.items():
            if res.avg_rate is not None and res.avg_rate != _ZERO and res.current_spot is not None:
                drift = abs(res.current_spot / res.avg_rate - _ONE)
                if drift > thr:
                    alerts.append(Alert(
                        id=f"fx_drift:{acct_id}", sev="info", rule="fx_drift",
                        title=f"{names.get(acct_id, acct_id)} 匯率偏離成本",
                        detail=f"即期匯率偏離成本匯率 {_pct(drift)}＞門檻 {_pct(thr)}"))

    if rules.exdiv_upcoming.enabled and rules.exdiv_upcoming.value is not None:
        days = int(rules.exdiv_upcoming.value)
        for item in data.ex_dividend_calendar:
            delta = (item.ex_date - as_of).days
            if 0 <= delta <= days:
                alerts.append(Alert(
                    id=f"exdiv_upcoming:{item.symbol}", sev="info", rule="exdiv_upcoming",
                    title=f"{item.symbol} 即將除息", detail=_exdiv_phrase(delta),
                    href=f"/symbol/{item.symbol}"))

    if rules.quota_low.enabled and ai_active and quota_remaining < quota_threshold:
        sev: Severity = "risk" if quota_remaining == _ZERO else "warn"
        alerts.append(Alert(
            id="quota_low", sev=sev, rule="quota_low", title="LLM 額度偏低",
            detail=f"剩餘額度 {_usd(quota_remaining)}＜警戒值 {_usd(quota_threshold)}",
            href="/settings"))

    # calib_gap: pp-vs-pp comparison (both `calib_gap` and the threshold are percentage
    # points). None → below the global min_samples gate → silent (no alert). Single global
    # alert (no symbol).
    if (rules.calib_gap.enabled and rules.calib_gap.value is not None
            and calib_gap is not None and calib_gap > rules.calib_gap.value):
        alerts.append(Alert(
            id="calib_gap", sev="warn", rule="calib_gap", title="AI 校準誤差偏高",
            detail=f"校準誤差 {_pp(calib_gap)}＞門檻 {_pp(rules.calib_gap.value)}",
            href="/settings"))

    # --- P3 batch 2: market-risk rules (held + watch universe; fed inputs) -----------------

    # ① drawdown_from_peak (held AND watch): current price vs the trailing 52-week high.
    # value = the RISK drawdown magnitude (0.20); warn fires at HALF that (−10% at default) —
    # one editable knob, documented two-level severity. pct_from_52w_high is a ratio <= 0.
    # A window below _DRAWDOWN_MIN_WINDOW sessions is too thin to call a "peak" (deep review
    # 2026-07-13: a 2-point declining series fired a RISK alert) → silent, never fabricated.
    if (rules.drawdown_from_peak.enabled
            and rules.drawdown_from_peak.value is not None and metrics):
        risk_thr = rules.drawdown_from_peak.value
        warn_thr = risk_thr * _HALF
        for sym, m in metrics.items():
            if m.pct_from_52w_high is None or m.window_days < _DRAWDOWN_MIN_WINDOW:
                continue  # no/too-thin price history → silent (never fabricated)
            dd = -m.pct_from_52w_high if m.pct_from_52w_high < _ZERO else _ZERO
            if dd >= risk_thr:
                sev_dd: Severity = "risk"
                thr_dd = risk_thr
            elif dd >= warn_thr:
                sev_dd = "warn"
                thr_dd = warn_thr
            else:
                continue
            alerts.append(Alert(
                id=f"drawdown_from_peak:{sym}", sev=sev_dd, rule="drawdown_from_peak",
                title=f"{sym} 自高點回撤",
                detail=(f"自 52 週高點回撤 {_pct(dd)}（{m.window_days} 日視窗）"
                        f"＞門檻 {_pct(thr_dd)}"),
                href=f"/symbol/{sym}"))

    # ② vol_spike (HELD only): 30d annualized vol >= multiple × the 90d baseline.
    if rules.vol_spike.enabled and rules.vol_spike.value is not None and metrics:
        mult = rules.vol_spike.value
        for sym, m in metrics.items():
            if not m.held or m.vol_30d is None or m.vol_90d is None or m.vol_90d <= _ZERO:
                continue  # not held or a window too short to judge → silent
            ratio = m.vol_30d / m.vol_90d
            if ratio >= mult:
                alerts.append(Alert(
                    id=f"vol_spike:{sym}", sev="warn", rule="vol_spike",
                    title=f"{sym} 波動突升",
                    detail=(f"30 日年化波動 {_pct(m.vol_30d)}，達 90 日基準 "
                            f"{_pct(m.vol_90d)} 的 {_mult(ratio)}＞門檻 {_mult(mult)}"),
                    href=f"/symbol/{sym}"))

    # ③ rebalance_drift (HELD with a target): Swedroe 5/25 — rebalance when the drift
    # crosses EITHER the absolute band OR 25% of the target, whichever comes FIRST, i.e.
    # the TIGHTER band: min(absolute_band, 0.25 × target). The relative leg exists to
    # tighten the band for small allocations (target 10% → 2.5pp governs, not 5pp).
    # (Deep review 2026-07-13: max() inverted the rule — relative leg was dead code for
    # small targets and RAISED the band for large ones.) No target → silent. current
    # weight is aggregated per symbol across accounts (holdings are per-row).
    if (rules.rebalance_drift.enabled
            and rules.rebalance_drift.value is not None and targets):
        abs_band = rules.rebalance_drift.value
        current_by_sym: dict[str, Decimal] = {}
        for h in data.holdings:
            if h.weight is not None:
                current_by_sym[h.symbol] = current_by_sym.get(h.symbol, _ZERO) + h.weight
        for sym, target in targets.items():
            cur = current_by_sym.get(sym)
            if cur is None:
                continue  # not held (or unpriced) → the drift rule is silent for it
            drift = abs(cur - target)
            band = min(abs_band, _REBALANCE_REL * target)
            if drift > band:
                alerts.append(Alert(
                    id=f"rebalance_drift:{sym}", sev="risk", rule="rebalance_drift",
                    title=f"{sym} 偏離目標配置",
                    detail=(f"現權重 {_pct(cur)} 偏離目標 {_pct(target)} 達 {_pct(drift)}"
                            f"＞帶寬 {_pct(band)}"),
                    href=f"/symbol/{sym}"))

    # ④ consensus_change (held AND watch): rating worsened by >= threshold (1..5 scale, so
    # worse = higher) OR mean target price cut by >= 10%. Latest snapshot vs the closest one
    # >= 7 days older; a missing leg does not fire, both missing → silent.
    if (rules.consensus_change.enabled
            and rules.consensus_change.value is not None and consensus):
        rating_thr = rules.consensus_change.value
        for sym, d in consensus.items():
            rating_worse = (
                d.score_now is not None and d.score_then is not None
                and d.score_now - d.score_then >= rating_thr
            )
            price_cut = (
                d.target_mean_now is not None and d.target_mean_then is not None
                and d.target_mean_then > _ZERO
                and (d.target_mean_then - d.target_mean_now) / d.target_mean_then
                >= _CONSENSUS_PRICE_CUT
            )
            if not (rating_worse or price_cut):
                continue
            parts: list[str] = []
            if rating_worse and d.score_then is not None and d.score_now is not None:
                parts.append(f"評級由 {_score(d.score_then)} 惡化至 {_score(d.score_now)}")
            if price_cut and d.target_mean_then is not None and d.target_mean_now is not None:
                cut = (d.target_mean_then - d.target_mean_now) / d.target_mean_then
                parts.append(f"目標均價下修 {_pct(cut)}")
            window = f"（對比 {d.days_apart} 日前）" if d.days_apart is not None else ""
            alerts.append(Alert(
                id=f"consensus_change:{sym}", sev="info", rule="consensus_change",
                title=f"{sym} 分析師共識轉弱",
                detail="；".join(parts) + window,
                href=f"/symbol/{sym}"))

    return alerts


def compute_alerts(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency,
    calib_gap: Decimal | None = None,
) -> list[Alert]:
    """Build the dashboard once, read rules + quota, run the single rule core.

    ``calib_gap`` (the portfolio-wide AI calibration error in pp) is FED IN by the api
    layer (``api.insight_service.calibration_gap`` — strategy/ never imports llm_insight).
    The scheduler's ``_compute_alerts_for_scan`` calls this WITHOUT ``calib_gap`` → None →
    no calib_gap alert in the scan; that is intentional/acceptable (the calib_gap rule is
    a dashboard/settings surface, not a scan trigger). Account display names are read
    here and fed into the pure core (FH2 fix: the bell shows「Moomoo MY (US)」, never
    the raw ``moomoo_my_us``).
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    return compute_alerts_from(
        data, get_alert_rules(conn),
        quota_remaining=budget_remaining(conn),
        quota_threshold=get_alert_threshold(conn),
        ai_active=ai_active(conn),
        calib_gap=calib_gap,
        account_names=account_display_names(conn),
    )


def account_display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """account_id → display name from the accounts table ({} when the table is absent)."""
    try:
        return {
            str(r["account_id"]): str(r["name"])
            for r in conn.execute("SELECT account_id, name FROM accounts")
        }
    except sqlite3.OperationalError:
        return {}
