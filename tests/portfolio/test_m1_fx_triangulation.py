"""M1 — the three stored FX rates are not triangularly consistent, and nothing said so.

Measured on the demo (2026-09-16, all three rows freshly refreshed, dated 2026-09-15,
``stale: false``):

    USD/TWD 31.834999 · MYR/TWD 7.803300 · USD/MYR 4.082500
    USD/MYR × MYR/TWD = 31.856972   →  +0.0690% above the stored USD/TWD

Consequence: converting 4,000 MYR → 983.28 USD at spot moved ``reporting_total`` by
−40.88 TWD, and deleting the conversion row restored it bit-identically — a phantom P&L
produced by the conversion PATH, not by any market move.

These tests pin the CHECK and the DISCLOSURE only. Deriving one of the three as a cross
rate (which would close the triangle by construction) is an owner decision and is
deliberately NOT implemented here: the rates are read, compared, and reported unchanged.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.dashboard import _fx_triangulation
from portfolio_dash.portfolio.dashboard_models import FreshnessReport
from portfolio_dash.pricing.results import FxRead
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import to_wire

_AS_OF = date(2026, 9, 15)


def _read(rate: str, *, as_of: date = _AS_OF, stale: bool = False) -> FxRead:
    return FxRead(rate=Decimal(rate), as_of=as_of, source="test", stale=stale)


def _demo_reads(
    usd_twd: str = "31.834999",
    myr_twd: str = "7.803300",
    usd_myr: str = "4.082500",
) -> dict[tuple[Currency, Currency], FxRead | None]:
    """The exact pair set the demo's reporting-TWD dashboard reads."""
    return {
        (Currency.USD, Currency.TWD): _read(usd_twd),
        (Currency.MYR, Currency.TWD): _read(myr_twd),
        (Currency.USD, Currency.MYR): _read(usd_myr),
    }


def test_the_measured_demo_triple_is_reported_inconsistent() -> None:
    """The exact figures from the audit, to the digit."""
    tris = _fx_triangulation(_demo_reads())
    assert len(tris) == 1, f"expected exactly one closable triangle, got {tris}"
    t = tris[0]
    assert t.via == "USD/MYR × MYR/TWD"
    assert t.pair == "USD/TWD"
    assert t.implied == Decimal("31.856972")
    assert t.direct == Decimal("31.834999")
    assert t.gap_pct == Decimal("0.0690")
    assert t.ok is False
    assert t.as_of == _AS_OF
    assert t.stale is False


def test_a_consistent_triple_is_ok() -> None:
    """Exact by construction: 4.0 × 8.0 == 32.0, so the gap is zero and ok is True."""
    tris = _fx_triangulation(
        _demo_reads(usd_twd="32.000000", myr_twd="8.000000", usd_myr="4.000000")
    )
    assert len(tris) == 1
    t = tris[0]
    assert t.implied == Decimal("32.000000")
    assert t.direct == Decimal("32.000000")
    assert t.gap_pct == Decimal("0.0000")
    assert t.ok is True


def test_a_gap_inside_the_tolerance_is_ok() -> None:
    """0.04% apart — within a retail spread, so it is measured but not flagged."""
    tris = _fx_triangulation(
        _demo_reads(usd_twd="32.000000", myr_twd="8.000000", usd_myr="4.001600")
    )
    assert tris[0].gap_pct == Decimal("0.0400") and tris[0].ok is True


def test_a_missing_leg_yields_no_triangle() -> None:
    """Two pairs close nothing; the list is EMPTY, never a partially-guessed triangle."""
    reads = _demo_reads()
    del reads[(Currency.USD, Currency.MYR)]
    assert _fx_triangulation(reads) == []


def test_an_unresolved_pair_is_not_a_leg() -> None:
    """A read recorded as None (pair never stored) cannot stand in for a rate."""
    reads: dict[tuple[Currency, Currency], FxRead | None] = dict(_demo_reads())
    reads[(Currency.USD, Currency.MYR)] = None
    assert _fx_triangulation(reads) == []


def test_single_currency_ledger_is_unaffected() -> None:
    """The overwhelmingly common case: nothing read, nothing reported."""
    assert _fx_triangulation({}) == []


def test_a_stale_leg_still_produces_a_flagged_triangle() -> None:
    """Presence is the gate, not freshness — a stale rate is exactly when a path
    disagreement is most likely, so the check must not go quiet there."""
    reads = _demo_reads()
    reads[(Currency.MYR, Currency.TWD)] = _read(
        "7.803300", as_of=date(2026, 9, 1), stale=True
    )
    t = _fx_triangulation(reads)[0]
    assert t.stale is True
    assert t.as_of == date(2026, 9, 1), "as_of is the OLDEST leg, not the newest"


def test_a_zero_direct_rate_never_divides() -> None:
    """A rate of 0 is not a rate; it yields no triangle rather than a ZeroDivisionError."""
    assert _fx_triangulation(_demo_reads(usd_twd="0")) == []


def test_wire_shape_is_decimal_strings() -> None:
    """Serialization goes through the existing to_wire path (Decimal → canonical string)."""
    report = FreshnessReport(
        prices=[], fx=[], any_stale=False, missing_prices=[], missing_fx=[],
        fx_triangulation=_fx_triangulation(_demo_reads()),
    )
    wire = to_wire(report.model_dump())
    assert wire["fx_triangulation"] == [
        {
            "via": "USD/MYR × MYR/TWD",
            "pair": "USD/TWD",
            "implied": "31.856972",
            "direct": "31.834999",
            "gap_pct": "0.0690",
            "ok": False,
            "as_of": "2026-09-15",
            "stale": False,
        }
    ]


def test_freshness_report_defaults_to_an_empty_list() -> None:
    """Additive field: constructions that predate it still validate."""
    report = FreshnessReport(
        prices=[], fx=[], any_stale=False, missing_prices=[], missing_fx=[]
    )
    assert report.fx_triangulation == []
    assert report.scheduler_running is None
