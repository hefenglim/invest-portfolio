"""The frozen v0.1.31 modules are still byte-for-byte the 473fe5a source (PERF plan W0.2).

Each ``# ==== FROZEN BEGIN 473fe5a:<file> L<a>-L<b> ====`` marker is followed by a
``# sha256=<hex>`` line holding the sha256 of exactly those source lines at 473fe5a (taken from
``git show``, LF line endings). The region up to ``# ==== FROZEN END ====`` must still hash to
it — a CRLF checkout is normalised first. No git in here: the digests were recorded once, and
``git show 473fe5a:<file> | sed -n '<a>,<b>p' | sha256sum`` re-derives any of them by hand.
"""

import hashlib
import re
from pathlib import Path

import pytest

import portfolio_dash.portfolio.cost_basis as live_cost_basis
from tests.reference import cost_basis_v0131, trend_v0131

_HERE = Path(__file__).resolve().parent
_BEGIN = re.compile(r"^# ==== FROZEN BEGIN (?P<commit>\w+):(?P<src>\S+) "
                    r"L(?P<a>\d+)-L(?P<b>\d+) ====$")
_SHA = re.compile(r"^# sha256=(?P<hex>[0-9a-f]{64})$")
_END = "# ==== FROZEN END ===="

#: The regions each frozen module must carry — a region silently dropped is a finding too.
_EXPECTED = {
    "cost_basis_v0131.py": [("portfolio_dash/portfolio/cost_basis.py", 1, 1250)],
    "trend_v0131.py": [
        ("portfolio_dash/portfolio/timeseries.py", 60, 65),
        ("portfolio_dash/portfolio/timeseries.py", 68, 238),
        ("portfolio_dash/portfolio/timeseries.py", 283, 469),
        ("portfolio_dash/portfolio/returns.py", 281, 308),
        ("portfolio_dash/portfolio/networth.py", 61, 169),
        ("portfolio_dash/portfolio/benchmark_counterfactual.py", 111, 200),
    ],
}


def _regions(path: Path) -> list[tuple[str, str, int, int, str, str]]:
    """``(commit, src, a, b, recorded sha256, region text)`` for every frozen region."""
    lines = path.read_bytes().decode("utf-8").replace("\r\n", "\n").split("\n")
    out: list[tuple[str, str, int, int, str, str]] = []
    i = 0
    while i < len(lines):
        m = _BEGIN.match(lines[i])
        if m is None:
            i += 1
            continue
        sha = _SHA.match(lines[i + 1])
        assert sha is not None, f"{path.name}:{i + 2}: a BEGIN marker without its sha256 line"
        j = lines.index(_END, i + 2)
        text = "".join(line + "\n" for line in lines[i + 2:j])
        out.append((m["commit"], m["src"], int(m["a"]), int(m["b"]), sha["hex"], text))
        i = j + 1
    return out


@pytest.mark.parametrize("module", sorted(_EXPECTED))
def test_frozen_regions_hash_to_the_473fe5a_source(module: str) -> None:
    regions = _regions(_HERE / module)
    assert [(src, a, b) for _c, src, a, b, _h, _t in regions] == _EXPECTED[module]
    for commit, src, a, b, recorded, text in regions:
        assert commit == "473fe5a"
        assert text.count("\n") == b - a + 1, f"{src} L{a}-L{b}: wrong line count"
        actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert actual == recorded, (
            f"{module}: the frozen copy of {src} L{a}-L{b} was edited (sha256 {actual}, "
            f"recorded {recorded}). The reference is never edited except to follow a type "
            f"rename; restore it from `git show 473fe5a:{src}`.")


def test_the_frozen_trend_calls_the_frozen_engine() -> None:
    # The copied bodies call `build_book` by name; that name must be the FROZEN engine, or
    # the reference would silently become the code it is meant to check.
    assert trend_v0131.build_book is cost_basis_v0131.ref_build_book
    assert cost_basis_v0131.ref_build_book is cost_basis_v0131.build_book
    assert cost_basis_v0131.ref_build_book is not live_cost_basis.build_book
    assert trend_v0131.UnbookableLedgerError is cost_basis_v0131.UnbookableLedgerError
    for name in ("replay_refusals", "replay_dividend_refusals", "build_reporting_flows",
                 "daily_value_series", "daily_cash_series", "compose_net_worth",
                 "counterfactual", "_fx_at"):
        assert getattr(trend_v0131, name).__module__ == trend_v0131.__name__, name
