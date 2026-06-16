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
   render a degraded state. */
(function () {
  'use strict';

  /** Structured error thrown for any non-2xx response. Mirrors the
      api/errors.py envelope: { error: { code, message, field?, issues? } }. */
  class PdApiError extends Error {
    constructor(status, code, message, field, issues) {
      super(message);
      this.name = 'PdApiError';
      this.status = status;
      this.code = code;
      this.message = message;
      this.field = field;            // optional; undefined when absent
      this.issues = issues;          // optional; undefined when absent
    }
  }

  /* abortable(): same-key in-flight request controllers (typeahead/search-cancel). */
  const _controllers = new Map();

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

  /** Parse a non-2xx envelope defensively into a PdApiError. */
  async function _toError(resp) {
    let code = 'error';
    let message = resp.statusText || 'request failed';
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
      /* no / non-JSON body — keep statusText-derived defaults */
    }
    return new PdApiError(resp.status, code, message, field, issues);
  }

  /** Shared response handler: 2xx → parsed-untouched JSON (or null); else throw. */
  async function _handle(resp) {
    if (resp.ok) {
      if (resp.status === 204) return null;
      const text = await resp.text();
      if (!text) return null;
      return JSON.parse(text);         // strings stay strings — NO coercion
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
    return fetch(url, _jsonInit('GET', undefined, opts)).then(_handle);
  }

  function post(path, body, opts) {
    return fetch(_normPath(path), _jsonInit('POST', body, opts)).then(_handle);
  }

  function put(path, body, opts) {
    return fetch(_normPath(path), _jsonInit('PUT', body, opts)).then(_handle);
  }

  function del(path, opts) {
    return fetch(_normPath(path), _jsonInit('DELETE', undefined, opts)).then(_handle);
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
    const method = body !== undefined && body !== null ? 'POST' : 'GET';
    const resp = await fetch(_normPath(path), _jsonInit(method, body, opts));
    if (!resp.ok) {
      const err = await _toError(resp);
      if (resp.status === 401 && !window.location.pathname.endsWith('login.html')) {
        window.location.replace('login.html');
      }
      throw err;
    }
    const blob = await resp.blob();
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
    abortable: abortable
  };
})();
