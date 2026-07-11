"""Unit tests for the notify leaf module (WP 3B): channels, dispatch, config, formatting.

Fully hermetic — no sockets: every channel is driven through a fake transport / smtp
factory (monkeypatch-free, injected at construction). Covers payload construction, the
severity→priority map, fan-out isolation, secret-free exception text, message formatting,
quiet-hours (incl. midnight wrap), the config defaults (one-time random topic persisted
once), and a catalog-drift guard against the backend rule ids.
"""

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.ops import notify
from portfolio_dash.strategy.rules_config import RULE_IDS
from portfolio_dash.strategy.signal_states import EVENT_CROSS, EVENT_MOMENTUM, EVENT_TREND

TAIPEI = ZoneInfo("Asia/Taipei")


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _Transport:
    """Captures the last POST; optionally raises to simulate a transport failure."""

    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._resp = resp or _Resp()
        self._exc = exc

    def post(
        self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None
    ) -> _Resp:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return self._resp


# --- ntfy ---------------------------------------------------------------------


def test_ntfy_payload_json_endpoint_and_priority() -> None:
    t = _Transport()
    ch = notify.NtfyChannel("https://ntfy.sh/", "pd-secret", token="tok-123456789", transport=t)
    ch.send("單一標的集中度", "2330：觸發預警", "risk", None)
    call = t.calls[0]
    assert call["url"] == "https://ntfy.sh"  # trailing slash trimmed, topic NOT in URL
    assert call["json"]["topic"] == "pd-secret"
    assert call["json"]["title"] == "單一標的集中度"  # zh in the UTF-8 JSON body, not a header
    assert call["json"]["message"] == "2330：觸發預警"
    assert call["json"]["priority"] == 4  # risk -> high
    assert call["headers"]["Authorization"] == "Bearer tok-123456789"


@pytest.mark.parametrize(
    ("severity", "priority"), [("risk", 4), ("warn", 3), ("info", 2), ("other", 3)]
)
def test_ntfy_severity_priority_map(severity: str, priority: int) -> None:
    t = _Transport()
    notify.NtfyChannel("https://ntfy.sh", "pd-x", transport=t).send("t", "b", severity, None)
    assert t.calls[0]["json"]["priority"] == priority


def test_ntfy_no_token_no_auth_header() -> None:
    t = _Transport()
    notify.NtfyChannel("https://ntfy.sh", "pd-x", transport=t).send("t", "b", "info", None)
    assert "Authorization" not in t.calls[0]["headers"]


# --- telegram (token redaction) -----------------------------------------------


def test_telegram_payload_plain_text_no_parse_mode() -> None:
    t = _Transport()
    ch = notify.TelegramChannel("11:AAA", "9988", transport=t)
    ch.send("title", "body ＞ 30%", "warn", None)
    call = t.calls[0]
    assert call["url"] == "https://api.telegram.org/bot11:AAA/sendMessage"
    assert call["json"] == {"chat_id": "9988", "text": "title\nbody ＞ 30%"}
    assert "parse_mode" not in call["json"]  # never Markdown -> no injection / break


def test_telegram_error_is_secret_free() -> None:
    token = "123456:SECRETTOKENVALUE"
    exc = RuntimeError(f"401 Client Error for url https://api.telegram.org/bot{token}/sendMessage")
    ch = notify.TelegramChannel(token, "9988", transport=_Transport(exc=exc))
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    assert token not in str(ei.value)
    assert "***" in str(ei.value)


# --- email --------------------------------------------------------------------


class _FakeSMTP:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.messages: list[Any] = []

    def starttls(self) -> None:
        self.events.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.events.append(f"login:{user}")

    def send_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def quit(self) -> None:
        self.events.append("quit")


def test_email_builds_utf8_message_and_starttls() -> None:
    fake = _FakeSMTP()
    ch = notify.EmailChannel(
        host="smtp.x", port=587, tls="starttls", username="u", password="pw",
        from_addr="a@x", to_addr="b@x", smtp_factory=lambda: fake,
    )
    ch.send("測試主旨", "測試內容", "info", None)
    assert "starttls" in fake.events and "login:u" in fake.events and "quit" in fake.events
    msg = fake.messages[0]
    assert msg["Subject"] == "測試主旨"
    assert msg["From"] == "a@x" and msg["To"] == "b@x"
    assert "測試內容" in msg.get_content()


def test_email_error_redacts_password() -> None:
    def _boom() -> Any:
        raise RuntimeError("SMTP auth failed for password s3cr3t-pass")

    ch = notify.EmailChannel(
        host="smtp.x", port=587, tls="starttls", username="u", password="s3cr3t-pass",
        from_addr="a@x", to_addr="b@x", smtp_factory=_boom,
    )
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    assert "s3cr3t-pass" not in str(ei.value)


# --- dispatch fan-out isolation ----------------------------------------------


class _FakeChannel:
    def __init__(self, name: str, exc: Exception | None = None) -> None:
        self.name = name
        self._exc = exc
        self.sent: list[tuple[str, str, str, str | None]] = []

    def send(self, title: str, body: str, severity: str, link: str | None) -> None:
        self.sent.append((title, body, severity, link))
        if self._exc is not None:
            raise self._exc


def test_dispatch_isolates_a_failing_channel() -> None:
    a = _FakeChannel("a", exc=notify.NotifyError("boom ***"))
    b = _FakeChannel("b")
    out = notify.dispatch([a, b], "t", "body", "risk", None)
    assert out["a"].startswith("error:") and out["b"] == "ok"
    assert b.sent, "channel B must still be called after channel A raised"


def test_dispatch_never_raises_and_returns_all_names() -> None:
    a = _FakeChannel("a", exc=RuntimeError("x"))
    out = notify.dispatch([a], "t", "b", "info", None)
    assert set(out) == {"a"}


# --- message formatting -------------------------------------------------------


def test_format_event_with_symbol() -> None:
    title, body, sev = notify.format_event("single_weight", "2330")
    assert "2330" in title and "單一標的集中度" in title
    assert "2330" in body and "單一標的集中度" in body
    assert sev == "risk"


def test_format_event_global_and_unknown() -> None:
    title, _, sev = notify.format_event("quota_low", None)
    assert "AI 額度偏低" in title and sev == "warn"
    t2, _, sev2 = notify.format_event("mystery_rule", None)
    assert "mystery_rule" in t2 and sev2 == "info"  # honest fallback, never crash


# --- quiet hours --------------------------------------------------------------


def _now(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 7, 12, hh, mm, tzinfo=TAIPEI)


def test_quiet_hours_wrap_midnight() -> None:
    q = notify.QuietHours(enabled=True, start="22:00", end="08:00")
    assert notify.in_quiet_hours(q, _now(23)) is True
    assert notify.in_quiet_hours(q, _now(2)) is True
    assert notify.in_quiet_hours(q, _now(12)) is False


def test_quiet_hours_same_day_window_and_disabled() -> None:
    q = notify.QuietHours(enabled=True, start="09:00", end="17:00")
    assert notify.in_quiet_hours(q, _now(10)) is True
    assert notify.in_quiet_hours(q, _now(20)) is False
    assert notify.in_quiet_hours(notify.QuietHours(enabled=False), _now(23)) is False
    # malformed / zero-length window -> fail-open (never silently swallow everything)
    bad = notify.QuietHours(enabled=True, start="x", end="y")
    zero = notify.QuietHours(enabled=True, start="08:00", end="08:00")
    assert notify.in_quiet_hours(bad, _now(23)) is False
    assert notify.in_quiet_hours(zero, _now(8)) is False


# --- config defaults / one-time topic -----------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_config_defaults_topic_generated_once(conn: sqlite3.Connection) -> None:
    cfg = notify.load_config(conn)
    assert cfg.ntfy.topic.startswith("pd-") and len(cfg.ntfy.topic) > 10
    assert cfg.ntfy.enabled is False and cfg.telegram.enabled is False
    # every catalog rule defaults subscribed (incl. signal_*)
    assert all(cfg.subscriptions[rid] for rid, _, _ in notify.RULE_CATALOG)
    # topic is stable across reloads (generated once, persisted)
    assert notify.load_config(conn).ntfy.topic == cfg.ntfy.topic


def test_save_round_trip(conn: sqlite3.Connection) -> None:
    cfg = notify.load_config(conn)
    cfg.telegram.enabled = True
    cfg.telegram.bot_token = "abc"
    notify.save_config(conn, cfg, now=_now(10))
    reloaded = notify.load_config(conn)
    assert reloaded.telegram.enabled is True and reloaded.telegram.bot_token == "abc"


def test_generate_topic_is_random() -> None:
    assert notify.generate_topic() != notify.generate_topic()


# --- catalog drift guard ------------------------------------------------------


def test_rule_catalog_covers_backend_rule_ids() -> None:
    catalog_ids = {rid for rid, _, _ in notify.RULE_CATALOG}
    assert set(RULE_IDS) <= catalog_ids, "a risk rule has no push label"
    signal_ids = {EVENT_TREND, EVENT_CROSS, EVENT_MOMENTUM}
    assert signal_ids <= catalog_ids, "a signal event has no push label"
