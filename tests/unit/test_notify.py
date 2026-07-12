"""Unit tests for the notify leaf module (WP 3B): channels, dispatch, config, formatting.

Fully hermetic — no sockets: every channel is driven through a fake transport / smtp
factory (monkeypatch-free, injected at construction). Covers payload construction, the
severity→priority map, fan-out isolation, secret-free exception text, message formatting,
quiet-hours (incl. midnight wrap), the config defaults (one-time random topic persisted
once), and a catalog-drift guard against the backend rule ids.
"""

import smtplib
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
    def __init__(self, status: int = 200, body: Any = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Transport:
    """Captures the last POST; optionally raises to simulate a transport failure."""

    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._resp = resp or _Resp()
        self._exc = exc

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: Any = None,
        timeout: Any = None,
        allow_redirects: Any = "unset",
    ) -> _Resp:
        self.calls.append({
            "url": url, "json": json, "headers": headers, "timeout": timeout,
            "allow_redirects": allow_redirects,
        })
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


def test_ntfy_error_redacts_token_and_topic() -> None:
    # Mirrors the Telegram redaction test (security review F3.1): BOTH ntfy secrets —
    # the bearer token AND the topic (the read secret) — must never reach the message.
    token = "tok-SECRETVALUE-123"
    topic = "pd-topicsecret456"
    exc = RuntimeError(f"401 for https://evil/{topic} with Authorization: Bearer {token}")
    ch = notify.NtfyChannel("https://ntfy.sh", topic, token=token, transport=_Transport(exc=exc))
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    assert token not in str(ei.value)
    assert topic not in str(ei.value)
    assert "***" in str(ei.value)


def test_ntfy_never_follows_redirects() -> None:
    # F1.3: the server URL is user config; a redirect could re-aim topic+token.
    t = _Transport()
    notify.NtfyChannel("https://ntfy.sh", "pd-x", transport=t).send("t", "b", "info", None)
    assert t.calls[0]["allow_redirects"] is False


def test_ntfy_3xx_is_a_failure() -> None:
    t = _Transport(resp=_Resp(status=302))
    ch = notify.NtfyChannel("https://ntfy.sh", "pd-x", transport=t)
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    assert "redirect" in str(ei.value)


def test_ntfy_and_telegram_pass_a_timeout() -> None:
    # F3.3: a hung provider must never hang the scheduler thread — every HTTP send
    # carries a finite timeout.
    t1 = _Transport()
    notify.NtfyChannel("https://ntfy.sh", "pd-x", transport=t1).send("t", "b", "info", None)
    assert t1.calls[0]["timeout"] is not None
    t2 = _Transport()
    notify.TelegramChannel("11:AAA", "9988", transport=t2).send("t", "b", "info", None)
    assert t2.calls[0]["timeout"] is not None


# --- telegram (token redaction) -----------------------------------------------


def test_telegram_payload_plain_text_no_parse_mode() -> None:
    t = _Transport()
    ch = notify.TelegramChannel("11:AAA", "9988", transport=t)
    ch.send("title", "body ＞ 30%", "warn", None)
    call = t.calls[0]
    assert call["url"] == "https://api.telegram.org/bot11:AAA/sendMessage"
    assert call["json"] == {"chat_id": "9988", "text": "title\nbody ＞ 30%"}
    assert "parse_mode" not in call["json"]  # never Markdown -> no injection / break


def test_telegram_http_error_surfaces_description_and_stays_secret_free() -> None:
    # Field report 2026-07-12: a bare "400 Bad Request" hid Telegram's actionable
    # reason ("chat not found" = bot never /start-ed or wrong chat_id). The response
    # body's description must surface; the token must stay redacted. chat_id is
    # trimmed before sending.
    token = "123456:SECRETTOKENVALUE"
    t = _Transport(resp=_Resp(400, body={
        "ok": False, "error_code": 400, "description": "Bad Request: chat not found",
    }))
    ch = notify.TelegramChannel(token, " 9988 ", transport=t)
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    msg = str(ei.value)
    assert "chat not found" in msg
    assert token not in msg
    assert t.calls[0]["json"]["chat_id"] == "9988"  # trimmed


def test_ntfy_http_error_surfaces_body_reason() -> None:
    t = _Transport(resp=_Resp(403, body={"error": "forbidden"}))
    ch = notify.NtfyChannel("https://ntfy.sh", "pd-x", "tok-secret", transport=t)
    with pytest.raises(notify.NotifyError) as ei:
        ch.send("t", "b", "info", None)
    msg = str(ei.value)
    assert "forbidden" in msg
    assert "tok-secret" not in msg


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


@pytest.mark.parametrize("tls", ["none", "ssl"])
def test_smtp_constructor_receives_timeout(
    monkeypatch: pytest.MonkeyPatch, tls: str
) -> None:
    # F3.3: without an injected factory, the REAL smtplib constructor path must carry a
    # finite timeout (a hung SMTP server must never hang the scheduler thread). Patching
    # the stdlib module patches the SAME object ``ops.notify`` imported.
    captured: dict[str, Any] = {}

    class _CapturingSMTP(_FakeSMTP):
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            super().__init__()
            captured["host"] = host
            captured["timeout"] = timeout

    monkeypatch.setattr(smtplib, "SMTP", _CapturingSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _CapturingSMTP)
    ch = notify.EmailChannel(
        host="smtp.x", port=465, tls=tls, username="", password="",
        from_addr="a@x", to_addr="b@x",
    )
    ch.send("t", "b", "info", None)
    assert captured["host"] == "smtp.x"
    assert captured["timeout"] is not None


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
