# 09 — 工作階段與授權用戶（P1）

> 前端現況：`shell.js window.pdAuth`＋`login.html`＋`settings-users.js` 全以 localStorage
> （`pd_users`/`pd_session`）mock。後端**完全沒有** auth 支援 — 本 spec 為全新領域。
> 接線後刪除 localStorage 帳密邏輯；`pd_theme`/`pd_sidebar_collapsed` 等 UI 偏好仍留本機。

## 9.0 模式規則（與 shell.js 守門邏輯 1:1）

- **訪客模式**：`auth_users` 表為空 → 所有頁面與 API 開放（單機自用情境），
  `GET /api/auth/session` 回 `mode:"guest"`。
- **保護模式**：表內有 ≥1 用戶 → 所有 `/api/*`（除 `/api/auth/login`、`/api/auth/session`）
  需有效 session cookie，否則 401；前端收到 401 一律導向 `login.html`。

---

## 9.1 POST /api/auth/login

### Endpoint & Method
`POST /api/auth/login`

### Description
帳密登入，建立 server-side session 並下發 HttpOnly cookie。也用於「鎖定畫面」的解鎖。

### Request Structure
- Body：
  ```jsonc
  { "username": "chiaming", "password": "••••••" }   // 皆 string、必填
  ```

### Response Structure
**200**（含 `Set-Cookie: pd_session=<token>; HttpOnly; SameSite=Lax; Path=/`）：
```jsonc
{ "username": "chiaming", "name": "家明" }
```
**401**：`{ "error": { "code": "invalid_credentials", "message": "帳號或密碼錯誤" } }`
**429**（選配）：`{ "error": { "code": "too_many_attempts", "message": "嘗試過多，請稍後再試" } }`

### Python Backend Implementation Notes
- 新表（`config_store.ensure_seeded(conn, "auth", …)` 風格）：
  ```sql
  CREATE TABLE IF NOT EXISTS auth_users (
    username TEXT PRIMARY KEY, name TEXT NOT NULL,
    password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS auth_sessions (
    token TEXT PRIMARY KEY, username TEXT NOT NULL,
    created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0);
  ```
- 雜湊：stdlib `hashlib.scrypt`（`salt = os.urandom(16)`，存 `scrypt$<salt_hex>$<hash_hex>`），
  避免新增第三方依賴；token 用 `secrets.token_urlsafe(32)`。
- 比對失敗與「用戶不存在」回**同一個** 401 訊息（不可洩漏帳號是否存在）。

---

## 9.2 GET /api/auth/session ・ POST /api/auth/logout ・ POST /api/auth/lock

### Endpoint & Method
```
GET  /api/auth/session
POST /api/auth/logout
POST /api/auth/lock
```

### Description
- `session`：頁面載入時的守門查詢（取代 shell.js 讀 localStorage）。
- `logout`：刪除 session 列＋清 cookie。
- `lock`：保留身分但標記 `locked=1`；解鎖＝重新 login（同帳號）。

### Request Structure
- Header：`Cookie: pd_session=…`；Body：皆無。

### Response Structure
`GET /api/auth/session` **200**：
```jsonc
{ "mode": "guest" }                                            // 訪客模式
{ "mode": "user", "username": "chiaming", "name": "家明", "locked": false }
```
`logout`/`lock` **204**（無 body）。無有效 session 時 `lock` 回 401。

### Python Backend Implementation Notes
- 以 FastAPI dependency `require_session()` 實作守門（讀 cookie → 查 `auth_sessions`）；
  訪客模式直接放行。`locked=1` 的 session 視同無效（401），但 login 成功時把同
  username 的 locked session 全數清除。

---

## 9.3 GET/POST/DELETE /api/users

### Endpoint & Method
```
GET    /api/users
POST   /api/users
DELETE /api/users/{username}
```

### Description
授權用戶管理（設定 › 帳戶與費率頁「授權用戶」區）。新增第一個用戶＝啟用保護模式。

### Request Structure
- `POST` Body：
  ```jsonc
  { "name": "家明", "username": "chiaming", "password": "至少 8 字" }
  ```
- `DELETE` Path：`username`。

### Response Structure
`GET` **200**：
```jsonc
[ { "username": "chiaming", "name": "家明", "created_at": "2026-06-12T09:00:00+08:00",
    "is_current": true } ]
```
`POST` **201** 回新列；**409** `{ "error": { "code": "duplicate_username", "message": "帳號已存在" } }`；
**400** 密碼太短。
`DELETE` **204**；移除自己時同時刪除自己的 sessions（前端會導回 login —
與 settings-users.js 現行為一致）。

### Python Backend Implementation Notes
- 回傳**永不**包含 password_hash。
- `is_current` 由 request 的 session 推得，省前端比對。
- 訪客模式下允許 `POST /api/users`（首次啟用流程本來就發生在無用戶時）。
