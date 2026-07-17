"""Unit tests for the digest leaf + assembly (P3 batch 3 · Wave 1).

Covers the pure day-change math (no DB), the push-body NO-AMOUNTS guard (B3-D4, mandated),
the config seed/backfill + upsert idempotency (ops.digest), the weekly item sub-helpers,
and end-to-end daily/weekly assembly over the golden DB (determinism + honest degradation +
push gating). Hermetic — no network (the push sender is injected).
"""

import json
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.api import digest_service as ds
from portfolio_dash.api.auth_store import create_auth_tables, create_user
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events_tables
from portfolio_dash.ops import digest as digest_store
from portfolio_dash.ops import notify
from portfolio_dash.portfolio.dashboard_models import ExDividendItem
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler.jobs import create_scheduler_tables
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.llm_config import LLMRole, ModelConfig, set_role, upsert_model
from portfolio_dash.strategy.alerts import Alert
from tests.conftest import GOLDEN_NOW

TAIPEI = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 7, 14, 15, 0, tzinfo=TAIPEI)

# A currency amount = a NT$/RM/$ marker OR a thousands-grouped number (spec B3-D4 regex).
_AMOUNT_RE = re.compile(r"NT\$|\$|\bRM\b|\d{1,3}(,\d{3})+")


def _mem() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# --- pure day-change math -----------------------------------------------------


def test_pct_from_last_two() -> None:
    assert ds._pct_from_last_two([]) is None
    assert ds._pct_from_last_two([Decimal("10")]) is None
    assert ds._pct_from_last_two([Decimal("100"), Decimal("110")]) == Decimal("0.1")
    # only the LAST two matter
    assert ds._pct_from_last_two([Decimal("1"), Decimal("100"), Decimal("110")]) == Decimal("0.1")
    # a zero prior close → no honest percentage
    assert ds._pct_from_last_two([Decimal("0"), Decimal("5")]) is None


def test_weighted_pct_value_weighted() -> None:
    weights = [("A", Decimal("0.6")), ("B", Decimal("0.4"))]
    pcts: dict[str, Decimal | None] = {"A": Decimal("0.10"), "B": Decimal("0.05")}
    pct, excluded = ds._weighted_pct(weights, pcts)
    assert pct == Decimal("0.08")  # 0.6*0.10 + 0.4*0.05
    assert excluded == 0


def test_weighted_pct_excludes_missing_history() -> None:
    weights = [("A", Decimal("0.6")), ("B", Decimal("0.4"))]
    pcts: dict[str, Decimal | None] = {"A": Decimal("0.10"), "B": None}
    pct, excluded = ds._weighted_pct(weights, pcts)
    assert pct == Decimal("0.10")  # only A counted, renormalized over its own weight
    assert excluded == 1


def test_weighted_pct_all_missing_is_none() -> None:
    weights = [("A", Decimal("0.6"))]
    pct, excluded = ds._weighted_pct(weights, {"A": None})
    assert pct is None and excluded == 1


def test_weighted_pct_dedupes_excluded_symbol_across_rows() -> None:
    # A held in two accounts, both missing history → counted ONCE.
    weights = [("A", Decimal("0.3")), ("A", Decimal("0.3"))]
    pct, excluded = ds._weighted_pct(weights, {"A": None})
    assert pct is None and excluded == 1


def test_movers_ranks_up_and_down() -> None:
    pcts: dict[str, Decimal | None] = {
        "A": Decimal("0.05"), "B": Decimal("-0.03"), "C": Decimal("0.10"), "D": None,
    }
    names = {"A": "Alpha", "B": "Beta", "C": "Gamma"}
    out = ds._movers(pcts, names, n=3)
    assert [m["symbol"] for m in out["up"]] == ["C", "A"]  # highest first
    assert [m["symbol"] for m in out["down"]] == ["B"]     # most negative first
    assert out["up"][0]["name"] == "Gamma"
    assert out["up"][0]["pct"] == "0.10"  # Decimal string, not computed
    # no meta supplied → quote_date / fetched_at / close keys are simply absent (old shape)
    assert "quote_date" not in out["up"][0] and "fetched_at" not in out["up"][0]
    assert "close" not in out["up"][0]


def test_movers_carries_quote_date_fetched_at_and_close_from_meta() -> None:
    pcts: dict[str, Decimal | None] = {"A": Decimal("0.05"), "B": Decimal("-0.03")}
    names = {"A": "Alpha", "B": "Beta"}
    meta: dict[str, tuple[str | None, str | None, str | None]] = {
        "A": ("2026-07-14", "2026-07-14T15:00:00+08:00", "185.50"),
        "B": ("2026-07-14", None, None),  # fetched_at + close missing → those keys omitted
    }
    out = ds._movers(pcts, names, meta=meta, n=3)
    up = out["up"][0]
    assert up["symbol"] == "A"
    assert up["quote_date"] == "2026-07-14"
    assert up["fetched_at"] == "2026-07-14T15:00:00+08:00"
    assert up["close"] == "185.50"  # FU-D14: later close as a Decimal string
    down = out["down"][0]
    assert down["symbol"] == "B" and down["quote_date"] == "2026-07-14"
    assert "fetched_at" not in down and "close" not in down  # None meta values → keys omitted


# --- push body: NO currency amounts (B3-D4 hard rule) -------------------------


def test_push_text_daily_has_no_currency_amount() -> None:
    payload = {
        "day_change": {"portfolio_pct": "0.0123"},
        "movers": {
            "up": [{"symbol": "AAPL", "pct": "0.02"}],
            "down": [{"symbol": "2330", "pct": "-0.01"}],
        },
        "alerts_today": [{"rule_id": "single_weight", "count": 4}],
        "signals_today": [{"rule_id": "signal_trend", "symbol": "AAPL"}],
    }
    title, body = ds.push_text("daily", payload, now=NOW)
    assert title.startswith("收盤摘要")
    assert not _AMOUNT_RE.search(body), f"push body carried an amount: {body!r}"
    assert "+1.23%" in body           # the percentage IS present
    assert "警示 4" in body           # small counts allowed
    assert "開啟儀表板查看" in body


def test_push_text_daily_negative_and_thousands_free() -> None:
    # A big count must still never render a thousands-grouped number in the push text.
    payload = {
        "day_change": {"portfolio_pct": "-0.0250"},
        "movers": {"up": [], "down": []},
        "alerts_today": [{"rule_id": "x", "count": 1200}],
        "signals_today": [],
    }
    _, body = ds.push_text("daily", payload, now=NOW)
    assert "−2.50%" in body
    assert not _AMOUNT_RE.search(body), f"push body carried a thousands amount: {body!r}"


def test_push_text_weekly_counts_only() -> None:
    _, body_full = ds.push_text("weekly", {"items": [1, 2, 3]}, now=NOW)
    assert body_full == "本週待辦 3 項・開啟儀表板查看"
    _, body_empty = ds.push_text("weekly", {"items": []}, now=NOW)
    assert body_empty == "本週無待辦事項・開啟儀表板查看"


def test_push_text_daily_linked_drops_jump_hint() -> None:
    # FU-D17: when a deep link will be attached, the redundant 「開啟儀表板查看」 hint is
    # dropped; the substance (percentage + counts) stays and no amount ever appears.
    payload = {
        "day_change": {"portfolio_pct": "0.0123"},
        "movers": {"up": [], "down": []},
        "alerts_today": [{"rule_id": "x", "count": 2}],
        "signals_today": [],
    }
    _, body = ds.push_text("daily", payload, now=NOW, linked=True)
    assert "開啟儀表板查看" not in body
    assert "組合 +1.23%" in body and "警示 2" in body
    assert not _AMOUNT_RE.search(body)


def test_push_text_weekly_linked_drops_jump_hint() -> None:
    _, body_full = ds.push_text("weekly", {"items": [1, 2]}, now=NOW, linked=True)
    assert body_full == "本週待辦 2 項"
    _, body_empty = ds.push_text("weekly", {"items": []}, now=NOW, linked=True)
    assert body_empty == "本週無待辦事項"


# --- config seed / backfill + upsert idempotency ------------------------------


def test_config_default_is_llm_off() -> None:
    c = _mem()
    cfg = digest_store.load_config(c)
    assert cfg.llm_summary_enabled is False


def test_config_save_round_trip() -> None:
    c = _mem()
    cfg = digest_store.load_config(c)
    cfg.llm_summary_enabled = True
    digest_store.save_config(c, cfg, now=NOW)
    assert digest_store.load_config(c).llm_summary_enabled is True


def test_upsert_digest_is_idempotent_per_day() -> None:
    c = _mem()
    digest_store.upsert_digest(c, kind="daily", digest_date="2026-07-14",
                               payload=json.dumps({"v": 1}), generated_at="t1")
    digest_store.upsert_digest(c, kind="daily", digest_date="2026-07-14",
                               payload=json.dumps({"v": 2}), generated_at="t2")
    n = c.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='daily'").fetchone()["n"]
    assert n == 1  # overwrite, never duplicate
    latest = digest_store.get_latest(c, "daily")
    assert latest is not None and latest["payload"] == {"v": 2} and latest["generated_at"] == "t2"


def test_get_history_pages_newest_first() -> None:
    c = _mem()
    for d in ("2026-07-10", "2026-07-11", "2026-07-12"):
        digest_store.upsert_digest(c, kind="daily", digest_date=d,
                                   payload=json.dumps({"d": d}), generated_at=d)
    total, rows = digest_store.get_history(c, "daily", offset=0, limit=2)
    assert total == 3
    assert [r["digest_date"] for r in rows] == ["2026-07-12", "2026-07-11"]
    _, page2 = digest_store.get_history(c, "daily", offset=2, limit=2)
    assert [r["digest_date"] for r in page2] == ["2026-07-10"]


# --- weekly item sub-helpers --------------------------------------------------


def test_drift_symbols_extracts_targets() -> None:
    alerts = [
        Alert(id="rebalance_drift:2330", sev="risk", rule="rebalance_drift", title="", detail=""),
        Alert(id="rebalance_drift:AAPL", sev="risk", rule="rebalance_drift", title="", detail=""),
        # a global (no-symbol) drift id must be skipped
        Alert(id="rebalance_drift", sev="risk", rule="rebalance_drift", title="", detail=""),
    ]
    assert ds._drift_symbols(alerts) == ["2330", "AAPL"]


def test_alert_review_and_signal_week() -> None:
    c = _mem()
    ensure_alert_events_tables(c)
    now = NOW
    # within 7 days
    for _ in range(2):
        c.execute("INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?,?,?,0)",
                  ("single_weight", "2330", (now.date().isoformat() + "T09:00:00")))
    c.execute("INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?,?,?,0)",
              ("signal_trend", "AAPL", now.date().isoformat() + "T09:00:00"))
    # outside 7 days (must be excluded)
    c.execute("INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?,?,?,0)",
              ("single_weight", "2330", "2026-07-01T09:00:00"))
    c.commit()
    review = ds._alert_review_week(c, now=now)
    assert review and review[0]["rule_id"] == "single_weight" and review[0]["count"] == 2
    assert review[0]["label"] == "單一標的集中度"
    sig = ds._signal_week(c, now=now)
    assert sig == ["AAPL"]


# --- LOW-2: quota_low gated on ai_active in the digest -------------------------


def _insert_quota_low(conn: sqlite3.Connection, now: datetime) -> None:
    # a consumed+notified quota_low (the normal dispatch flow) — the gate is ai_active,
    # NOT consumed/notified: a legitimately-fired one (AI on) must still appear.
    conn.execute(
        "INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?,?,?,1)",
        ("quota_low", None, now.date().isoformat() + "T09:00:00"),
    )
    conn.commit()


def _bind_enabled_model(conn: sqlite3.Connection) -> None:
    upsert_model(conn, ModelConfig(
        id="m1", model_alias="m1", provider="anthropic", model_name="claude-sonnet-4-5",
        api_key="sk-abcdef1234567890a2f", vision=True,
        input_price_per_mtok=Decimal("3.00"), output_price_per_mtok=Decimal("15.00"),
        context_window=200000, max_output_tokens=8192, timeout_seconds=60, max_retries=2,
        enabled=True))
    set_role(conn, LLMRole.DEFAULT, "m1")


def test_quota_low_absent_from_digest_when_ai_inactive() -> None:
    c = _mem()
    ensure_alert_events_tables(c)  # no llm tables -> ai_active degrades to False
    _insert_quota_low(c, NOW)
    # a normal alert proves ONLY quota_low is filtered
    c.execute("INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?,?,?,0)",
              ("single_weight", "2330", NOW.date().isoformat() + "T09:00:00"))
    c.commit()
    today_rids = {g["rule_id"] for g in ds._alerts_today(c, NOW)}
    assert "quota_low" not in today_rids and "single_weight" in today_rids
    review_rids = {g["rule_id"] for g in ds._alert_review_week(c, now=NOW)}
    assert "quota_low" not in review_rids and "single_weight" in review_rids


def test_quota_low_present_in_digest_when_ai_active() -> None:
    c = _mem()
    bootstrap_db(c)                # llm_models / llm_defaults tables
    ensure_alert_events_tables(c)
    _bind_enabled_model(c)         # AI now usable -> quota_low is legitimate
    _insert_quota_low(c, NOW)
    assert "quota_low" in {g["rule_id"] for g in ds._alerts_today(c, NOW)}
    assert "quota_low" in {g["rule_id"] for g in ds._alert_review_week(c, now=NOW)}


def test_upcoming_exdiv_window() -> None:
    now = NOW
    cal = [
        ExDividendItem(symbol="2330", name="TSMC", ex_date=date(2026, 7, 20), source="t"),
        ExDividendItem(symbol="AAPL", name="Apple", ex_date=date(2026, 8, 30), source="t"),  # >14d
        ExDividendItem(symbol="OLD", name="Old", ex_date=date(2026, 7, 1), source="t"),       # past
    ]
    out = ds._upcoming_exdiv(cal, now=now)
    assert out == ["2330(2026-07-20)"]


# --- end-to-end assembly over the golden DB -----------------------------------


def _add_second_close(conn: sqlite3.Connection) -> None:
    """Golden DB has one close (2026-06-09); add 2026-06-10 so day-change computes."""
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 10),
                 close=Decimal("606"), source="test"),   # 600 -> 606 = +1%
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 10),
                 close=Decimal("123.6"), source="test"),  # 120 -> 123.6 = +3%
    ], fetched_at=GOLDEN_NOW)


def test_run_digest_daily_assembles_and_stores(golden_db: sqlite3.Connection) -> None:
    _add_second_close(golden_db)
    ds.run_digest_daily(golden_db, now=GOLDEN_NOW)
    latest = digest_store.get_latest(golden_db, "daily")
    assert latest is not None
    p = latest["payload"]
    assert p["schema_version"] == 1 and p["kind"] == "daily"
    # day-change computed (both held symbols moved up), nothing excluded
    assert p["day_change"]["portfolio_pct"] is not None
    assert p["day_change"]["excluded_count"] == 0
    up_by_sym = {m["symbol"]: m for m in p["movers"]["up"]}
    assert {"2330", "AAPL"} <= set(up_by_sym)
    # movers carry the later-close provenance from the same price read (quote_date +
    # fetched_at + close), threaded from get_price_history — the tooltip source (FU-D14).
    assert up_by_sym["2330"]["quote_date"] == "2026-06-10"  # the second close added above
    assert up_by_sym["2330"]["fetched_at"] == GOLDEN_NOW.isoformat()
    assert up_by_sym["2330"]["close"] == "606"  # the later close as a Decimal string
    assert p["llm_note"] is None  # default OFF


def test_run_digest_daily_is_deterministic(golden_db: sqlite3.Connection) -> None:
    _add_second_close(golden_db)
    ds.run_digest_daily(golden_db, now=GOLDEN_NOW)
    first = digest_store.get_latest(golden_db, "daily")
    ds.run_digest_daily(golden_db, now=GOLDEN_NOW)
    second = digest_store.get_latest(golden_db, "daily")
    assert first is not None and second is not None
    # identical apart from the generated_at stamp (which is the same frozen NOW here)
    assert first["payload"] == second["payload"]
    # re-run overwrote in place, never duplicated
    n = golden_db.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='daily'").fetchone()["n"]
    assert n == 1


def test_run_digest_daily_degrades_without_history(golden_db: sqlite3.Connection) -> None:
    # No second close added: both held symbols lack two closes → honest exclusion, null move.
    ds.run_digest_daily(golden_db, now=GOLDEN_NOW)
    p = digest_store.get_latest(golden_db, "daily")
    assert p is not None
    assert p["payload"]["day_change"]["portfolio_pct"] is None
    assert p["payload"]["day_change"]["excluded_count"] >= 1


def test_run_digest_weekly_stores_items_list(golden_db: sqlite3.Connection) -> None:
    summary = ds.run_digest_weekly(golden_db, now=GOLDEN_NOW)
    assert "weekly digest" in summary
    p = digest_store.get_latest(golden_db, "weekly")
    assert p is not None and p["payload"]["kind"] == "weekly"
    assert isinstance(p["payload"]["items"], list)  # empty-week still generates a list


# --- push gating (quiet hours / subscription / enabled channels) --------------


def _enable_ntfy(conn: sqlite3.Connection) -> None:
    cfg = notify.load_config(conn)
    cfg.ntfy.enabled = True
    cfg.ntfy.topic = "pd-test-topic"
    notify.save_config(conn, cfg, now=GOLDEN_NOW)


def _protect(conn: sqlite3.Connection) -> None:
    """Add an auth user so ``is_protected(conn)`` → True.

    The golden DB is guest by default (empty auth tables); the FU-D4 guest gate suppresses
    the digest push in guest mode, so the dispatch-path tests must run PROTECTED to exercise
    the enabled/subscribed/quiet-hours gates.
    """
    create_auth_tables(conn)  # idempotent — golden_db already created these
    create_user(conn, name="Owner", username="owner", password="password123", now=GOLDEN_NOW)


def test_push_dispatches_when_subscribed(golden_db: sqlite3.Connection) -> None:
    _protect(golden_db)
    _enable_ntfy(golden_db)
    calls: list[tuple[str, str]] = []

    def fake_sender(channels, title, body, severity, link):  # type: ignore[no-untyped-def]
        calls.append((title, body))
        return {ch.name: "ok" for ch in channels}

    summary = ds.run_digest_daily(golden_db, now=GOLDEN_NOW, sender=fake_sender)
    assert calls, "the digest push must fire when a channel is enabled + subscribed"
    assert "推播" in summary
    assert not _AMOUNT_RE.search(calls[0][1])  # dispatched body carries no amount


def test_push_links_to_dashboard_when_base_configured(golden_db: sqlite3.Connection) -> None:
    # FU-D17: a configured public base URL → the daily push links to the dashboard and the
    # 「開啟儀表板查看」 hint is dropped from the body.
    _protect(golden_db)
    _enable_ntfy(golden_db)
    cfg = notify.load_config(golden_db)
    cfg.public_base_url = "https://invest.example.com"
    notify.save_config(golden_db, cfg, now=GOLDEN_NOW)
    captured: dict[str, str | None] = {}

    def fake_sender(channels, title, body, severity, link):  # type: ignore[no-untyped-def]
        captured["link"] = link
        captured["body"] = body
        return {ch.name: "ok" for ch in channels}

    ds.run_digest_daily(golden_db, now=GOLDEN_NOW, sender=fake_sender)
    assert captured["link"] == "https://invest.example.com/index.html"
    assert "開啟儀表板查看" not in (captured["body"] or "")


def test_push_suppressed_in_guest_mode(golden_db: sqlite3.Connection) -> None:
    # FU-D4: golden_db is guest (no auth users). Generation is open, but the outbound push
    # is suppressed — the injected sender must NOT be called, yet the digest is still stored.
    _enable_ntfy(golden_db)  # channel enabled + subscribed — only guest mode blocks dispatch
    calls: list[str] = []

    def fake_sender(channels, title, body, severity, link):  # type: ignore[no-untyped-def]
        calls.append(title)
        return {ch.name: "ok" for ch in channels}

    summary = ds.run_digest_daily(golden_db, now=GOLDEN_NOW, sender=fake_sender)
    assert not calls, "guest-mode digest must NOT dispatch outbound push"
    assert "示範模式略過推播" in summary
    assert digest_store.get_latest(golden_db, "daily") is not None  # generation + cache open


def test_push_skips_in_quiet_hours(golden_db: sqlite3.Connection) -> None:
    _protect(golden_db)
    _enable_ntfy(golden_db)
    cfg = notify.load_config(golden_db)
    # GOLDEN_NOW is 14:30 Taipei → put a window around it.
    cfg.quiet_hours = notify.QuietHours(enabled=True, start="14:00", end="16:00")
    notify.save_config(golden_db, cfg, now=GOLDEN_NOW)
    calls: list[str] = []

    def fake_sender(channels, title, body, severity, link):  # type: ignore[no-untyped-def]
        calls.append(title)
        return {}

    summary = ds.run_digest_daily(golden_db, now=GOLDEN_NOW, sender=fake_sender)
    assert not calls, "quiet hours must skip the push"
    assert "靜音時段略過推播" in summary
    # but the digest is still stored
    assert digest_store.get_latest(golden_db, "daily") is not None


def test_push_skips_when_unsubscribed(golden_db: sqlite3.Connection) -> None:
    _protect(golden_db)
    _enable_ntfy(golden_db)
    cfg = notify.load_config(golden_db)
    cfg.subscriptions["digest_daily"] = False
    notify.save_config(golden_db, cfg, now=GOLDEN_NOW)
    calls: list[str] = []

    def fake_sender(channels, title, body, severity, link):  # type: ignore[no-untyped-def]
        calls.append(title)
        return {}

    summary = ds.run_digest_daily(golden_db, now=GOLDEN_NOW, sender=fake_sender)
    assert not calls and "未訂閱" in summary


def test_scheduler_tables_present_for_digest(golden_db: sqlite3.Connection) -> None:
    # sanity: the golden base already carries job_runs (data_health / chores read it).
    create_scheduler_tables(golden_db)  # idempotent
    assert golden_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_runs'"
    ).fetchone() is not None
