"""Unit tests: pricing.names — best-effort name lookup (all seams stubbed)."""

import pytest

from portfolio_dash.pricing import names
from portfolio_dash.shared.enums import Market


def test_tw_uses_static_table_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(names, "_tw_name", lambda s: "台積電")
    monkeypatch.setattr(names, "_yf_name", lambda s: pytest.fail("must not hit yfinance"))
    assert names.lookup_name("2330", Market.TW) == "台積電"


def test_tw_falls_through_to_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def yf(sym: str) -> str | None:
        seen.append(sym)
        return "GlobalWafers"

    monkeypatch.setattr(names, "_tw_name", lambda s: None)
    monkeypatch.setattr(names, "_yf_name", yf)
    assert names.lookup_name("6488", Market.TW, board="TPEx") == "GlobalWafers"
    assert seen == ["6488.TWO"]  # TPEx uses the .TWO suffix


def test_us_and_my_suffixes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def yf(sym: str) -> str | None:
        seen.append(sym)
        return "X"

    monkeypatch.setattr(names, "_yf_name", yf)
    # Isolate the yfinance-suffix path: force a registry MISS so MY falls through to .KL.
    monkeypatch.setattr(names, "bursa_name", lambda s: None)
    assert names.lookup_name("AAPL", Market.US) == "X"
    assert names.lookup_name("0138", Market.MY) == "X"
    assert seen == ["AAPL", "0138.KL"]


def test_my_uses_bursa_registry_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """MY resolves offline via the baked Bursa registry BEFORE yfinance — the registry
    covers counters the ``.KL`` feed lacks (the resolve-demotion fix, W1 batch-A)."""
    monkeypatch.setattr(names, "bursa_name", lambda s: "MYEG" if s == "0138" else None)
    monkeypatch.setattr(names, "_yf_name", lambda s: pytest.fail("must not hit yfinance"))
    assert names.lookup_name("0138", Market.MY) == "MYEG"


def test_my_falls_through_to_yfinance_on_registry_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def yf(sym: str) -> str | None:
        seen.append(sym)
        return "SomeCounter"

    monkeypatch.setattr(names, "bursa_name", lambda s: None)  # not in the static list
    monkeypatch.setattr(names, "_yf_name", yf)
    assert names.lookup_name("9999", Market.MY) == "SomeCounter"
    assert seen == ["9999.KL"]


def test_tw_static_table_failure_still_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (live 2026-07-02): a missing/broken twstock must NOT abort the
    lookup — the yfinance fallback still runs (deployed venvs lacked twstock and
    every TW name came back empty)."""
    def boom(sym: str) -> str | None:
        raise ModuleNotFoundError("No module named 'twstock'")

    monkeypatch.setattr(names, "_tw_name", boom)
    monkeypatch.setattr(names, "_yf_name", lambda s: "TSMC")
    assert names.lookup_name("2330", Market.TW) == "TSMC"


def test_lookup_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(sym: str) -> str | None:
        raise RuntimeError("network down")

    monkeypatch.setattr(names, "_tw_name", boom)
    monkeypatch.setattr(names, "_yf_name", boom)
    assert names.lookup_name("2330", Market.TW) is None
    assert names.lookup_name("AAPL", Market.US) is None
