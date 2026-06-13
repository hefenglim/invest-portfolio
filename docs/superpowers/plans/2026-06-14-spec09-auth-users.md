# Spec 09 — Sessions & Authorized Users Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development /
> test-driven-development. Checkbox (`- [ ]`) tracking. You work in an isolated git worktree. You
> MAY edit `portfolio_dash/api/app.py` and `tests/conftest.py` to wire and test the global gate.
> **Do NOT edit `CHANGELOG.md`** (controller owns it — avoids a conflict with the parallel spec-15
> worktree). Commit with scoped `git add` (never `-A`/`.`). **Never** commit secrets; this feature
> stores only salted scrypt hashes — never log or return `password_hash`.

**Goal:** Add server-side auth: guest mode (no users → everything open) vs protected mode (≥1 user
→ all `/api/*` except login/session require a valid `pd_session` cookie). Endpoints: login, session,
logout, lock, and authorized-user CRUD. Stdlib only (`hashlib.scrypt`, `secrets`) — no new dependency.

**Architecture:** A self-contained `portfolio_dash/api/auth_store.py` (table DDL, scrypt
hash/verify, token gen, user + session CRUD, mode check) + a `require_session` FastAPI dependency
applied **globally** in `create_app()` (so it shares the `get_conn` override in tests — a
middleware would run outside DI scope and read the wrong DB). Thin routers `routers/auth.py` and
`routers/users.py`. Auth is access-control, not business calculation — it legitimately lives in the
api layer; it imports only `shared` + stdlib.

**Tech stack:** FastAPI (Depends, Response.set_cookie, Cookie), sqlite3, `hashlib.scrypt`,
`secrets.token_urlsafe`, pytest + TestClient.

---

## Reconciliations (read before coding)
1. **Guest mode keeps existing tests green.** `golden_db` seeds **no** `auth_users` → guest →
   `require_session` allows everything → all current contract tests pass unchanged. This is the
   safety property; do not seed a user into `golden_db`.
2. **Global gate via dependency, not middleware.** Add `dependencies=[Depends(require_session)]`
   to the `FastAPI(...)` constructor in `create_app()`. `require_session` (a) only gates
   `request.url.path.startswith("/api/")` (static files & non-api pass), (b) always allows
   `/api/auth/login` and `/api/auth/session`, (c) allows everything in guest mode, (d) else
   validates the `pd_session` cookie against `auth_sessions` (and `locked=0`) → 401 if missing/
   invalid/locked. It depends on `Depends(get_conn)` so tests' `golden_db` override applies.
3. **No user enumeration.** Bad username and bad password both return the **same** 401
   `invalid_credentials`. scrypt verify uses `hmac.compare_digest` on the derived key.
4. **409/400 custom codes** use explicit `JSONResponse(error_body(...))` (the `errors.py`
   `_STATUS_CODE` map lacks 409; mirror `instruments.py`). `duplicate_username`→409,
   short-password→400 `validation_error` (field "password").
5. **Cookie**: `Set-Cookie: pd_session=<token>; HttpOnly; SameSite=Lax; Path=/`. Use
   `response.set_cookie(key="pd_session", value=token, httponly=True, samesite="lax", path="/")`.
   logout deletes the row + `response.delete_cookie("pd_session", path="/")`.
6. **scrypt format**: store `f"scrypt${salt_hex}${hash_hex}"`; `salt = os.urandom(16)`;
   `hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)`. Keep the params as named
   module constants so verify reuses them.

---

### Task 1: `auth_store.py` — tables, crypto, user/session CRUD, mode + `require_session`

**Files:** Create `portfolio_dash/api/auth_store.py`; Test `tests/api/test_auth_store.py`.

**DDL + seed seam** (reuse `config_store.ensure_seeded` so it integrates with the existing
`settings_meta` pattern; seed is a no-op = guest by default):
```python
def create_auth_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS auth_users ("
        " username TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " password_hash TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS auth_sessions ("
        " token TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0);")
    conn.commit()

def ensure_auth_seeded(conn: sqlite3.Connection) -> None:
    config_store.ensure_seeded(conn, "auth", create=create_auth_tables, seed=lambda c: None)
```

**Crypto** (module constants `_SCRYPT_N/R/P/DKLEN`):
```python
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${salt.hex()}${dk.hex()}"

def verify_password(pw: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex),
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return hmac.compare_digest(dk.hex(), hash_hex)
```

**User + session CRUD + mode** (datetimes via an injected `now: datetime` for determinism):
```python
def is_protected(conn) -> bool:        # ≥1 user
def list_users(conn) -> list[Row-ish dicts without password_hash]
def create_user(conn, *, name, username, password, now) -> None  # caller checks dup/short first
def user_exists(conn, username) -> bool
def delete_user(conn, username) -> None   # also delete that user's sessions
def authenticate(conn, username, password) -> bool   # False on missing user OR bad pw (same path)
def create_session(conn, username, *, now) -> str     # token_urlsafe(32); also clears that user's locked sessions
def session_user(conn, token) -> str | None           # username if token valid AND locked=0 else None
def session_row(conn, token) -> dict | None            # {username, locked: bool} for ANY known token (incl. locked); None if unknown — used by GET /auth/session to tell locked-known from unknown
def delete_session(conn, token) -> None
def lock_session(conn, token) -> bool                  # set locked=1; False if token unknown
def get_user(conn, username) -> dict | None            # for name lookup; never includes hash
```
`create_session` must, on login success, delete the user's existing `locked=1` sessions (spec 9.2:
"login 成功時把同 username 的 locked session 全數清除").

**`require_session` dependency** (lives here; wired into app in Task 3):
```python
_OPEN_PATHS = {"/api/auth/login", "/api/auth/session"}

def require_session(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> None:
    path = request.url.path
    if not path.startswith("/api/") or path in _OPEN_PATHS:
        return
    if not is_protected(conn):
        return
    token = request.cookies.get("pd_session")
    if token is None or session_user(conn, token) is None:
        raise HTTPException(status_code=401, detail="需要登入")
```
(Import `get_conn` from `portfolio_dash.api.deps`.)

- [ ] **Step 1 — failing unit tests** `tests/api/test_auth_store.py`:
```python
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from portfolio_dash.api import auth_store as A

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    A.create_auth_tables(c); return c

def test_hash_roundtrip_and_format():
    h = A.hash_password("password123")
    assert h.startswith("scrypt$") and A.verify_password("password123", h)
    assert not A.verify_password("wrong", h)
    assert not A.verify_password("x", "garbage")

def test_guest_until_first_user():
    c = _c()
    assert A.is_protected(c) is False
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    assert A.is_protected(c) is True

def test_authenticate_same_path_for_missing_and_bad():
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    assert A.authenticate(c, "chiaming", "password123") is True
    assert A.authenticate(c, "chiaming", "nope") is False
    assert A.authenticate(c, "ghost", "whatever") is False

def test_session_lifecycle_and_lock():
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    tok = A.create_session(c, "chiaming", now=NOW)
    assert A.session_user(c, tok) == "chiaming"
    assert A.lock_session(c, tok) is True
    assert A.session_user(c, tok) is None         # locked → invalid
    tok2 = A.create_session(c, "chiaming", now=NOW)  # re-login clears locked sessions
    assert A.session_user(c, tok2) == "chiaming"
    A.delete_session(c, tok2)
    assert A.session_user(c, tok2) is None

def test_list_users_never_leaks_hash():
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    rows = A.list_users(c)
    assert rows[0]["username"] == "chiaming" and "password_hash" not in rows[0]

def test_delete_user_removes_sessions():
    c = _c()
    A.create_user(c, name="家明", username="chiaming", password="password123", now=NOW)
    tok = A.create_session(c, "chiaming", now=NOW)
    A.delete_user(c, "chiaming")
    assert A.user_exists(c, "chiaming") is False and A.session_user(c, tok) is None
```

- [ ] **Step 2 — run, FAIL. Step 3 — implement `auth_store.py`. Step 4 — tests green.**
- [ ] **Step 5 — gates (pytest tests/api, mypy, ruff). Step 6 — commit:**
`git add portfolio_dash/api/auth_store.py tests/api/test_auth_store.py && git commit -m "feat(auth): auth_store (scrypt, users/sessions, guest-vs-protected, require_session) (spec 9.0)"`

---

### Task 2: routers `auth.py` + `users.py`

**Files:** Create `portfolio_dash/api/routers/auth.py`, `portfolio_dash/api/routers/users.py`;
Test `tests/contract/test_auth_api.py`.

**`routers/auth.py`:**
- `POST /auth/login` body `{username, password}` (Pydantic model, both required str). On
  `authenticate` False → **401** `JSONResponse` `invalid_credentials` ("帳號或密碼錯誤"). On success:
  `token = create_session(conn, username, now=now)`, set cookie, return 200 `{username, name}`
  (name from `get_user`). Inject `now` via `Depends(get_now)`.
- `GET /auth/session` → **three cases, exactly:**
  1. **Not protected** (no users): `{"mode": "guest"}`.
  2. **Protected + valid/known `pd_session` cookie:** read the session row even if `locked=1`
     (so lock state surfaces): `{"mode": "user", "username": ..., "name": ..., "locked": <bool>}`
     (name via `get_user`; `locked` from the row).
  3. **Protected + absent/unknown cookie:** `{"mode": "user", "username": null, "name": null,
     "locked": false}` — i.e. "protected app, currently signed out" so the shell shows the login
     screen. This is additive to the spec's two literal examples (guest / signed-in user), not
     contradictory; document it in a one-line comment. The controller records it in CHANGELOG.
  Implementation note: this endpoint is in `_OPEN_PATHS`, so it must look up the cookie itself
  (the gate does not run for it). To distinguish case 2's locked row from case 3's invalid cookie,
  query `auth_sessions` directly by token (do NOT use `session_user`, which returns None for locked
  rows — you need to tell "locked but known" apart from "unknown").
- `POST /auth/logout` → delete session (if cookie), `delete_cookie`, **204**.
- `POST /auth/lock` → require a valid cookie; `lock_session`; **204**; if no valid session → **401**.

**`routers/users.py`:**
- `GET /users` → list_users + `is_current` computed from the request's `pd_session` cookie's
  username. 200.
- `POST /users` body `{name, username, password}` → password < 8 → 400 `validation_error`
  (field "password"); `user_exists` → 409 `duplicate_username`; else `create_user` → **201** with
  `{username, name, created_at, is_current: false}`. Allowed in guest mode (first-user bootstrap)
  — this is naturally permitted because `/api/users` is gated, but in guest mode the gate allows it.
- `DELETE /users/{username}` → `delete_user` (idempotent; 404 if not exists is acceptable but spec
  says 204 — return **204** regardless, deleting own sessions if it is the caller). If deleting self,
  also `delete_cookie`.

Use `Depends(get_conn)`, `Depends(get_now)`. For 401/409/400 use explicit `JSONResponse` +
`error_body`. Cookie reads via `request.cookies.get("pd_session")` or `Cookie(default=None)`.

- [ ] **Step 1 — failing contract tests** `tests/contract/test_auth_api.py` (use `api_client` +
`golden_db`; the gate is wired in Task 3, but these tests pass once Task 3 is done — write them now,
they will FAIL until the routers + wiring exist):
```python
def test_guest_session(api_client):
    assert api_client.get("/api/auth/session").json() == {"mode": "guest"}

def test_login_bad_credentials_401(api_client, golden_db):
    from portfolio_dash.api import auth_store as A
    A.create_user(golden_db, name="家明", username="chiaming", password="password123",
                  now=__import__("datetime").datetime(2026,6,11,14,30))
    r = api_client.post("/api/auth/login", json={"username": "chiaming", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r2 = api_client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "invalid_credentials"

def test_login_success_sets_cookie_and_session(api_client, golden_db):
    from portfolio_dash.api import auth_store as A
    A.create_user(golden_db, name="家明", username="chiaming", password="password123",
                  now=__import__("datetime").datetime(2026,6,11,14,30))
    r = api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert r.status_code == 200 and r.json() == {"username": "chiaming", "name": "家明"}
    assert "pd_session" in r.cookies
    s = api_client.get("/api/auth/session").json()
    assert s["mode"] == "user" and s["username"] == "chiaming" and s["locked"] is False

def test_protected_mode_blocks_without_cookie(api_client, golden_db):
    # create a user via the API in guest mode (allowed), then a fresh client has no cookie
    api_client.post("/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"})
    # api_client retains cookies from prior calls; use a cookie-less request
    r = api_client.get("/api/dashboard", cookies={})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"

def test_users_crud(api_client, golden_db):
    r = api_client.post("/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"})
    assert r.status_code == 201
    assert api_client.post("/api/users", json={"name": "x", "username": "chiaming", "password": "password123"}).status_code == 409
    assert api_client.post("/api/users", json={"name": "x", "username": "u2", "password": "short"}).status_code == 400
    # login then list
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    users = api_client.get("/api/users").json()
    assert any(u["username"] == "chiaming" and u["is_current"] for u in users)
    assert all("password_hash" not in u for u in users)

def test_logout_and_lock_204(api_client, golden_db):
    api_client.post("/api/users", json={"name": "家明", "username": "chiaming", "password": "password123"})
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert api_client.post("/api/auth/lock").status_code == 204
    # after lock, session invalid → relogin
    api_client.post("/api/auth/login", json={"username": "chiaming", "password": "password123"})
    assert api_client.post("/api/auth/logout").status_code == 204
```
NOTE on TestClient cookies: `TestClient` persists cookies across calls on the same instance. For
the "blocks without cookie" assertion, pass `cookies={}` on that call, or use
`api_client.cookies.clear()` before it. Adjust as needed so the intent (no cookie ⇒ 401) holds.

- [ ] **Step 2 — run, FAIL (routers/wiring absent). Step 3 — implement both routers.**
- [ ] **Step 4 — proceed to Task 3 to wire; come back and make these green. Step 5 — commit** (after
Task 3 wiring so tests pass):
`git add portfolio_dash/api/routers/auth.py portfolio_dash/api/routers/users.py tests/contract/test_auth_api.py && git commit -m "feat(auth): /api/auth/* + /api/users CRUD (spec 9.1-9.3)"`

---

### Task 3: wire the global gate + routers into the app + golden_db auth tables

**Files:** Modify `portfolio_dash/api/app.py`, `tests/conftest.py`.

- [ ] **Step 1 — `app.py`:**
  - import `auth, users` routers and `from portfolio_dash.api.auth_store import ensure_auth_seeded, require_session`.
  - In `_lifespan`, after the other `ensure_*_seeded(conn)` calls, add `ensure_auth_seeded(conn)`.
  - Change `app = FastAPI(title="portfolio-dash", lifespan=_lifespan)` →
    `app = FastAPI(title="portfolio-dash", lifespan=_lifespan, dependencies=[Depends(require_session)])`
    (import `Depends` from fastapi).
  - `app.include_router(auth.router, prefix="/api")` and `app.include_router(users.router, prefix="/api")`.
- [ ] **Step 2 — `tests/conftest.py` `golden_db` fixture:** after `create_scheduler_tables(conn)`
  add `from portfolio_dash.api.auth_store import create_auth_tables` (top import) and
  `create_auth_tables(conn)` so the gate can query `auth_users` (empty → guest). Do NOT seed a user.
- [ ] **Step 3 — run the FULL suite via `.venv`.** Expect: all prior tests still pass (guest mode),
  and `tests/contract/test_auth_api.py` now green. Fix any test that breaks because the global
  dependency now runs (it should not, since golden_db is guest). If a non-auth test sends paths
  outside `/api/` it is unaffected.
- [ ] **Step 4 — mypy --strict + ruff clean. Step 5 — commit:**
`git add portfolio_dash/api/app.py tests/conftest.py && git commit -m "feat(auth): wire global require_session gate + auth tables (guest-safe) (spec 9.0)"`

## Self-review checklist
Guest mode leaves all existing tests green (no user seeded in golden_db); gate is a global
dependency sharing get_conn (not middleware); login/session exempt; same 401 for bad-user/bad-pass;
password_hash never returned/logged; scrypt + compare_digest; cookie HttpOnly/SameSite=Lax/Path=/;
409/400 explicit JSONResponse; delete-self clears own sessions; auth imports only shared+stdlib
(no portfolio/forex/pricing).
