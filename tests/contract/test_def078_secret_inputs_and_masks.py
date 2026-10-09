"""DEF-078 (owner ruling ⑦, 2026-09-26): every secret is TYPED into a password field, and
every stored secret is SHOWN as the server's first-3 + ••• + last-3 mask — never more.

Ruling history: ⑦ read 「改密碼欄位＋前後 6 個 char 顯示明文」; the verifier's §15 reading
turned that into a 6+6 mask for API keys. The owner then ruled directly (2026-09-26) that ALL
keys keep the existing 3+3 mask (``shared/masking.py::mask_secret``) — so this file PINS the
mask rather than widening it, and a future 6+6 fails here.

Found on 3be67db: four secret inputs were plain ``type="text"`` — the model drawer's
``#dr-key``, the data-source 重設金鑰 dialog (``inp.type = 'text'``), ``#nt-ntfy-token`` and
``#nt-tg-token`` — so a key being typed (or pasted) was echoed in the clear.

Two halves:

1. **The masks, through the real doors** — a 40-char model key, a 40-char data-source key, the
   ntfy token, the Telegram bot token and the email password all read ``abc•••xyz``; a
   5-character secret reads ``•••``; no response body carries a full secret.
2. **The inputs, by scan** — every ``<input>`` in ``web/*.html`` and every input built in
   ``web/*.js`` whose id / label / placeholder names a secret is ``type="password"``. The set is
   DERIVED by the scan, not listed: a new secret field that forgets the type fails here.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.ops import notify
from tests.conftest import GOLDEN_NOW

_WEB = Path(__file__).resolve().parents[2] / "web"

_KEY40 = "sk-or-v1-0123456789abcdefghijklmnopqrstu"    # 40 characters
_DSKEY40 = "fm-9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeed"  # 40 characters
_SHORT = "ab12x"                                       # 5 characters


def _mask3(secret: str) -> str:
    return f"{secret[:3]}•••{secret[-3:]}"


def test_the_fixture_keys_are_the_lengths_the_ruling_is_about() -> None:
    assert len(_KEY40) == 40 and len(_DSKEY40) == 40 and len(_SHORT) == 5


# ------------------------------------------------------------------ 1. the masks


@pytest.mark.parametrize(("key", "expected"), [(_KEY40, _mask3(_KEY40)), (_SHORT, "•••")])
def test_a_model_key_is_masked_3_plus_3(api_client: TestClient, key: str, expected: str
                                       ) -> None:
    r = api_client.post("/api/llm/models", json={
        "alias": "qa-model", "provider": "openrouter", "model_name": "vendor/model",
        "api_key": key})
    assert r.status_code in (200, 201), r.text
    body = api_client.get("/api/llm/config")
    model = next(m for m in body.json()["models"] if m["alias"] == "qa-model")
    assert model["api_key_masked"] == expected
    assert "api_key" not in model
    if len(key) > 6:
        assert key not in body.text and key not in r.text


@pytest.mark.parametrize(("key", "expected"), [(_DSKEY40, _mask3(_DSKEY40)), (_SHORT, "•••")])
def test_a_data_source_key_is_masked_like_a_model_key(
    api_client: TestClient, key: str, expected: str
) -> None:
    r = api_client.put("/api/datasources/finmind/key", json={"api_key": key})
    assert r.status_code == 200, r.text
    assert r.json()["token_masked"] == expected
    listing = api_client.get("/api/datasources")
    row = next(s for s in listing.json()["sources"] if s["id"] == "finmind")
    assert row["token_masked"] == expected
    if len(key) > 6:
        assert key not in listing.text and key not in r.text


def test_notify_secrets_are_masked_3_plus_3(api_client: TestClient,
                                            golden_db: sqlite3.Connection) -> None:
    notify.ensure_seeded(golden_db)
    cfg = notify.load_config(golden_db)
    cfg.ntfy.token = "tk_" + "n" * 30 + "END"
    cfg.telegram.bot_token = "123456:" + "T" * 30 + "KEN"
    cfg.email.password = "app-password-" + "p" * 20 + "WXY"
    notify.save_config(golden_db, cfg, now=GOLDEN_NOW)
    r = api_client.get("/api/notify/config")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ntfy"]["token_masked"] == _mask3(cfg.ntfy.token)
    assert b["telegram"]["bot_token_masked"] == _mask3(cfg.telegram.bot_token)
    assert b["email"]["password_masked"] == _mask3(cfg.email.password)
    for secret in (cfg.ntfy.token, cfg.telegram.bot_token, cfg.email.password):
        assert secret not in r.text


def test_a_short_notify_secret_is_fully_masked(api_client: TestClient,
                                               golden_db: sqlite3.Connection) -> None:
    notify.ensure_seeded(golden_db)
    cfg = notify.load_config(golden_db)
    cfg.email.password = _SHORT
    notify.save_config(golden_db, cfg, now=GOLDEN_NOW)
    assert api_client.get("/api/notify/config").json()["email"]["password_masked"] == "•••"


# ------------------------------------------------------------------ 2. the inputs

#: What names a secret: an id / label / placeholder mentioning one of these.
_SECRET = re.compile(r"api[ -_]?key|\bkey\b|token|password|passwd|secret|金鑰|權杖|密碼", re.I)
#: Input types that cannot hold a secret (a count of tokens, a checkbox, a date…).
_NOT_TEXT = {"number", "checkbox", "radio", "date", "range", "file", "hidden"}
#: Reviewed exceptions, each with its reason.
_ALLOWED_VISIBLE = {
    # The ntfy topic is GENERATED by the server and never typed: the field is readonly and
    # exists to be read and 複製'd into the phone app (the SOP on the same card says so). On a
    # guest (demo) instance the API sends only its 3+3 mask (test_notify_api.py, F1).
    "nt-ntfy-topic": "readonly, server-generated; shown so the owner can copy it to the phone",
}


def _html_inputs() -> list[tuple[str, str, str | None]]:
    """(where, descriptor, type) for every <input> in web/*.html."""
    out: list[tuple[str, str, str | None]] = []
    for path in sorted(_WEB.glob("*.html")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<input\b[^>]*>", src):
            tag = m.group(0)
            ident = re.search(r'\bid="([^"]+)"', tag)
            ph = re.search(r'\bplaceholder="([^"]+)"', tag)
            typ = re.search(r'\btype="([^"]+)"', tag)
            before = src[max(0, m.start() - 200):m.start()]
            labels = re.findall(r"<label[^>]*>([^<]*)</label>", before)
            desc = " ".join(x for x in (ident.group(1) if ident else "",
                                        ph.group(1) if ph else "",
                                        labels[-1] if labels else "") if x)
            line = src.count("\n", 0, m.start()) + 1
            out.append((f"{path.name}:{line}#{ident.group(1) if ident else '?'}",
                        desc, typ.group(1) if typ else None))
    return out


def _js_inputs() -> list[tuple[str, str, str | None]]:
    """(where, descriptor, type) for every input BUILT in web/*.js: the creating line plus
    the statements that configure it (its .type / .placeholder and the field's label)."""
    out: list[tuple[str, str, str | None]] = []
    for path in sorted(_WEB.glob("*.js")):
        if path.name.endswith(".min.js"):
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            m = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:el|document\.createElement)"
                          r"\(\s*'input'", line)
            if not m:
                continue
            name = m.group(1)
            window = "\n".join(lines[max(0, i - 4):i + 8])
            typ = re.search(rf"{name}\.type\s*=\s*'([^']+)'", window)
            out.append((f"{path.name}:{i + 1}", window, typ.group(1) if typ else None))
    return out


def _secret_inputs() -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    for where, desc, typ in _html_inputs() + _js_inputs():
        if typ in _NOT_TEXT or not _SECRET.search(desc):
            continue
        found.append((where, typ))
    return found


def test_the_scan_finds_the_known_secret_inputs() -> None:
    """The scan is not blind: the fields the verifier named, and the three that were already
    right, are all inside it."""
    where = " ".join(w for w, _ in _secret_inputs())
    for needle in ("#dr-key", "#nt-ntfy-token", "#nt-tg-token", "#nt-em-pass", "#nu-pass",
                   "#login-pass", "settings-datasources.js:"):
        assert needle in where, f"{needle} is missing from the secret-input scan: {where}"


def test_every_secret_input_is_a_password_field() -> None:
    bad = [f"{w} (type={t!r})" for w, t in _secret_inputs()
           if t != "password" and not any(f"#{k}" in w for k in _ALLOWED_VISIBLE)]
    assert not bad, "secret inputs that echo what is typed:\n" + "\n".join(bad)


def test_every_allowed_visible_input_still_exists() -> None:
    where = " ".join(w for w, _ in _secret_inputs())
    stale = [k for k in _ALLOWED_VISIBLE if f"#{k}" not in where]
    assert not stale, f"allowlist entries no longer in the scan — remove them: {stale}"


def test_the_password_fields_do_not_offer_to_autofill_a_login() -> None:
    """A key field is not the owner's login: ``autocomplete="new-password"`` keeps a browser
    from filling the saved LOGIN password into it."""
    html = (_WEB / "settings.html").read_text(encoding="utf-8")
    for ident in ("dr-key", "nt-ntfy-token", "nt-tg-token", "nt-em-pass"):
        tag = re.search(rf'<input\b[^>]*\bid="{ident}"[^>]*>', html)
        assert tag and 'autocomplete="new-password"' in tag.group(0), ident
    js = (_WEB / "settings-datasources.js").read_text(encoding="utf-8")
    assert "inp.autocomplete = 'new-password'" in js
