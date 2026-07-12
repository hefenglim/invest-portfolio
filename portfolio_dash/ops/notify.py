"""Multi-channel push notifications (Blueprint Phase 3 · WP 3B).

A leaf ops module: it imports ONLY stdlib + ``requests`` + ``portfolio_dash.shared``
(``config_store`` for the single-row config, ``clock`` for the auto-fix stamp). It never
imports ``portfolio``/``pricing``/``strategy``/``llm_insight``/``api``/``scheduler``
(architecture.md — ops is a leaf above shared; higher layers call in, never the reverse).

Three delivery channels, each a small object satisfying :class:`NotifyChannel`:

- **ntfy** — HTTP POST to a self-hostable pub/sub server (default ``https://ntfy.sh``).
  Publishes via the JSON endpoint (``POST {server}`` with ``{"topic","title","message",
  "priority"}``) so a Traditional-Chinese title travels in a UTF-8 JSON body, never an
  RFC2047-encoded header. The **topic string IS the secret** (anyone who knows it can read
  the feed); an optional access token authenticates protected topics via ``Authorization:
  Bearer``.
- **Telegram** — ``POST https://api.telegram.org/bot{token}/sendMessage`` with a PLAIN-text
  body (no ``parse_mode`` → symbol names / ``＞`` etc. never break Markdown parsing or open
  an injection surface). The bot token sits in the URL, so every raised exception is
  redacted (:func:`_redact`) before it can reach a log or an API response.
- **Email** — stdlib ``smtplib`` + ``email.message.EmailMessage`` (ZERO new dependencies),
  STARTTLS or implicit SSL by config, UTF-8 subject + body.

:func:`dispatch` fans one message out to every supplied channel and NEVER raises: a single
channel's failure is caught, redacted, logged, and recorded in the returned per-channel
outcome dict so the other channels — and the scheduler — are unaffected.

Secrets (ntfy token · telegram bot_token · email password) are stored raw in the DB and
returned MASKED on the settings read path (the api router masks via ``shared.masking``);
they are NEVER logged and NEVER embedded in an exception message.
"""

import contextlib
import logging
import secrets
import smtplib
import sqlite3
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import requests
from pydantic import BaseModel, Field

from portfolio_dash.shared import config_store
from portfolio_dash.shared.clock import app_now

logger = logging.getLogger(__name__)

_APP_TZ = ZoneInfo("Asia/Taipei")

# ntfy priority levels (1 min .. 5 max). severity -> priority per owner decision:
# risk -> high(4), warn -> default(3), info -> low(2).
_NTFY_PRIORITY: dict[str, int] = {"risk": 4, "warn": 3, "info": 2}

# The alert/​signal rule catalog for the NOTIFICATION surface: (rule_id, zh label, severity).
# This is the single Python source of the push-message labels + subscription defaults; the
# ids MIRROR strategy.rules_config RULE_META + strategy.signal_states EVENT_* and the zh
# labels mirror web/settings-alerts.js META + web/detail.js. A drift test
# (tests/unit/test_notify.py) asserts this catalog covers every backend rule id so a new
# rule can never silently ship without a push label.
RULE_CATALOG: list[tuple[str, str, str]] = [
    ("single_weight", "單一標的集中度", "risk"),
    ("sector_weight", "產業集中度", "risk"),
    ("stale_price", "價格過期", "warn"),
    ("missing_price", "缺價", "warn"),
    ("fx_drift", "匯率漂移", "info"),
    ("exdiv_upcoming", "即將除息", "info"),
    ("quota_low", "AI 額度偏低", "warn"),
    ("calib_gap", "AI 校準誤差", "warn"),
    ("signal_trend", "趨勢反轉", "info"),
    ("signal_cross", "均線交叉", "info"),
    ("signal_momentum", "動能轉向", "info"),
]
_RULE_LABEL: dict[str, tuple[str, str]] = {rid: (label, sev) for rid, label, sev in RULE_CATALOG}


class NotifyError(Exception):
    """A channel-send failure carrying a SECRET-FREE message (already redacted)."""


def _redact(text: str, *secret_values: str) -> str:
    """Replace every non-empty secret substring in *text* with ``***`` (defense in depth)."""
    out = text
    for value in secret_values:
        if value:
            out = out.replace(value, "***")
    return out


# --- channel protocol + implementations ---------------------------------------


@runtime_checkable
class NotifyChannel(Protocol):
    """A delivery channel. ``send`` RAISES on failure; :func:`dispatch` isolates it."""

    name: str

    def send(self, title: str, body: str, severity: str, link: str | None) -> None: ...


class NtfyChannel:
    """ntfy publisher (JSON endpoint; UTF-8 safe for zh titles; topic = the secret)."""

    name = "ntfy"

    def __init__(
        self,
        server: str,
        topic: str,
        token: str = "",
        *,
        timeout: float = 10.0,
        transport: Any = None,
    ) -> None:
        self.server = server
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self._transport: Any = transport if transport is not None else requests

    def send(self, title: str, body: str, severity: str, link: str | None) -> None:
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": title,
            "message": body,
            "priority": _NTFY_PRIORITY.get(severity, 3),
        }
        if link:
            payload["click"] = link
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            # allow_redirects=False (security review F1): the server URL is user config,
            # so a redirect could re-aim the POST (topic + token) at an arbitrary host.
            # Never follow; a 3xx is a hard failure (raise_for_status passes 3xx).
            resp = self._transport.post(
                self.server.rstrip("/"), json=payload, headers=headers,
                timeout=self.timeout, allow_redirects=False,
            )
            if 300 <= int(resp.status_code) < 400:
                raise NotifyError(f"unexpected redirect (HTTP {resp.status_code})")
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - normalize to a redacted NotifyError
            raise NotifyError(_redact(str(exc), self.token, self.topic)) from None


class TelegramChannel:
    """Telegram bot sender (plain text, no parse_mode; bot token redacted on error)."""

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
        transport: Any = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._transport: Any = transport if transport is not None else requests

    def send(self, title: str, body: str, severity: str, link: str | None) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        text = f"{title}\n{body}"
        if link:
            text = f"{text}\n{link}"
        try:
            resp = self._transport.post(
                url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - redact the token out of the URL/message
            raise NotifyError(_redact(str(exc), self.bot_token)) from None


class EmailChannel:
    """stdlib SMTP sender (STARTTLS / SSL / plain by config; UTF-8; password redacted)."""

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        tls: str,
        username: str,
        password: str,
        from_addr: str,
        to_addr: str,
        timeout: float = 15.0,
        smtp_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.timeout = timeout
        self._smtp_factory = smtp_factory

    def _open(self) -> Any:
        if self._smtp_factory is not None:
            return self._smtp_factory()
        if self.tls == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, title: str, body: str, severity: str, link: str | None) -> None:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg.set_content(f"{body}\n\n{link}" if link else body)
        try:
            client = self._open()
            try:
                if self.tls == "starttls":
                    client.starttls()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(msg)
            finally:
                with contextlib.suppress(Exception):
                    client.quit()
        except Exception as exc:  # noqa: BLE001 - redact the password out of the message
            raise NotifyError(_redact(str(exc), self.password)) from None


# A sender: ``fn(channels, title, body, severity, link) -> {channel_name: outcome}``.
Sender = Callable[[list[NotifyChannel], str, str, str, str | None], dict[str, str]]


def dispatch(
    channels: list[NotifyChannel],
    title: str,
    body: str,
    severity: str,
    link: str | None = None,
) -> dict[str, str]:
    """Fan *one* message out to every channel; return ``{name: "ok" | "error: <msg>"}``.

    NEVER raises: each channel is tried independently and its failure is caught, logged,
    and recorded so the other channels — and any scheduler job calling this — are isolated
    from a single channel being down. Error text is already secret-free (channels redact).
    """
    outcome: dict[str, str] = {}
    for channel in channels:
        try:
            channel.send(title, body, severity, link)
            outcome[channel.name] = "ok"
        except Exception as exc:  # noqa: BLE001 - isolate per channel; never propagate
            msg = str(exc)[:200] or exc.__class__.__name__
            outcome[channel.name] = f"error: {msg}"
            logger.warning("notify channel %s failed: %s", channel.name, msg)
    return outcome


# --- message formatting (pure) ------------------------------------------------


def format_event(rule_id: str, symbol: str | None) -> tuple[str, str, str]:
    """A fired alert event → ``(title, body, severity)`` in zh-TW (deterministic, pure).

    Uses only the rule_id + symbol carried by the ``alert_events`` row — NO account
    balances or amounts. An unknown rule id degrades to ``(rule_id, info)`` (never crashes).
    """
    label, severity = _RULE_LABEL.get(rule_id, (rule_id, "info"))
    if symbol:
        title = f"portfolio-dash · {symbol} {label}"
        body = f"{symbol}：觸發「{label}」預警，請至儀表板查看詳情。"
    else:
        title = f"portfolio-dash · {label}"
        body = f"觸發「{label}」預警，請至儀表板查看詳情。"
    return title, body, severity


# --- config model + persistence (single-row JSON in config_store) -------------


class NtfyConfig(BaseModel):
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""  # secret (optional access token for protected topics)


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""  # secret
    chat_id: str = ""


class EmailConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    tls: str = "starttls"  # "starttls" | "ssl" | "none"
    username: str = ""
    password: str = ""  # secret
    from_addr: str = ""
    to_addr: str = ""


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "22:00"
    end: str = "08:00"


class NotifyConfig(BaseModel):
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    subscriptions: dict[str, bool] = Field(default_factory=dict)


_CATEGORY = "notify"
_SEED_AT = datetime(2026, 7, 12)
_DDL = (
    "CREATE TABLE IF NOT EXISTS notify_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


def generate_topic() -> str:
    """A fresh ntfy topic — a long random string that doubles AS the subscription secret."""
    return f"pd-{secrets.token_hex(12)}"


def _default_config() -> NotifyConfig:
    """The out-of-the-box config: all channels off, a one-time random topic, all rules on."""
    return NotifyConfig(
        ntfy=NtfyConfig(topic=generate_topic()),
        subscriptions={rid: True for rid, _, _ in RULE_CATALOG},
    )


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO notify_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (_default_config().model_dump_json(), _SEED_AT.isoformat()),
    )


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always) and seed the default config once."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def _persist(conn: sqlite3.Connection, cfg: NotifyConfig, *, now: datetime) -> None:
    conn.execute(
        "INSERT INTO notify_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (cfg.model_dump_json(), now.isoformat()),
    )
    conn.commit()


def load_config(conn: sqlite3.Connection) -> NotifyConfig:
    """Return the persisted :class:`NotifyConfig`, self-healing a missing topic / rules.

    The topic is generated ONCE (at seed) and persisted; a legacy/hand-edited row with an
    empty topic is back-filled here, and any rule newly added to :data:`RULE_CATALOG` is
    defaulted to subscribed — both persisted so the fix happens exactly once.
    """
    ensure_seeded(conn)
    row = conn.execute("SELECT body FROM notify_config WHERE id = 1").fetchone()
    if row is None:
        cfg = _default_config()
        _persist(conn, cfg, now=app_now())
        return cfg
    cfg = NotifyConfig.model_validate_json(row["body"])
    changed = False
    if not cfg.ntfy.topic:
        cfg.ntfy.topic = generate_topic()
        changed = True
    for rid, _, _ in RULE_CATALOG:
        if rid not in cfg.subscriptions:
            cfg.subscriptions[rid] = True
            changed = True
    if changed:
        _persist(conn, cfg, now=app_now())
    return cfg


def save_config(conn: sqlite3.Connection, cfg: NotifyConfig, *, now: datetime) -> NotifyConfig:
    """Persist *cfg* (caller has already validated + resolved masked secrets). Returns it."""
    ensure_seeded(conn)
    _persist(conn, cfg, now=now)
    return cfg


# --- channel construction + quiet-hours + subscription helpers ----------------


def build_channel(cfg: NotifyConfig, name: str) -> NotifyChannel | None:
    """Build ONE channel from saved config (ignoring ``enabled`` — for the test-send path).

    Returns ``None`` when the channel's essential fields are not configured, so the caller
    can report a clear "not configured" outcome instead of firing an empty request.
    """
    if name == "ntfy":
        if not cfg.ntfy.topic:
            return None
        return NtfyChannel(cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token)
    if name == "telegram":
        if not (cfg.telegram.bot_token and cfg.telegram.chat_id):
            return None
        return TelegramChannel(cfg.telegram.bot_token, cfg.telegram.chat_id)
    if name == "email":
        if not (cfg.email.host and cfg.email.from_addr and cfg.email.to_addr):
            return None
        return EmailChannel(
            host=cfg.email.host, port=cfg.email.port, tls=cfg.email.tls,
            username=cfg.email.username, password=cfg.email.password,
            from_addr=cfg.email.from_addr, to_addr=cfg.email.to_addr,
        )
    return None


def build_enabled_channels(cfg: NotifyConfig) -> list[NotifyChannel]:
    """Every channel whose ``enabled`` flag is set AND whose essential fields are present."""
    channels: list[NotifyChannel] = []
    if cfg.ntfy.enabled:
        ch = build_channel(cfg, "ntfy")
        if ch is not None:
            channels.append(ch)
    if cfg.telegram.enabled:
        ch = build_channel(cfg, "telegram")
        if ch is not None:
            channels.append(ch)
    if cfg.email.enabled:
        ch = build_channel(cfg, "email")
        if ch is not None:
            channels.append(ch)
    return channels


def _parse_hm(value: str) -> int | None:
    """``"HH:MM"`` → minutes-since-midnight, or ``None`` when malformed."""
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


def in_quiet_hours(quiet: QuietHours, now: datetime) -> bool:
    """True when *now* (evaluated in Asia/Taipei) falls inside the configured quiet window.

    Handles a window that wraps midnight (e.g. 22:00–08:00). A malformed or zero-length
    window is treated as NOT quiet (fail-open: never silently swallow every notification).
    """
    if not quiet.enabled:
        return False
    start = _parse_hm(quiet.start)
    end = _parse_hm(quiet.end)
    if start is None or end is None or start == end:
        return False
    local = now.astimezone(_APP_TZ)
    cur = local.hour * 60 + local.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # wraps midnight


__all__ = [
    "RULE_CATALOG",
    "EmailChannel",
    "EmailConfig",
    "NotifyChannel",
    "NotifyConfig",
    "NotifyError",
    "NtfyChannel",
    "NtfyConfig",
    "QuietHours",
    "Sender",
    "TelegramChannel",
    "TelegramConfig",
    "build_channel",
    "build_enabled_channels",
    "dispatch",
    "ensure_seeded",
    "format_event",
    "generate_topic",
    "in_quiet_hours",
    "load_config",
    "save_config",
]
