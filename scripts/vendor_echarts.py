"""Download web/echarts.min.js from upstream, verified against TWO mirrors and a pinned hash.

Why a script and not a manual download (owner ruling 2026-08-12): the artifact is committed,
so the only thing that keeps it honest is a reproducible way to re-derive it. This is the same
shape as ``scripts/regen_golden_full.py`` -> ``tests/golden/dashboard_full.json``: a committed
generator whose committed output is guarded by a test.

    .venv/Scripts/python scripts/vendor_echarts.py

Three gates, all of which must pass BEFORE a single byte is written:

1. **Two mirrors.** jsdelivr and unpkg both serve the same npm tarball. They must answer
   byte-identically, so one compromised or truncated mirror cannot land in the tree.
2. **The pinned digest.** Imported from ``tests/contract/test_vendored_assets.py`` rather than
   restated here, so the downloader and the guard cannot drift apart. There is exactly one
   machine-readable copy of the hash in this repo.
3. **The pinned size**, as a cheap independent check on the same bytes.

No Node, no npm, no bundler — stdlib ``urllib`` and a file write. A pre-minified ``dist`` file
copied in is a file copy; the "no build step" rule (`.claude/rules/stack.md`) is untouched. A
CUSTOM or partial ECharts build would need a toolchain and is out of bounds.

To upgrade the version: change the constants in ``tests/contract/test_vendored_assets.py``
(version + size + sha256, taken from the upstream release you intend to ship), run this, and
review the diff. See ``docs/reference/vendored-assets.md``.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tests.contract.test_vendored_assets import (  # noqa: E402
    ECHARTS_PATH,
    ECHARTS_SHA256,
    ECHARTS_SIZE,
    ECHARTS_SOURCES,
    ECHARTS_VERSION,
)

_TIMEOUT_S = 120
_UA = "portfolio-dash-vendor-script"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (pinned https)
        if resp.status != 200:
            raise RuntimeError(f"{url} -> HTTP {resp.status}")
        body: bytes = resp.read()
    print(f"  fetched {len(body):,} bytes  sha256={hashlib.sha256(body).hexdigest()}  {url}")
    return body


def main() -> int:
    print(f"vendoring ECharts {ECHARTS_VERSION} -> {ECHARTS_PATH.relative_to(_ROOT)}")
    bodies: list[bytes] = []
    for url in ECHARTS_SOURCES:
        try:
            bodies.append(_fetch(url))
        except Exception as exc:  # noqa: BLE001 (any failure is the same verdict: do not write)
            print(f"FAIL: could not fetch {url}: {exc!r}")
            return 1

    first, *rest = bodies
    for url, body in zip(ECHARTS_SOURCES[1:], rest, strict=True):
        if body != first:
            print(
                f"FAIL: mirrors disagree — {ECHARTS_SOURCES[0]} and {url} returned different "
                f"bytes ({len(first):,} vs {len(body):,}). Nothing written."
            )
            return 1
    print(f"  OK: {len(ECHARTS_SOURCES)} mirrors agree byte-for-byte")

    digest = hashlib.sha256(first).hexdigest()
    if len(first) != ECHARTS_SIZE or digest != ECHARTS_SHA256:
        print(
            "FAIL: upstream does not match the pin. Nothing written.\n"
            f"  got  size={len(first):,} sha256={digest}\n"
            f"  want size={ECHARTS_SIZE:,} sha256={ECHARTS_SHA256}\n"
            "  If this is an intended UPGRADE, update the constants in "
            "tests/contract/test_vendored_assets.py first — do NOT edit them just to make "
            "this pass."
        )
        return 1
    print(f"  OK: matches the pin (size={ECHARTS_SIZE:,}, sha256={ECHARTS_SHA256})")

    unchanged = ECHARTS_PATH.is_file() and ECHARTS_PATH.read_bytes() == first
    ECHARTS_PATH.write_bytes(first)
    print(f"{'unchanged' if unchanged else 'WROTE'} {ECHARTS_PATH.relative_to(_ROOT)}")
    print("done. Remember: the file is COMMITTED — `git add web/echarts.min.js`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
