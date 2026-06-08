"""Run one source probe with retries + timing; record raw fixtures."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.probe.models import DataType, ProbeResult, Verdict

_FIXTURE_ROOT = Path("tests/pricing/fixtures")


def run_probe(
    source: str,
    data_type: DataType,
    market: str,
    fn: Callable[[], Any],
    *,
    requires_key: bool,
    attempts: int = 3,
) -> ProbeResult:
    """Call ``fn`` up to ``attempts`` times, timing the first success.

    ``fn`` performs the live fetch and returns parsed data (truthy on success).
    On total failure the result is UNUSABLE with the last error recorded.
    """
    last_err: Exception | None = None
    for _ in range(attempts):
        start = time.perf_counter()
        try:
            fn()
            latency = (time.perf_counter() - start) * 1000.0
            return ProbeResult(
                source=source, data_type=data_type, market=market,
                requires_key=requires_key, verdict=Verdict.FALLBACK,
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001 - probe records any failure
            last_err = exc
    return ProbeResult(
        source=source, data_type=data_type, market=market,
        requires_key=requires_key, verdict=Verdict.UNUSABLE,
        error=str(last_err),
    )


def save_fixture(
    source: str,
    key: str,
    raw: str,
    *,
    root: Path = _FIXTURE_ROOT,
    ext: str = "json",
) -> Path:
    """Persist a raw response under ``root/source/key.ext`` for later mock tests."""
    safe = key.replace("/", "_").replace("=", "")
    out = root / source / f"{safe}.{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")
    return out
