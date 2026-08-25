"""R3/⑪ counter-evidence: the evidence windows outran the horizon they back.

``official_templates._ADVICE_BODY`` hands the model ``signal_backtest_json`` to back a
DIRECTIONAL claim, and that claim is scored over the card's prediction horizon — 14
CALENDAR days on the official advice card. The study, meanwhile, only ever measured
+20/+60/+120 TRADING days. So the assistant cited a +20-session distribution in support
of a 14-day call: two different questions, one of them silently substituted for the other.

The fix adds a **+10 trading-day** window (~14 calendar days) so at least one evidence
window measures the thing the scoreboard will grade. The scoring contract is NOT touched.

The second test pins the property that makes the short window worth having: right-
censoring is monotone in window length, so the aligned window is also the one with the
MOST samples — the opposite of the usual accuracy-for-coverage trade.
"""

from decimal import Decimal

from portfolio_dash.portfolio.backtest import (
    FORWARD_WINDOWS,
    HistoryPoint,
    event_study,
)

D = Decimal
_HIGH = D("65")
_LOW = D("35")


def _study(signs: list[str], n_closes: int) -> object:
    from datetime import date, timedelta
    start = date(2026, 1, 1)
    points = [
        HistoryPoint(as_of=start + timedelta(days=i),
                     scores={"momentum_12_1": D(s)}, tech_score=None)
        for i, s in enumerate(signs)
    ]
    closes = [(start + timedelta(days=i), D(100 + i)) for i in range(n_closes)]
    return event_study(points, closes, band_high=_HIGH, band_low=_LOW)


def test_the_window_set_carries_the_scoring_horizon() -> None:
    """10 trading days ~ 14 calendar days = the official advice card horizon."""
    assert 10 in FORWARD_WINDOWS
    assert FORWARD_WINDOWS[0] == 10, "the aligned window leads — it is the one cited"


def test_a_short_series_can_answer_the_14_day_question_and_only_that_one() -> None:
    """The defect in miniature: on a 15-session series every OLD window is censored.

    Event at index 1 (price 101). +10 lands on index 11 (price 111) -> 10/101. +20/+60/+120
    all need sessions that have not happened. Before the fix the study had NOTHING to say
    about this event, while the card it backed was due in 14 days.
    """
    study = _study(["0.5", "-0.5"], 15)
    group = study.groups[0]                                    # type: ignore[attr-defined]
    assert (group.kind, group.direction, group.events) == ("momentum_12_1", "bearish", 1)
    by_window = {o.window: o for o in group.outcomes}
    assert set(by_window) == {10, 20, 60, 120}

    aligned = by_window[10]
    assert aligned.n == 1 and aligned.n_censored == 0
    # n=1 is below MIN_SAMPLE, so the honest report is the COUNT with no numbers.
    assert aligned.stats is None

    for w in (20, 60, 120):
        assert by_window[w].n == 0 and by_window[w].n_censored == 1

    # The baseline follows the same axis: only the aligned window has one here.
    assert study.baselines[0] is not None                      # type: ignore[attr-defined]
    assert study.baselines[1] is None                          # type: ignore[attr-defined]


def test_right_censoring_is_monotone_so_the_aligned_window_has_the_most_samples() -> None:
    """A shorter window can only ever KEEP events a longer one had to drop."""
    signs = ["0.5"] + ["-0.5" if i % 2 else "0.5" for i in range(1, 40)]
    study = _study(signs, 60)
    for group in study.groups:                                 # type: ignore[attr-defined]
        counts = [o.n for o in group.outcomes]
        assert counts == sorted(counts, reverse=True), (
            f"{group.kind}/{group.direction}: n must not rise with window length"
        )
        assert counts[0] > 0, "the aligned window must survive a 60-session series"
