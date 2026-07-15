"""Digest assembly (P3 batch 3 · Wave 1) — the conn-bearing seam: portfolio + pricing →
stored digest → gated push.

Registered as the ``digest_daily`` / ``digest_weekly`` runner at app startup
(``register_digest_runner``), so ``scheduler/`` never imports this (architecture.md:
scheduler triggers only). Mirrors ``api/news_service.py``: everything network / LLM / core
lives behind this api-layer seam; the assembly reads the computed dashboard + stored prices
and NEVER fabricates a number — every block degrades honestly to ``null`` / ``[]`` /
``excluded_count`` when its data is missing.

Money invariant: every price / percentage in a payload is a Decimal **string**
(``shared.wire.decimal_str``); the frontend formats, never computes. The PUSH text
(B3-D4, hard rule) carries counts + percentages ONLY — never a currency amount.

The optional LLM one-liner (owner ruling B3-D3, default OFF) narrates only the numbers it
is handed; any failure yields ``null`` — the digest NEVER fails because of the LLM.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from portfolio_dash.api import auth_store
from portfolio_dash.ops import digest as digest_store
from portfolio_dash.ops import notify
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    ExDividendItem,
    HoldingRow,
)
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared import llm
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import ai_active
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy.alerts import Alert

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
# Enough calendar slack to find the last TWO stored closes across a weekend / holiday gap.
_DAY_CHANGE_LOOKBACK_DAYS = 14
_STALE_AGE_DAYS = 3          # a held symbol's latest close older than this = data_health line
_EXDIV_WINDOW_DAYS = 14      # weekly: ex-dividends within the next fortnight
_WEEK_DAYS = 7               # weekly review / signal / chores lookback
_MOVERS_N = 3                # top-N up + top-N down
_LLM_PROMPT_VERSION = "digest-daily-note-v1"

# rule_id -> (zh label, severity) from the single push catalog (single source of labels).
_RULE_META: dict[str, tuple[str, str]] = {
    rid: (label, sev) for rid, label, sev in notify.RULE_CATALOG
}


def _try[T](fn: Callable[[], T], default: T) -> T:
    """Run *fn*; on ANY failure log + return *default* (honest per-block degradation)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a failing block degrades to a default, never crashes
        logger.warning("digest block failed", exc_info=True)
        return default


def _today(now: datetime) -> str:
    return now.date().isoformat()


# --- pure day-change math (unit-tested without a DB) --------------------------


def _pct_from_last_two(closes: list[Decimal]) -> Decimal | None:
    """Day-change ratio from the last two stored closes, or ``None``.

    ``None`` when fewer than two closes exist (excluded from the portfolio move + counted)
    or when the prior close is zero (no honest percentage). Price-only (quote ccy) — this
    is a pure price move and deliberately EXCLUDES FX drift.
    """
    if len(closes) < 2:
        return None
    prev, last = closes[-2], closes[-1]
    if prev == 0:
        return None
    return (last - prev) / prev


def _weighted_pct(
    weights: list[tuple[str, Decimal]], per_symbol_pct: dict[str, Decimal | None]
) -> tuple[Decimal | None, int]:
    """Value-weighted portfolio day-change + excluded held-symbol count.

    ``weights`` is ``(symbol, reporting-ccy weight)`` per held holding row (a symbol may
    appear in multiple rows across accounts). A symbol whose day-change is ``None`` (missing
    two closes) is excluded and counted ONCE. Returns ``(Σ(w·pct)/Σw, excluded_count)`` or
    ``(None, excluded_count)`` when nothing is computable.
    """
    weighted = Decimal(0)
    total = Decimal(0)
    excluded: set[str] = set()
    counted = False
    for sym, w in weights:
        pct = per_symbol_pct.get(sym)
        if pct is None:
            excluded.add(sym)
            continue
        weighted += w * pct
        total += w
        counted = True
    if not counted or total == 0:
        return None, len(excluded)
    return weighted / total, len(excluded)


def _movers(
    per_symbol_pct: dict[str, Decimal | None],
    names: dict[str, str],
    *,
    meta: dict[str, tuple[str | None, str | None]] | None = None,
    n: int = _MOVERS_N,
) -> dict[str, list[dict[str, str]]]:
    """Top-*n* up + top-*n* down symbols by day-change %.

    Each entry is ``{symbol, name, pct}`` plus, when the per-symbol *meta* map supplies
    them, ``quote_date`` (the later close's ``as_of_date``) and ``fetched_at`` (that price
    row's fetch timestamp) — both ISO strings the movers tooltip renders. Absent meta keys
    are simply omitted (older stored digests never carried them; the renderer tolerates it).
    """
    meta = meta or {}
    ranked: list[tuple[str, Decimal]] = sorted(
        ((sym, pct) for sym, pct in per_symbol_pct.items() if pct is not None),
        key=lambda kv: kv[1],
        reverse=True,
    )

    def _entry(sym: str, pct: Decimal) -> dict[str, str]:
        entry = {"symbol": sym, "name": names.get(sym, sym), "pct": decimal_str(pct)}
        quote_date, fetched_at = meta.get(sym, (None, None))
        if quote_date is not None:
            entry["quote_date"] = quote_date
        if fetched_at is not None:
            entry["fetched_at"] = fetched_at
        return entry

    ups = [_entry(s, p) for s, p in ranked if p > 0][:n]
    downs = [_entry(s, p) for s, p in reversed(ranked) if p < 0][:n]
    return {"up": ups, "down": downs}


# --- daily block assembly (reads the DB) --------------------------------------


def _held_symbols_and_names(data: DashboardData) -> tuple[list[str], dict[str, str]]:
    """Distinct held symbols (shares > 0) + a symbol → display-name map."""
    names: dict[str, str] = {}
    held: list[str] = []
    for h in data.holdings:
        if h.shares > 0:
            names[h.symbol] = h.name
            if h.symbol not in held:
                held.append(h.symbol)
    return held, names


def _holding_weights(rows: list[HoldingRow]) -> list[tuple[str, Decimal]]:
    """``(symbol, reporting-ccy weight)`` for every held row (weight None → 0)."""
    out: list[tuple[str, Decimal]] = []
    for row in rows:
        if row.shares > 0:
            out.append((row.symbol, row.weight if row.weight is not None else Decimal(0)))
    return out


def _per_symbol_day_change(
    conn: sqlite3.Connection, symbols: list[str], *, now: datetime
) -> tuple[dict[str, Decimal | None], str | None, dict[str, tuple[str | None, str | None]]]:
    """Per-symbol day-change ratio (last two stored closes) + the latest close date used +
    per-symbol ``(quote_date, fetched_at)`` metadata for the later close feeding each pct.

    The metadata reuses the SAME ``get_price_history`` read (no extra query): the later close
    is ``hist[-1]``, whose ``as_of`` is the quote date and whose ``fetched_at`` is the fetch
    timestamp. Only symbols with a computable pct get a metadata entry.
    """
    start = now.date() - timedelta(days=_DAY_CHANGE_LOOKBACK_DAYS)
    end = now.date()
    out: dict[str, Decimal | None] = {}
    meta: dict[str, tuple[str | None, str | None]] = {}
    latest_as_of: date | None = None
    for sym in symbols:
        hist = get_price_history(conn, sym, start, end)
        pct = _pct_from_last_two([p.value for p in hist])
        out[sym] = pct
        if pct is not None and hist:
            later = hist[-1]
            fetched = later.fetched_at.isoformat() if later.fetched_at is not None else None
            meta[sym] = (later.as_of.isoformat(), fetched)
            if latest_as_of is None or later.as_of > latest_as_of:
                latest_as_of = later.as_of
    return out, (latest_as_of.isoformat() if latest_as_of is not None else None), meta


def _alerts_today(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Today's alert events (excluding signal_*), grouped by rule with count + symbols.

    When AI is inactive, a ``quota_low`` event is suppressed (LOW-2): the same gate the
    live-alert engine applies (a low LLM budget is only worth surfacing when AI is usable),
    so a first digest can't resurface a quota_low the running system would never show. A
    legitimately-fired quota_low (AI on) is still included even after it is consumed/notified.
    """
    extra = "" if ai_active(conn) else " AND rule_id != 'quota_low'"
    rows = conn.execute(
        "SELECT rule_id, symbol FROM alert_events "
        "WHERE substr(fired_at,1,10) = ? AND rule_id NOT LIKE 'signal_%'"
        + extra + " ORDER BY id",
        (_today(now),),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = str(r["rule_id"])
        label, sev = _RULE_META.get(rid, (rid, "info"))
        g = grouped.setdefault(
            rid,
            {"rule_id": rid, "label": label, "severity": sev, "count": 0, "symbols": []},
        )
        g["count"] = int(g["count"]) + 1
        if r["symbol"]:
            g["symbols"].append(str(r["symbol"]))
    return list(grouped.values())


def _signals_today(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Today's signal_* transition events (rule_id + symbol)."""
    rows = conn.execute(
        "SELECT rule_id, symbol FROM alert_events "
        "WHERE substr(fired_at,1,10) = ? AND rule_id LIKE 'signal_%' ORDER BY id",
        (_today(now),),
    ).fetchall()
    return [
        {"rule_id": str(r["rule_id"]), "symbol": (str(r["symbol"]) if r["symbol"] else None)}
        for r in rows
    ]


def _data_health(
    conn: sqlite3.Connection, held: list[str], *, now: datetime
) -> dict[str, Any]:
    """Held symbols with a stale/absent latest close (+ age) and today's failed-job count."""
    stale: list[dict[str, Any]] = []
    for sym in sorted(set(held)):
        pr = get_latest_price(conn, sym, now=now)
        if pr is None:
            stale.append({"symbol": sym, "age_days": None})
        else:
            age = (now.date() - pr.as_of).days
            if age > _STALE_AGE_DAYS:
                stale.append({"symbol": sym, "age_days": age})
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM job_runs "
        "WHERE status = 'error' AND substr(started_at,1,10) = ?",
        (_today(now),),
    ).fetchone()["n"]
    return {"stale": stale, "failed_jobs": int(failed)}


# --- optional LLM one-liner (default OFF; NEVER fails generation) --------------


def _note_prompt(payload: dict[str, Any]) -> str:
    """Build the one-liner prompt: hand the model the numbers, forbid new figures."""
    dc = payload.get("day_change", {})
    numbers = {
        "portfolio_day_change_pct": dc.get("portfolio_pct"),
        "movers_up": payload.get("movers", {}).get("up", []),
        "movers_down": payload.get("movers", {}).get("down", []),
        "alerts_today_count": sum(int(a.get("count", 0)) for a in payload.get("alerts_today", [])),
        "signals_today_count": len(payload.get("signals_today", [])),
        "stale_quote_count": len(payload.get("data_health", {}).get("stale", [])),
    }
    return (
        "你是投資組合摘要助理。以下是今日已計算好的數字（JSON）。\n"
        "<numbers>\n"
        f"{json.dumps(numbers, ensure_ascii=False)}\n"
        "</numbers>\n"
        "請用繁體中文寫『一句話』的收盤摘要，只能引用上面提供的數字，"
        "不得杜撰任何新數字或金額。不要加上金額符號。"
    )


def _llm_note(
    conn: sqlite3.Connection, cfg: digest_store.DigestConfig, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """The optional one-liner. Returns ``None`` unless enabled AND the LLM succeeds.

    Wrapped so the AI-active predicate / budget / provider failure (all raised by
    ``llm.complete_text``) yields ``None`` — the digest NEVER fails because of the LLM.
    """
    if not cfg.llm_summary_enabled:
        return None
    try:
        completion = llm.complete_text(_note_prompt(payload), agent="digest_note", conn=conn)
        text = completion.reply.strip()
        if not text:
            return None
        return {"text": text, "prompt_version": _LLM_PROMPT_VERSION, "model": completion.model}
    except Exception:  # noqa: BLE001 — the one-liner is best-effort; generation must not fail
        logger.warning("digest llm note failed", exc_info=True)
        return None


# --- push (B3-D4: counts + percentages ONLY, never amounts) -------------------


def _signed_pct(pct: str) -> str:
    """A Decimal-string ratio → signed percentage string (no currency, no thousands sep)."""
    q = (Decimal(pct) * 100).quantize(Decimal("0.01"))
    sign = "+" if q > 0 else ("−" if q < 0 else "")
    return f"{sign}{abs(q)}%"


def push_text(kind: str, payload: dict[str, Any], *, now: datetime) -> tuple[str, str]:
    """Compose the push ``(title, body)`` — counts + percentages + a jump hint ONLY.

    HARD RULE (B3-D4): the body contains NO currency amount. A unit test asserts this
    (regex guard). Kept module-public so that test can hit it directly.
    """
    md = now.strftime("%m/%d")
    if kind == "weekly":
        items = payload.get("items", [])
        body = (
            f"本週待辦 {len(items)} 項・開啟儀表板查看"
            if items
            else "本週無待辦事項・開啟儀表板查看"
        )
        return f"週行動清單 {md}", body
    parts: list[str] = []
    pct = payload.get("day_change", {}).get("portfolio_pct")
    if pct is not None:
        parts.append(f"組合 {_signed_pct(str(pct))}")
    movers = payload.get("movers", {})
    parts.append(f"上漲 {len(movers.get('up', []))}・下跌 {len(movers.get('down', []))}")
    parts.append(f"警示 {sum(int(a.get('count', 0)) for a in payload.get('alerts_today', []))}")
    parts.append(f"訊號 {len(payload.get('signals_today', []))}")
    parts.append("開啟儀表板查看")
    return f"收盤摘要 {md}", "・".join(parts)


def _push(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    now: datetime,
    sender: notify.Sender,
) -> str:
    """Dispatch the digest to the enabled channels (gated). Returns a short push note.

    Gates (in order): guest/demo mode → suppress outbound dispatch entirely (FU-D4 — the
    digest run is open in guest mode so the demo can be exercised, but outbound push stays
    locked; the stored digest is unaffected); no enabled channel → skip; rule unsubscribed →
    skip; quiet hours → skip. Never raises (``sender`` isolates channel failures). The push
    text carries counts + percentages only (B3-D4).
    """
    if not auth_store.is_protected(conn):
        logger.info(
            "digest push suppressed in guest/demo mode (kind=%s): outbound stays locked", kind
        )
        return "示範模式略過推播"
    cfg = notify.load_config(conn)
    rule_id = f"digest_{kind}"
    channels = notify.build_enabled_channels(cfg)
    if not channels:
        return "無啟用通道"
    if not cfg.subscriptions.get(rule_id, True):
        return "未訂閱"
    if notify.in_quiet_hours(cfg.quiet_hours, now):
        return "靜音時段略過推播"
    title, body = push_text(kind, payload, now=now)
    outcome = sender(channels, title, body, "info", None)
    ok = sum(1 for v in outcome.values() if v == "ok")
    return f"推播 {ok}/{len(channels)} 通道"


# --- daily / weekly runners ---------------------------------------------------


def run_digest_daily(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    sender: notify.Sender = notify.dispatch,
) -> str:
    """Assemble → store → push the daily close digest. Returns a ``job_runs`` summary.

    Every block degrades honestly (a failing block → null / [] / 0), so a stored digest is
    always produced. The optional LLM one-liner is added last and never fails generation.
    """
    cfg = digest_store.load_config(conn)
    data = build_dashboard(conn, now=now, reporting=reporting)
    held, names = _held_symbols_and_names(data)

    # Each block is bound to a typed local so a failing block degrades to a typed default
    # (the payload is opaque ``dict[str, Any]``, so the annotations must live here). The
    # tuple defaults are typed constants — an inline ``({}, None)`` would infer too narrow
    # for the invariant dict inside the tuple.
    empty_day_change: tuple[
        dict[str, Decimal | None], str | None, dict[str, tuple[str | None, str | None]]
    ] = ({}, None, {})
    per_symbol_pct, as_of, mover_meta = _try(
        lambda: _per_symbol_day_change(conn, held, now=now), empty_day_change
    )
    empty_pf: tuple[Decimal | None, int] = (None, 0)
    pf_pct, excluded = _try(
        lambda: _weighted_pct(_holding_weights(data.holdings), per_symbol_pct), empty_pf
    )
    movers: dict[str, list[dict[str, str]]] = _try(
        lambda: _movers(per_symbol_pct, names, meta=mover_meta), {"up": [], "down": []}
    )
    alerts_today: list[dict[str, Any]] = _try(lambda: _alerts_today(conn, now), [])
    signals_today: list[dict[str, Any]] = _try(lambda: _signals_today(conn, now), [])
    data_health: dict[str, Any] = _try(
        lambda: _data_health(conn, held, now=now), {"stale": [], "failed_jobs": 0}
    )
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "daily",
        "digest_date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "day_change": {
            "portfolio_pct": decimal_str(pf_pct) if pf_pct is not None else None,
            "excluded_count": excluded,
            "as_of": as_of,
        },
        "movers": movers,
        "alerts_today": alerts_today,
        "signals_today": signals_today,
        "data_health": data_health,
        "llm_note": None,
    }
    payload["llm_note"] = _llm_note(conn, cfg, payload)

    digest_store.upsert_digest(
        conn,
        kind="daily",
        digest_date=str(payload["digest_date"]),
        payload=json.dumps(payload, ensure_ascii=False),
        generated_at=str(payload["generated_at"]),
    )
    push = _push(conn, "daily", payload, now=now, sender=sender)
    dc = payload["day_change"]["portfolio_pct"]
    return (
        f"daily digest {payload['digest_date']}: 組合 {dc if dc is not None else '—'}, "
        f"警示 {len(payload['alerts_today'])}, 訊號 {len(payload['signals_today'])}; {push}"
    )


# --- weekly action-list blocks ------------------------------------------------


def _drift_symbols(alerts: list[Alert]) -> list[str]:
    """Extract the per-target symbol from ``rule:symbol`` alert ids (skip global ids)."""
    out: list[str] = []
    for a in alerts:
        prefix = f"{a.rule}:"
        if a.id.startswith(prefix):
            out.append(a.id[len(prefix):])
    return out


def _alert_review_week(conn: sqlite3.Connection, *, now: datetime) -> list[dict[str, Any]]:
    """Last-7-day alert events (excluding signal_*), grouped by rule with count + severity.

    Suppresses ``quota_low`` when AI is inactive (LOW-2), mirroring ``_alerts_today``."""
    since = (now.date() - timedelta(days=_WEEK_DAYS)).isoformat()
    extra = "" if ai_active(conn) else " AND rule_id != 'quota_low'"
    rows = conn.execute(
        "SELECT rule_id, COUNT(*) AS n FROM alert_events "
        "WHERE substr(fired_at,1,10) >= ? AND rule_id NOT LIKE 'signal_%'"
        + extra + " GROUP BY rule_id ORDER BY n DESC, rule_id",
        (since,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        rid = str(r["rule_id"])
        label, sev = _RULE_META.get(rid, (rid, "info"))
        out.append({"rule_id": rid, "label": label, "severity": sev, "count": int(r["n"])})
    return out


def _signal_week(conn: sqlite3.Connection, *, now: datetime) -> list[str]:
    """Distinct symbols with a signal_* transition in the last 7 days."""
    since = (now.date() - timedelta(days=_WEEK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM alert_events "
        "WHERE substr(fired_at,1,10) >= ? AND rule_id LIKE 'signal_%' AND symbol IS NOT NULL "
        "ORDER BY symbol",
        (since,),
    ).fetchall()
    return [str(r["symbol"]) for r in rows]


def _upcoming_exdiv(calendar: list[ExDividendItem], *, now: datetime) -> list[str]:
    """Held ex-dividends within the next fortnight → ``["2330(2026-07-20)", ...]``."""
    horizon = now.date() + timedelta(days=_EXDIV_WINDOW_DAYS)
    out: list[str] = []
    for item in calendar:
        if now.date() <= item.ex_date <= horizon:
            out.append(f"{item.symbol}({item.ex_date.isoformat()})")
    return out


def _chores(conn: sqlite3.Connection, data: DashboardData, *, now: datetime) -> dict[str, Any]:
    """Stale held quotes (> 3d / absent) + failed jobs in the last 7 days."""
    stale: list[str] = []
    seen: set[str] = set()
    for h in data.holdings:
        if h.shares <= 0 or h.symbol in seen:
            continue
        seen.add(h.symbol)
        pr = get_latest_price(conn, h.symbol, now=now)
        if pr is None or (now.date() - pr.as_of).days > _STALE_AGE_DAYS:
            stale.append(h.symbol)
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM job_runs "
        "WHERE status = 'error' AND substr(started_at,1,10) >= ?",
        ((now.date() - timedelta(days=_WEEK_DAYS)).isoformat(),),
    ).fetchone()["n"]
    return {"stale": stale, "failed_jobs": int(failed)}


def _weekly_items(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime, reporting: Currency
) -> list[dict[str, Any]]:
    """The computed weekly action items (each block skips itself when it has no data)."""
    # Imported here (not at module top) to keep the import graph shallow + avoid any cycle
    # via api.alert_inputs (api → api is allowed; this is the single call site).
    from portfolio_dash.api.alert_inputs import compute_alerts_full

    items: list[dict[str, Any]] = []

    drift: list[Alert] = _try(
        lambda: [
            a
            for a in compute_alerts_full(conn, now=now, reporting=reporting)
            if a.rule == "rebalance_drift"
        ],
        [],
    )
    if drift:
        syms = _drift_symbols(drift)
        desc = f"{len(drift)} 檔偏離目標區間" + (f"：{'、'.join(syms)}" if syms else "")
        items.append({
            "id": "rebalance", "icon": "⚖", "title": "再平衡漂移", "desc": desc,
            "href": "index.html", "target": ".rb-open-btn",
        })

    review: list[dict[str, Any]] = _try(lambda: _alert_review_week(conn, now=now), [])
    if review:
        total = sum(int(g["count"]) for g in review)
        top = max(review, key=lambda g: int(g["count"]))
        items.append({
            "id": "alert-review", "icon": "\U0001f514", "title": "本週警示回顧",
            "desc": f"共 {total} 筆（最多：{top['label']} {top['count']}）",
            "href": "settings.html#alerts", "target": "#alert-rules-wrap",
        })

    sig: list[str] = _try(lambda: _signal_week(conn, now=now), [])
    if sig:
        items.append({
            "id": "signals", "icon": "\U0001f4c8", "title": "本週訊號轉折",
            "desc": f"{len(sig)} 檔：{'、'.join(sig)}",
            "href": "instruments.html", "target": 'section[data-screen-label="標的清單"]',
        })

    exdiv: list[str] = _try(lambda: _upcoming_exdiv(data.ex_dividend_calendar, now=now), [])
    if exdiv:
        items.append({
            "id": "exdiv", "icon": "\U0001f4b0", "title": "即將除息",
            "desc": f"{len(exdiv)} 檔：{'、'.join(exdiv)}",
            "href": "trades.html", "target": None,
        })

    chores: dict[str, Any] = _try(
        lambda: _chores(conn, data, now=now), {"stale": [], "failed_jobs": 0}
    )
    if chores["stale"] or chores["failed_jobs"]:
        items.append({
            "id": "chores", "icon": "\U0001f6e0", "title": "資料與系統待辦",
            "desc": f"停滯報價 {len(chores['stale'])}・近 7 日失敗工作 {chores['failed_jobs']}",
            "href": "settings.html#scheduler", "target": None,
        })
    return items


def run_digest_weekly(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    sender: notify.Sender = notify.dispatch,
) -> str:
    """Assemble → store → push the weekly action list. Returns a ``job_runs`` summary.

    An empty week still generates + stores a digest (``items: []``) so the card shows the
    friendly empty copy rather than a stale prior week.
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    items: list[dict[str, Any]] = _try(
        lambda: _weekly_items(conn, data, now=now, reporting=reporting), []
    )
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "weekly",
        "digest_date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "items": items,
    }
    digest_store.upsert_digest(
        conn,
        kind="weekly",
        digest_date=str(payload["digest_date"]),
        payload=json.dumps(payload, ensure_ascii=False),
        generated_at=str(payload["generated_at"]),
    )
    push = _push(conn, "weekly", payload, now=now, sender=sender)
    return f"weekly digest {payload['digest_date']}: {len(items)} 項; {push}"


def run_digest(conn: sqlite3.Connection, kind: str, *, now: datetime) -> str:
    """The registered runner seam: dispatch by *kind* (``daily`` / ``weekly``).

    Registered via ``scheduler.jobs.register_digest_runner`` at app startup, so the
    scheduler never imports this module. An unknown kind falls back to daily (the caller —
    the two static JobSpecs — only ever passes a valid kind).
    """
    if kind == "weekly":
        return run_digest_weekly(conn, now=now)
    return run_digest_daily(conn, now=now)


__all__ = ["push_text", "run_digest", "run_digest_daily", "run_digest_weekly"]
