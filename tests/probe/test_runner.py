from pathlib import Path

from scripts.probe.models import DataType, Verdict
from scripts.probe.runner import run_probe, save_fixture


def test_run_probe_captures_latency_and_success() -> None:
    def ok() -> dict[str, int]:
        return {"price": 100}

    r = run_probe("fake", DataType.QUOTE_LATEST, "US", ok, requires_key=False)
    assert r.error is None
    assert r.latency_ms is not None and r.latency_ms >= 0
    assert r.verdict in (Verdict.PRIMARY, Verdict.FALLBACK)


def test_run_probe_records_error_after_retries() -> None:
    calls = {"n": 0}

    def boom() -> dict[str, int]:
        calls["n"] += 1
        raise RuntimeError("nope")

    r = run_probe("fake", DataType.QUOTE_LATEST, "US", boom,
                  requires_key=False, attempts=3)
    assert calls["n"] == 3
    assert r.verdict is Verdict.UNUSABLE
    assert r.error is not None and "nope" in r.error


def test_save_fixture_writes_file(tmp_path: Path) -> None:
    p = save_fixture("yfinance", "AAPL", '{"ok": 1}', root=tmp_path, ext="json")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == '{"ok": 1}'
    assert p.parent.name == "yfinance"
