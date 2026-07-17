"""Notification-settings API (Blueprint Phase 3 · WP 3B): channel config + test-send.

Thin router over ``ops.notify``: it reads/writes the single-row ``notify_config`` and
serializes a MASKED view (ntfy token · telegram bot_token · email password never leave the
server in the clear — mirrors the LLM-key convention exactly, delegating to
``shared.masking``). The write path is placeholder-preserving: a secret field still holding
the returned mask (contains ``•••``) means "unchanged", an empty string clears it, anything
else sets a new value. In PROTECTED mode the ntfy TOPIC is returned in full on purpose —
the user must copy it to subscribe on their phone (it is the read secret, flagged as such
in the UI).

Guest-mode lockdown (security review F1): when the instance has NO auth users (the public
demo), the notify surface is an outbound-request primitive any visitor could aim — so
``PUT /notify/config`` and ``POST /notify/test`` return **403**, and ``GET`` masks the ntfy
topic (``topic_masked`` + ``topic_set``, never the full read secret). In protected mode
everything behaves as before (owner-only via the global session gate).

``POST /notify/test`` fires ONE channel using the SAVED config (off the event loop, like
the datasources probe) and reports that channel's per-send outcome. No business logic and
no money here; the actual send lives in ``ops.notify``.
"""

import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import auth_store
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.ops import notify
from portfolio_dash.shared.masking import mask_secret

router = APIRouter()

_CHANNELS = ("ntfy", "telegram", "email")
_TLS_MODES = ("starttls", "ssl", "none")
_MASK_MARK = "•••"

# Characters that must never appear in a bare SMTP host (path/userinfo/port smuggling —
# the port has its own field). Security review F1.
_HOST_FORBIDDEN = ("/", "@", ":")


# --- serialization (masked) ---------------------------------------------------


def _masked_wire(cfg: notify.NotifyConfig, *, guest: bool = False) -> dict[str, Any]:
    """The GET/PUT response: secrets masked, rule catalog for the UI.

    ``guest=True`` (public demo) replaces the full ntfy topic with ``topic_masked`` +
    ``topic_set`` — the topic is the READ secret, so a demo visitor never sees it. In
    protected mode it is shown in full so the owner can copy it to the phone.
    """
    ntfy_wire: dict[str, Any] = {
        "enabled": cfg.ntfy.enabled,
        "server": cfg.ntfy.server,
        "token_masked": mask_secret(cfg.ntfy.token),
        "token_set": bool(cfg.ntfy.token),
    }
    if guest:
        ntfy_wire["topic_masked"] = mask_secret(cfg.ntfy.topic)
        ntfy_wire["topic_set"] = bool(cfg.ntfy.topic)
    else:
        ntfy_wire["topic"] = cfg.ntfy.topic  # the READ secret — shown so it can be copied
    return {
        "ntfy": ntfy_wire,
        "telegram": {
            "enabled": cfg.telegram.enabled,
            "bot_token_masked": mask_secret(cfg.telegram.bot_token),
            "bot_token_set": bool(cfg.telegram.bot_token),
            "chat_id": cfg.telegram.chat_id,
        },
        "email": {
            "enabled": cfg.email.enabled,
            "host": cfg.email.host,
            "port": cfg.email.port,
            "tls": cfg.email.tls,
            "username": cfg.email.username,
            "password_masked": mask_secret(cfg.email.password),
            "password_set": bool(cfg.email.password),
            "from_addr": cfg.email.from_addr,
            "to_addr": cfg.email.to_addr,
        },
        "quiet_hours": {
            "enabled": cfg.quiet_hours.enabled,
            "start": cfg.quiet_hours.start,
            "end": cfg.quiet_hours.end,
        },
        "subscriptions": dict(cfg.subscriptions),
        # FU-D17: the public base URL is NOT a secret (it is the site's own address), so it
        # is returned in full on both protected and guest reads — the frontend needs it to
        # populate the deep-link field.
        "public_base_url": cfg.public_base_url,
        "rule_catalog": [
            {"id": rid, "label": label, "severity": sev}
            for rid, label, sev in notify.RULE_CATALOG
        ],
    }


@router.get("/notify/config")
def get_notify_config(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _masked_wire(
        notify.load_config(conn), guest=not auth_store.is_protected(conn)
    )


# --- write (placeholder-preserving) -------------------------------------------


class NtfyIn(BaseModel):
    enabled: bool | None = None
    server: str | None = None
    topic: str | None = None
    token: str | None = None


class TelegramIn(BaseModel):
    enabled: bool | None = None
    bot_token: str | None = None
    chat_id: str | None = None


class EmailIn(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = None
    tls: str | None = None
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    to_addr: str | None = None


class QuietIn(BaseModel):
    enabled: bool | None = None
    start: str | None = None
    end: str | None = None


class NotifyConfigIn(BaseModel):
    ntfy: NtfyIn | None = None
    telegram: TelegramIn | None = None
    email: EmailIn | None = None
    quiet_hours: QuietIn | None = None
    subscriptions: dict[str, bool] | None = None
    public_base_url: str | None = None  # FU-D17 (not a secret)


def _resolve_secret(incoming: str | None, existing: str) -> str:
    """Placeholder-preserving secret update: mask → keep, ``""`` → clear, else → set."""
    if incoming is None or _MASK_MARK in incoming:
        return existing
    return incoming


def _bad(msg: str, field: str) -> JSONResponse:
    return JSONResponse(
        status_code=400, content=error_body("validation_error", msg, field=field)
    )


def _guest_forbidden() -> JSONResponse:
    """403 for notify WRITES in guest mode (security review F1 — SSRF lockdown)."""
    return JSONResponse(
        status_code=403,
        content=error_body(
            "forbidden", "通知設定僅於受保護模式開放（示範站不開放通知設定）"
        ),
    )


def _valid_smtp_host(value: str) -> bool:
    """A bare SMTP host: non-empty, no whitespace, no ``/`` ``@`` ``:`` (F1 hygiene)."""
    stripped = value.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    return not any(ch in value for ch in _HOST_FORBIDDEN)


def _valid_hm(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return False
    hh, mm = int(parts[0]), int(parts[1])
    return 0 <= hh < 24 and 0 <= mm < 60


@router.put("/notify/config")
def put_notify_config(
    body: NotifyConfigIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Merge a partial update into the saved config (validate shapes; 400 on junk).

    Guest mode (no auth users — the public demo) → 403: the notify config is an
    outbound-request primitive (SSRF surface), so only protected mode may write it.
    """
    if not auth_store.is_protected(conn):
        return _guest_forbidden()
    cfg = notify.load_config(conn)

    if body.ntfy is not None:
        n = body.ntfy
        if n.server is not None and n.server:
            if not n.server.startswith(("http://", "https://")):
                return _bad("ntfy 伺服器需為 http(s) 網址", "ntfy.server")
            try:
                has_userinfo = urlsplit(n.server).username is not None
            except ValueError:
                return _bad("ntfy 伺服器網址無法解析", "ntfy.server")
            if has_userinfo:
                # userinfo smuggling (https://user@evil/) re-aims the POST — reject (F1).
                return _bad("ntfy 伺服器網址不可包含帳號資訊（@）", "ntfy.server")
        if n.enabled is not None:
            cfg.ntfy.enabled = n.enabled
        if n.server is not None:
            cfg.ntfy.server = n.server or "https://ntfy.sh"
        if n.topic is not None and n.topic:
            cfg.ntfy.topic = n.topic  # topic is not secret-masked; only set when non-empty
        cfg.ntfy.token = _resolve_secret(n.token, cfg.ntfy.token)

    if body.telegram is not None:
        t = body.telegram
        if t.enabled is not None:
            cfg.telegram.enabled = t.enabled
        if t.chat_id is not None:
            cfg.telegram.chat_id = t.chat_id
        cfg.telegram.bot_token = _resolve_secret(t.bot_token, cfg.telegram.bot_token)

    if body.email is not None:
        e = body.email
        if e.tls is not None and e.tls not in _TLS_MODES:
            return _bad("email TLS 需為 starttls / ssl / none", "email.tls")
        if e.port is not None and not (1 <= e.port <= 65535):
            return _bad("email 連接埠需在 1–65535", "email.port")
        if e.host is not None and not _valid_smtp_host(e.host):
            # host ONLY (port has its own field): no empty/whitespace, no / @ : (F1).
            return _bad("email 主機需為純主機名（不可含 / @ : 或空白）", "email.host")
        if e.enabled is not None:
            cfg.email.enabled = e.enabled
        if e.host is not None:
            cfg.email.host = e.host
        if e.port is not None:
            cfg.email.port = e.port
        if e.tls is not None:
            cfg.email.tls = e.tls
        if e.username is not None:
            cfg.email.username = e.username
        if e.from_addr is not None:
            cfg.email.from_addr = e.from_addr
        if e.to_addr is not None:
            cfg.email.to_addr = e.to_addr
        cfg.email.password = _resolve_secret(e.password, cfg.email.password)

    if body.quiet_hours is not None:
        q = body.quiet_hours
        if q.start is not None and not _valid_hm(q.start):
            return _bad("靜音起始需為 HH:MM", "quiet_hours.start")
        if q.end is not None and not _valid_hm(q.end):
            return _bad("靜音結束需為 HH:MM", "quiet_hours.end")
        if q.enabled is not None:
            cfg.quiet_hours.enabled = q.enabled
        if q.start is not None:
            cfg.quiet_hours.start = q.start
        if q.end is not None:
            cfg.quiet_hours.end = q.end

    if body.subscriptions is not None:
        known = {rid for rid, _, _ in notify.RULE_CATALOG}
        for rid, on in body.subscriptions.items():
            if rid in known:  # ignore junk keys; keep the map clean
                cfg.subscriptions[rid] = bool(on)

    if body.public_base_url is not None:
        normalized = notify.normalize_base_url(body.public_base_url)
        if normalized is None:  # neither empty nor http(s) → reject (never a bare host)
            return _bad("站台公開網址需為 http(s) 開頭，或留空", "public_base_url")
        cfg.public_base_url = normalized

    notify.save_config(conn, cfg, now=now)
    return _masked_wire(cfg)  # protected mode (guarded above) → full topic


# --- test-send ----------------------------------------------------------------


class TestIn(BaseModel):
    channel: str


@router.post("/notify/test")
async def test_notify(
    body: TestIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Send a test message via ONE channel using the saved config; report its outcome.

    Guest mode → 403 (same F1 lockdown as the write path: a demo visitor must not be
    able to fire outbound requests from this host).
    """
    if not auth_store.is_protected(conn):
        return _guest_forbidden()
    if body.channel not in _CHANNELS:
        return _bad(f"未知通道：{body.channel}", "channel")
    cfg = notify.load_config(conn)
    channel = notify.build_channel(cfg, body.channel)
    if channel is None:
        return {
            "channel": body.channel,
            "ok": False,
            "detail": "error: 尚未設定必要欄位",
        }
    # FU-D17: link the test message to the site when a public base URL is configured, so
    # the owner verifies the whole deep-link path (empty ⇒ None ⇒ legacy link-free test).
    link = cfg.public_base_url or None
    outcome = await run_in_threadpool(
        notify.dispatch,
        [channel],
        "portfolio-dash 測試通知",
        "這是一則測試訊息，收到代表通道設定正確。",
        "info",
        link,
    )
    result = outcome.get(body.channel, "error: unknown")
    return {"channel": body.channel, "ok": result == "ok", "detail": result}


__all__ = ["router"]
