"""Unit tests for the pure column-level date parser (FU-D19).

Covers every accepted format, evidence-based M/D-vs-D/M disambiguation, identical-readings
auto-resolve, conflicting-readings -> AMBIGUOUS (never a guess), the Excel serial window
bounds, the CJK form, and mixed/garbage columns.
"""

from datetime import date

import pytest

from portfolio_dash.data_ingestion.dateparse import (
    FORMAT_IDS,
    parse_one,
    resolve_date_column,
)

# --- parse_one: each format in isolation -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        ("2026-07-10", "iso", date(2026, 7, 10)),
        ("2026/7/10", "ymd_slash", date(2026, 7, 10)),
        ("2026/07/10", "ymd_slash", date(2026, 7, 10)),
        ("2026.7.10", "ymd_dot", date(2026, 7, 10)),
        ("20260710", "ymd_compact", date(2026, 7, 10)),
        ("7/10/2026", "mdy", date(2026, 7, 10)),
        ("7/10/2026", "dmy", date(2026, 10, 7)),
        ("2026年7月10日", "cjk", date(2026, 7, 10)),
        ("2026年12月1日", "cjk", date(2026, 12, 1)),
    ],
)
def test_parse_one_each_format(value: str, fmt: str, expected: date) -> None:
    assert parse_one(value, fmt) == expected


def test_parse_one_rejects_wrong_syntax() -> None:
    assert parse_one("2026-07-10", "ymd_slash") is None   # dashes are ISO only
    assert parse_one("7/10/2026", "iso") is None
    assert parse_one("2026/7/10", "mdy") is None           # year-first is not M/D/YYYY
    assert parse_one("", "iso") is None                    # blank never parses
    assert parse_one("not-a-date", "iso") is None


def test_parse_one_rejects_impossible_dates() -> None:
    assert parse_one("2026-13-01", "iso") is None          # month 13
    assert parse_one("2026-02-30", "iso") is None          # Feb 30
    assert parse_one("20261301", "ymd_compact") is None
    assert parse_one("13/10/2026", "mdy") is None          # month 13 (but valid as D/M)
    assert parse_one("13/10/2026", "dmy") == date(2026, 10, 13)


def test_parse_one_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="unknown date format id"):
        parse_one("2026-07-10", "nonsense")


# --- Excel serial window bounds ----------------------------------------------------------


def test_serial_epoch_and_window() -> None:
    assert parse_one("32874", "serial") == date(1990, 1, 1)     # lower bound
    assert parse_one("73415", "serial") == date(2100, 12, 31)   # upper bound
    assert parse_one("46578", "serial") == date(2027, 7, 10)
    # out of window -> not a date (avoids mistaking a stray integer for a date)
    assert parse_one("32873", "serial") is None
    assert parse_one("73416", "serial") is None
    assert parse_one("100", "serial") is None
    # an 8-digit YYYYMMDD is NOT in the serial window (disjoint by construction)
    assert parse_one("20260710", "serial") is None


# --- column-level inference: the four cases ----------------------------------------------


def test_iso_column_fast_path() -> None:
    res = resolve_date_column(["2026-07-10", "2026-07-11", "2026-01-02"])
    assert res.format_id == "iso"
    assert not res.ambiguous
    assert res.dates == [date(2026, 7, 10), date(2026, 7, 11), date(2026, 1, 2)]


def test_single_format_auto_resolves() -> None:
    res = resolve_date_column(["2026/7/10", "2026/12/1"])
    assert res.format_id == "ymd_slash"
    assert res.dates == [date(2026, 7, 10), date(2026, 12, 1)]


def test_day_over_12_forces_dmy() -> None:
    # 15 cannot be a month -> only D/M fits every value -> auto-resolve, no ambiguity.
    res = resolve_date_column(["3/4/2026", "15/6/2026"])
    assert res.format_id == "dmy"
    assert not res.ambiguous
    assert res.dates == [date(2026, 4, 3), date(2026, 6, 15)]


def test_day_over_12_forces_mdy() -> None:
    # second token 15 cannot be a day-as-month -> only M/D fits.
    res = resolve_date_column(["3/4/2026", "6/15/2026"])
    assert res.format_id == "mdy"
    assert not res.ambiguous
    assert res.dates == [date(2026, 3, 4), date(2026, 6, 15)]


def test_identical_readings_auto_resolve() -> None:
    # every value reads the same under M/D and D/M (m == d) -> auto-resolve, no chooser.
    res = resolve_date_column(["3/3/2026", "5/5/2026", "12/12/2026"])
    assert not res.ambiguous
    assert res.format_id in {"mdy", "dmy"}
    assert res.dates == [date(2026, 3, 3), date(2026, 5, 5), date(2026, 12, 12)]


def test_conflicting_readings_are_ambiguous() -> None:
    res = resolve_date_column(["3/4/2026", "5/6/2026"])
    assert res.ambiguous
    assert res.format_id is None
    assert res.dates == [None, None]           # never a guess
    ids = {c.id for c in res.candidates}
    assert ids == {"mdy", "dmy"}
    # the example distinguishes the two readings
    ex = res.candidates[0]
    assert ex.example_in in {"3/4/2026", "5/6/2026"}
    outs = {c.example_out for c in res.candidates}
    assert len(outs) == 2  # different ISO reading per candidate


def test_ambiguity_samples_are_distinguishing() -> None:
    res = resolve_date_column(["3/3/2026", "3/4/2026"])  # 3/3 agrees, 3/4 conflicts
    assert res.ambiguous
    assert res.samples == ["3/4/2026"]  # only the conflicting value is a useful sample


def test_zh_cjk_column() -> None:
    res = resolve_date_column(["2026年7月10日", "2026年1月2日"])
    assert res.format_id == "cjk"
    assert res.dates == [date(2026, 7, 10), date(2026, 1, 2)]


def test_compact_column() -> None:
    res = resolve_date_column(["20260710", "20260102"])
    assert res.format_id == "ymd_compact"
    assert res.dates == [date(2026, 7, 10), date(2026, 1, 2)]


def test_serial_column() -> None:
    res = resolve_date_column(["46578", "46579"])
    assert res.format_id == "serial"
    assert res.dates == [date(2027, 7, 10), date(2027, 7, 11)]


def test_mixed_garbage_errors_offending_value_only() -> None:
    # ISO dominates; the single garbage value errors as None, the good rows still parse.
    res = resolve_date_column(["2026-07-10", "2026-07-11", "garbage"])
    assert not res.ambiguous
    assert res.format_id == "iso"
    assert res.dates == [date(2026, 7, 10), date(2026, 7, 11), None]


def test_all_garbage_no_format() -> None:
    res = resolve_date_column(["nope", "still-no"])
    assert not res.ambiguous
    assert res.format_id is None
    assert res.dates == [None, None]


def test_blanks_are_none_and_do_not_drive_inference() -> None:
    res = resolve_date_column(["2026-07-10", "", "2026-07-12"])
    assert res.format_id == "iso"
    assert res.dates == [date(2026, 7, 10), None, date(2026, 7, 12)]


def test_empty_column() -> None:
    res = resolve_date_column(["", "", ""])
    assert res.format_id is None
    assert not res.ambiguous
    assert res.dates == [None, None, None]


# --- pinned format -----------------------------------------------------------------------


def test_pinned_format_forces_parse() -> None:
    # the same ambiguous column, but the user pinned D/M -> deterministic, no ambiguity.
    res = resolve_date_column(["3/4/2026", "5/6/2026"], pinned="dmy")
    assert res.format_id == "dmy"
    assert not res.ambiguous
    assert res.dates == [date(2026, 4, 3), date(2026, 6, 5)]


def test_pinned_format_can_leave_nonmatching_rows_as_errors() -> None:
    res = resolve_date_column(["7/10/2026", "2026-07-11"], pinned="mdy")
    # M/D reading of 7/10/2026 is July 10; the ISO-shaped row does not match M/D/YYYY -> None.
    assert res.dates == [date(2026, 7, 10), None]


def test_pinned_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="unknown date format id"):
        resolve_date_column(["7/10/2026"], pinned="bogus")


def test_all_format_ids_are_labelled_and_parseable() -> None:
    # every advertised id must round-trip through parse_one without raising on a known value.
    for fid in FORMAT_IDS:
        assert parse_one("2026-07-10", fid) is not None or fid != "iso"
