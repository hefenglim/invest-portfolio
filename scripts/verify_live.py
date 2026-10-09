"""Post-deploy live smoke check — run from the DEV machine against a deployed URL.

The division of labor (engineering-process.md, "Two-environment loop-engineering"):
heavy gates (pytest / mypy / ruff / browser e2e) run on the dev machine; the deployed
instance is verified BEHAVIOURALLY from outside. This script is that outside check.

    .venv/Scripts/python scripts/verify_live.py https://<host>                # read-only
    .venv/Scripts/python scripts/verify_live.py https://<host> --expect-version 0.1.3
    .venv/Scripts/python scripts/verify_live.py https://<host> --refresh      # + quote refresh

Auth-aware: on a protected (login-gated) instance, gated endpoints answering 401 is
treated as PASS (service up + auth posture correct); on a guest/demo instance they
must answer 200. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.contract.test_vendored_assets import (  # noqa: E402
    ECHARTS_FILENAME,
    ECHARTS_SHA256,
    ECHARTS_SIZE,
)

_TIMEOUT_S = 60


def _get_bytes(base: str, path: str) -> tuple[int, bytes]:
    """GET base+path -> (status, raw body). Never raises on HTTP errors."""
    try:
        with urllib.request.urlopen(base + path, timeout=_TIMEOUT_S) as r:  # noqa: S310
            return r.status, bytes(r.read())
    except urllib.error.HTTPError as e:
        return e.code, bytes(e.read())
    except (urllib.error.URLError, OSError) as e:
        print(f"      (transport error: {e!r})")
        return 0, b""


def _get(base: str, path: str) -> tuple[int, Any]:
    """GET base+path -> (status, parsed json | text | None). Never raises on HTTP errors."""
    try:
        with urllib.request.urlopen(base + path, timeout=_TIMEOUT_S) as r:  # noqa: S310
            raw = r.read().decode("utf-8")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _post(base: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:  # noqa: S310
            raw = r.read().decode("utf-8")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="deployed instance base URL (no trailing slash)")
    ap.add_argument("--expect-version", default=None,
                    help="fail unless /api/health reports this version")
    ap.add_argument("--expect-release", default=None,
                    help="fail unless /api/health reports this release tag "
                         "(e.g. v0.1.4; prod promotes should always pass this)")
    ap.add_argument("--refresh", action="store_true",
                    help="also POST /api/actions/refresh-quotes (mutating; guest/demo only)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 1. health (open even in protected mode) + build identity (version/commit/release).
    st, health = _get(base, "/api/health")
    ok = st == 200 and isinstance(health, dict) and health.get("status") == "ok"
    check("health", ok, f"HTTP {st} {health}")
    if args.expect_version is not None and isinstance(health, dict):
        check("version", health.get("version") == args.expect_version,
              f"got {health.get('version')!r}, want {args.expect_version!r}")
    if args.expect_release is not None and isinstance(health, dict):
        check("release tag", health.get("release") == args.expect_release,
              f"got {health.get('release')!r}, want {args.expect_release!r} "
              f"(commit {health.get('commit')!r})")

    # 2. frontend shell served.
    st, _ = _get(base, "/")
    check("frontend /", st == 200, f"HTTP {st}")

    # 2b. The vendored chart library is actually THERE, and is actually the pinned artifact.
    # This is the ONLY check that proves the offline-resilience property on a DEPLOYED
    # instance: the hermetic contract test reads the dev machine's working tree, so it stays
    # green even if the file never reached the host — and the failure that would cause is
    # silent-ish (every chart on the site degrades to 「圖表元件未載入」 while the API keeps
    # answering 200). It also catches the line-ending trap: a checkout that rewrote LF -> CRLF
    # serves a byte-different file that still RUNS, so only the digest notices.
    st, body = _get_bytes(base, "/" + ECHARTS_FILENAME)
    digest = hashlib.sha256(body).hexdigest() if body else "-"
    check(
        "vendored echarts.min.js",
        st == 200 and len(body) == ECHARTS_SIZE and digest == ECHARTS_SHA256,
        f"HTTP {st}, {len(body):,} bytes (want {ECHARTS_SIZE:,}), sha256 {digest[:16]}… "
        f"(want {ECHARTS_SHA256[:16]}…)",
    )

    # 3. gated data endpoints: 200 (guest) or 401 (protected) are both healthy.
    protected = False
    st, dash = _get(base, "/api/dashboard")
    if st == 401:
        protected = True
        check("dashboard (protected mode)", True, "401 = auth gate intact")
    else:
        ok = st == 200 and isinstance(dash, dict) and "holdings" in dash
        n = len(dash.get("holdings", [])) if isinstance(dash, dict) else "?"
        unreg = (dash.get("freshness", {}).get("unregistered_symbols")
                 if isinstance(dash, dict) else None)
        check("dashboard", ok, f"HTTP {st}, holdings={n}, unregistered={unreg}")

    st, insts = _get(base, "/api/instruments")
    if protected:
        check("instruments (protected mode)", st == 401, f"HTTP {st}")
    else:
        ok = st == 200 and isinstance(insts, dict) and isinstance(insts.get("list"), list)
        check("instruments", ok,
              f"HTTP {st}, n={len(insts.get('list', [])) if isinstance(insts, dict) else '?'}")

    # 3b. Corporate-action reconciliation (spec §6.3 / W5), on the demo corpus's
    # multi-account 3-for-1 SPLIT. Deliberately withheld until W5: the ＋公司行動 term is
    # what closes the identity, so adding this check any earlier would have reddened BOTH
    # sites for a ledger that is in fact perfectly consistent (measured 2026-08-11 against
    # the seeded demo: ORBT reported balances=false with diff −60 / −40 — exactly
    # `shares × (ratio − 1)`, the corporate delta with no term to carry it).
    #
    # ORBT is the one symbol in the corpus held in TWO accounts, so this asserts the
    # PER-ACCOUNT footers as well as the aggregate: D13's all-or-nothing rule is invisible
    # in a total that happens to balance. Skipped (not failed) where the corpus is absent —
    # prod holds the real ledger and has no ORBT.
    if not protected:
        st, det = _get(base, "/api/symbol/ORBT/detail")
        ba = (det.get("activity_reconcile", {}).get("by_account", {})
              if isinstance(det, dict) else {})
        if st == 200 and not ba:
            check("multi-account corporate action (no demo corpus)", True,
                  "ORBT absent — not a demo instance")
        else:
            states = {k: v.get("balances") for k, v in ba.items()}
            check("multi-account corporate action reconciles",
                  st == 200 and len(ba) >= 2 and all(ba[k].get("balances") for k in ba),
                  f"HTTP {st}, per-account balances={states}")

    # 4. optional mutating check: on-demand quote refresh end to end.
    if args.refresh:
        if protected:
            check("refresh-quotes (skipped)", True, "protected instance — not attempted")
        else:
            st, resp = _post(base, "/api/actions/refresh-quotes", {})
            ok = st == 200 and isinstance(resp, dict) and len(resp.get("run_ids", [])) >= 1
            check("refresh-quotes", ok, f"HTTP {st} {resp}")

    print("=" * 40)
    if failures:
        print(f"RESULT: FAIL ({len(failures)}): {', '.join(failures)}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
