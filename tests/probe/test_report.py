from scripts.probe.models import DataType, ProbeResult, Verdict
from scripts.probe.report import render_report


def _r(source: str, dt: DataType, market: str, verdict: Verdict) -> ProbeResult:
    return ProbeResult(source=source, data_type=dt, market=market,
                       requires_key=False, verdict=verdict, coverage_hits=5)


def test_render_report_groups_by_market_and_type() -> None:
    results = [
        _r("yfinance", DataType.QUOTE_LATEST, "US", Verdict.PRIMARY),
        _r("finnhub", DataType.QUOTE_LATEST, "US", Verdict.FALLBACK),
        _r("yfinance", DataType.FX, "FX", Verdict.PRIMARY),
    ]
    md = render_report(results)
    assert "# Data-Source Probe Results" in md
    assert "## US — quote_latest" in md
    assert "yfinance" in md and "finnhub" in md
    # primary listed before fallback in the recommendation line
    assert md.index("Recommended order") < md.index("finnhub")


def test_render_report_marks_unusable_and_skipped() -> None:
    md = render_report([_r("bursa", DataType.QUOTE_LATEST, "MY", Verdict.UNUSABLE)])
    assert "unusable" in md.lower()
