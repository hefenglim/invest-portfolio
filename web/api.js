/* portfolio-dash — the ONE fetch layer (decision B, spec 19.1).

   Every page routes ALL `/api/*` calls through `window.pdApi`; no page calls
   `fetch` directly. This is the single seam between the static vanilla-JS
   frontend and the FastAPI JSON API.

   MONEY-PASSTHROUGH GUARANTEE: the backend delivers every Decimal (money /
   price / rate / ratio) as a canonical STRING. pdApi hands the parsed response
   body to callers UNTOUCHED — `JSON.parse` keeps strings as strings, and pdApi
   never applies `parseFloat` / `Number` / `+` to it. The frontend NEVER computes
   money and NEVER coerces these values. Pure counts (shares / tokens / n) ride
   along as JSON numbers, but the body is treated opaquely regardless.

   ERROR MODEL: non-2xx bodies follow the api/errors.py envelope
     { "error": { "code", "message", "field"?, "issues"? } }.
   pdApi does NOT toast. It THROWS a structured `PdApiError`; the CALLER catches
   and does `window.toast(err.message, 'fail', err.code)`. The ONE exception is
   401 → `window.location.replace('login.html')`. Both redirect sites — the shared
   `_handle` (GET/POST/PUT/DELETE) and `download()` — apply the IDENTICAL guard,
   redirecting EXCEPT when already on login.html (a wrong-password POST
   /api/auth/login returns 401; redirecting there would self-reload and swallow the
   form's error).
   In all cases the PdApiError is still thrown so in-flight callers stop. 402 / 409 /
   503 are re-thrown WITHOUT redirect so the AI/insight block can catch them and
   render a degraded state.

   ACCOUNT REFERENCE TOKENS (DEF-023, 2026-09-23): the backend has no zh account name
   (web/names.js is the ONE naming authority), so a backend sentence that names an
   account carries the token `{account:<id>}` instead of a name or a bare id
   (portfolio_dash/shared/account_ref.py). This layer resolves the token on EVERY
   response — the parsed 2xx body and the error envelope's `message` / `issues` alike —
   by walking the value and replacing tokens inside strings with
   `pdNames.account(id)` (`pdNames.resolveRefs`). Without names.js on the page the id
   itself is shown, exactly what pdNames does for an unknown id. The walk touches ONLY
   strings that contain the literal `{account:`; every other value — every Decimal
   money string in particular — is returned as-is, so the money-passthrough guarantee
   above is unchanged. */
(function () {
  'use strict';

  /* The token grammar, duplicated from names.js ONLY for the degrade path (a page that
     loads api.js without names.js). Pinned equal by tests/contract/test_account_ref_seam.py. */
  const ACCOUNT_REF = /\{account:([^{}\s]+)\}/g;

  function _resolveText(s) {
    if (s.indexOf('{account:') === -1) return s;        // fast path: byte-identical
    const names = window.pdNames;
    if (names && typeof names.resolveRefs === 'function') return names.resolveRefs(s);
    return s.replace(ACCOUNT_REF, (m, id) => id);      // names.js absent: the id itself
  }

  /** Resolve account tokens in every string of a parsed JSON value, in place. Arrays and
      plain objects are walked; strings are replaced only when they carry a token; every
      other value (numbers, booleans, null) is untouched. Returns the same value. */
  function _resolveRefs(v) {
    if (typeof v === 'string') return _resolveText(v);
    if (Array.isArray(v)) {
      for (let i = 0; i < v.length; i++) v[i] = _resolveRefs(v[i]);
      return v;
    }
    if (v !== null && typeof v === 'object') {
      Object.keys(v).forEach(function (k) { v[k] = _resolveRefs(v[k]); });
      return v;
    }
    return v;
  }

  /** Structured error thrown for any non-2xx response. Mirrors the
      api/errors.py envelope: { error: { code, message, field?, issues? } }.

      `message` is left EMPTY when the envelope supplied none (2026-08-29): `super()`
      leaves Error's inherited '' in that case, and '' is falsy, so a caller's
      `(err && err.message) || '中文預設'` reaches its Chinese fallback. Assigning the
      argument unconditionally would write `undefined` here — which renders as the string
      "undefined" at any site that concatenates without a fallback. See _toError. */
  class PdApiError extends Error {
    constructor(status, code, message, field, issues) {
      super(message);
      this.name = 'PdApiError';
      this.status = status;
      this.code = code;
      if (message !== undefined && message !== null) this.message = message;
      this.field = field;            // optional; undefined when absent
      this.issues = issues;          // optional; undefined when absent
    }
  }

  /* abortable(): same-key in-flight request controllers (typeahead/search-cancel). */
  const _controllers = new Map();

  /* ---- global network-activity tracking (Progress system, 2026-07-02) ----
     Every pdApi call increments/decrements one in-flight counter and dispatches a
     `pd-net` CustomEvent on document with {pending}. shell.js renders the global
     top progress bar off these events, so EVERY network wait in the app gets a
     visible indicator with no per-page wiring. Decrement runs on settle (success,
     HTTP error, network error, abort alike). */
  let _pending = 0;
  function _netEvent() {
    try {
      document.dispatchEvent(new CustomEvent('pd-net', { detail: { pending: _pending } }));
    } catch (e) { /* dispatch must never break a request */ }
  }
  function _track(p) {
    _pending += 1;
    _netEvent();
    const dec = function () { _pending = Math.max(0, _pending - 1); _netEvent(); };
    return p.then(
      function (v) { dec(); return v; },
      function (e) { dec(); throw e; }
    );
  }

  /** Normalize a path to a single leading slash; used verbatim (no /api prefix). */
  function _normPath(path) {
    const p = String(path == null ? '' : path);
    return '/' + p.replace(/^\/+/, '');
  }

  /** Append params (object) as a querystring, skipping null/undefined values. */
  function _withParams(path, params) {
    if (!params || typeof params !== 'object') return path;
    const usp = new URLSearchParams();
    Object.keys(params).forEach(function (k) {
      const v = params[k];
      if (v === null || v === undefined) return;
      usp.append(k, String(v));
    });
    const qs = usp.toString();
    return qs ? path + (path.indexOf('?') === -1 ? '?' : '&') + qs : path;
  }

  /** Build fetch init for a JSON request. `opts` may carry { signal }. */
  function _jsonInit(method, body, opts) {
    const init = { method: method, credentials: 'same-origin' };
    if (body !== undefined && body !== null) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    if (opts && opts.signal) init.signal = opts.signal;
    return init;
  }

  /** Parse a non-2xx envelope defensively into a PdApiError.

      NO MESSAGE IS INVENTED HERE. This used to open with
      `let message = resp.statusText || 'request failed'` — always truthy — so every
      caller's `(err && err.message) || '中文預設'` was dead code and a proxy 502 with an
      HTML body toasted 「Bad Gateway」 at the owner (82 Chinese fallbacks across 27 files
      were unreachable, 2026-08-29). The server's envelope is the ONLY source of a
      user-facing message; when it did not supply one, `message` stays unset and the
      caller's own Chinese default is what the owner reads.

      The English `statusText` is not lost — it rides along on `err.statusText` for
      diagnostics (web/detail.js's comment already names it as such). It is simply not a
      sentence anyone should be shown. */
  async function _toError(resp) {
    let code = 'error';
    let message;                       // stays undefined unless the envelope supplies one
    let field;
    let issues;
    try {
      const body = await resp.json();
      const err = body && body.error;
      if (err && typeof err === 'object') {
        if (err.code) code = err.code;
        if (err.message) message = err.message;
        field = err.field;             // stays undefined if absent
        issues = err.issues;           // stays undefined if absent
      }
    } catch (e) {
      /* no / non-JSON body — no message of record; the caller's zh fallback renders */
    }
    // The error path resolves account tokens too: a 4xx `message` is toasted verbatim and
    // its `issues[].text` are rendered as the form's findings.
    const out = new PdApiError(
      resp.status, code, _resolveRefs(message), field, _resolveRefs(issues));
    out.statusText = resp.statusText || '';   // diagnostics only — never toasted
    return out;
  }

  /** Shared response handler: 2xx → parsed-untouched JSON (or null); else throw. */
  async function _handle(resp) {
    if (resp.ok) {
      if (resp.status === 204) return null;
      const text = await resp.text();
      if (!text) return null;
      // strings stay strings — NO coercion; only account tokens inside them are resolved
      return _resolveRefs(JSON.parse(text));
    }
    const err = await _toError(resp);
    if (resp.status === 401 && !window.location.pathname.endsWith('login.html')) {
      // The ONE place the login redirect lives — but NOT when we are already on the
      // login page (a wrong-password POST /api/auth/login returns 401; redirecting
      // there would self-reload and swallow the error the form needs to show).
      window.location.replace('login.html');
    }
    // 402 / 409 / 503 and all other non-2xx: throw so the caller can handle.
    throw err;
  }

  function get(path, params, opts) {
    const url = _withParams(_normPath(path), params);
    return _track(fetch(url, _jsonInit('GET', undefined, opts)).then(_handle));
  }

  function post(path, body, opts) {
    return _track(fetch(_normPath(path), _jsonInit('POST', body, opts)).then(_handle));
  }

  function put(path, body, opts) {
    return _track(fetch(_normPath(path), _jsonInit('PUT', body, opts)).then(_handle));
  }

  function del(path, opts) {
    return _track(fetch(_normPath(path), _jsonInit('DELETE', undefined, opts)).then(_handle));
  }

  /** Derive a download filename from Content-Disposition, else a fallback. */
  function _filenameFromDisposition(resp, fallback) {
    const cd = resp.headers.get('Content-Disposition') || '';
    // RFC 5987 filename*=UTF-8''… takes precedence over plain filename=…
    let m = /filename\*=(?:UTF-8'')?["']?([^"';]+)["']?/i.exec(cd);
    if (m && m[1]) {
      try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
    }
    m = /filename=["']?([^"';]+)["']?/i.exec(cd);
    if (m && m[1]) return m[1];
    return fallback;
  }

  /** File/CSV export: POST if `body` given, else GET; same `_handle` error path;
      on 2xx read a Blob, name it from Content-Disposition, and trigger a browser
      download via a temporary <a download>. Resolves when the click is issued. */
  async function download(path, body, opts) {
    return _track(_download(path, body, opts));
  }

  /* I-10: a downloaded file is the one response no page renders — a print report is SAVED
     and opened offline, where no fetch layer and no names.js can reach it. So the tokens are
     resolved HERE, before the Blob is saved, by the same resolver as every other response:
     text/* only (a zip or any binary is never touched), byte-identical when there is no
     token, the display name HTML-escaped inside text/html, and the UTF-8 BOM preserved
     (Excel needs it on a CSV; `ignoreBOM` keeps it in the decoded string). */
  const _HTML_ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  async function _resolveBlobRefs(blob, resp) {
    const type = String((resp.headers && resp.headers.get('content-type')) || blob.type || '')
      .toLowerCase();
    if (type.indexOf('text/') !== 0) return blob;
    const text = new TextDecoder('utf-8', { ignoreBOM: true }).decode(await blob.arrayBuffer());
    if (text.indexOf('{account:') === -1) return blob;
    const html = type.indexOf('text/html') === 0;
    const out = text.replace(ACCOUNT_REF, function (m) {
      const name = _resolveText(m);
      return html ? name.replace(/[&<>"']/g, function (c) { return _HTML_ESC[c]; }) : name;
    });
    return new Blob([out], { type: blob.type || type });
  }

  async function _download(path, body, opts) {
    const method = body !== undefined && body !== null ? 'POST' : 'GET';
    const resp = await fetch(_normPath(path), _jsonInit(method, body, opts));
    if (!resp.ok) {
      const err = await _toError(resp);
      if (resp.status === 401 && !window.location.pathname.endsWith('login.html')) {
        window.location.replace('login.html');
      }
      throw err;
    }
    const blob = await _resolveBlobRefs(await resp.blob(), resp);
    const filename = _filenameFromDisposition(resp, 'download');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    return null;
  }

  /** abortable(key): cancel any prior in-flight request under `key`, then return
      a fresh AbortController stored under `key`. Caller passes its `.signal` via
      `opts.signal` to the next request, so a new same-key request aborts the
      previous one (typeahead / search-cancel pattern). */
  function abortable(key) {
    const prev = _controllers.get(key);
    if (prev) prev.abort();
    const ctrl = new AbortController();
    _controllers.set(key, ctrl);
    return ctrl;
  }

  window.PdApiError = PdApiError;
  window.pdApi = {
    get: get,
    post: post,
    put: put,
    del: del,
    download: download,
    abortable: abortable,
    pending: function () { return _pending; }
  };
})();
