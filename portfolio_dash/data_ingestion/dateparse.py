"""Column-level date parsing for CSV imports — accepts common shapes, never guesses.

Excel silently reformats an ISO date (``2026-07-10``) into a locale string (``7/10/2026``)
or an integer serial the moment a user opens a template, so an import must accept the common
shapes.  The catch: ``M/D`` and ``D/M`` share the same syntax, so a column like ``3/4/2026``
is genuinely ambiguous.  This module infers ONE format for the whole column from the evidence
in *all* its values (a value with a day > 12 rules a reading out), and when a real ambiguity
survives it refuses to guess — the caller surfaces a chooser and pins the format.

Pure + fully typed.  ``resolve_date_column`` does the column-level inference; ``parse_one``
parses a single value under a named format id.

Design note — why the ONLY ambiguity is M/D vs D/M: every other format's syntax is disjoint
(ISO has ``-``; ``YYYY/M/D`` leads with a 4-digit year; ``YYYY.M.D`` has ``.``; ``YYYYMMDD``
is exactly 8 digits; the Excel serial is a 5-digit integer in a bounded window; the CJK form
has 年月日).  So at most one non-locale format can parse any given value, and the two locale
slash forms are the sole pair that can read the SAME value two different ways.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# Stable format ids (wire-visible: the frontend chooser passes one back as ``date_format``).
FORMAT_IDS: tuple[str, ...] = (
    "iso", "ymd_slash", "ymd_dot", "ymd_compact", "cjk", "serial", "mdy", "dmy",
)

_LABELS: dict[str, str] = {
    "iso": "西元-月-日（YYYY-MM-DD）",
    "ymd_slash": "西元/月/日（YYYY/M/D）",
    "ymd_dot": "西元.月.日（YYYY.M.D）",
    "ymd_compact": "西元月日八碼（YYYYMMDD）",
    "cjk": "西元年月日（YYYY年M月D日）",
    "serial": "Excel 日期序號",
    "mdy": "月/日/年（美式 M/D/YYYY）",
    "dmy": "日/月/年（歐式 D/M/YYYY）",
}

# Priority when several single-syntax formats tie in a mixed column, and for the identical-
# readings auto-resolve: ISO-like first, the locale-ambiguous slash forms last.
_CANONICAL_ORDER: tuple[str, ...] = (
    "iso", "ymd_slash", "ymd_dot", "ymd_compact", "cjk", "serial", "mdy", "dmy",
)

# Excel serial epoch quirk: day 0 = 1899-12-30. The window ≈ 1990-01-01 .. 2100-12-31 keeps a
# stray 5-digit integer from being mistaken for a date and keeps YYYYMMDD (8 digits) disjoint.
_EXCEL_EPOCH = date(1899, 12, 30)
_SERIAL_MIN = 32874   # 1990-01-01
_SERIAL_MAX = 73415   # 2100-12-31

_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_YMD_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_RE_YMD_DOT = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$")
_RE_YMD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_RE_CJK = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
_RE_SLASH_YEAR_LAST = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_RE_DIGITS = re.compile(r"^\d+$")


def format_label(fmt_id: str) -> str:
    """Human (zh-TW) label for a format id; the id itself if unknown."""
    return _LABELS.get(fmt_id, fmt_id)


def _mk(y: int, m: int, d: int) -> date | None:
    """``date(y, m, d)`` or None if the components are out of range."""
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_one(value: str, fmt_id: str) -> date | None:
    """Parse *value* under the named format id; None when it does not match/validate.

    Raises ``ValueError`` only for an unknown *fmt_id* (a programming/tamper error).
    """
    s = value.strip()
    if not s:
        return None
    if fmt_id == "iso":
        m = _RE_ISO.match(s)
        return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None
    if fmt_id == "ymd_slash":
        m = _RE_YMD_SLASH.match(s)
        return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None
    if fmt_id == "ymd_dot":
        m = _RE_YMD_DOT.match(s)
        return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None
    if fmt_id == "ymd_compact":
        m = _RE_YMD_COMPACT.match(s)
        return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None
    if fmt_id == "cjk":
        m = _RE_CJK.match(s)
        return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None
    if fmt_id == "serial":
        if not _RE_DIGITS.match(s):
            return None
        n = int(s)
        return _EXCEL_EPOCH + timedelta(days=n) if _SERIAL_MIN <= n <= _SERIAL_MAX else None
    if fmt_id == "mdy":
        m = _RE_SLASH_YEAR_LAST.match(s)
        return _mk(int(m[3]), int(m[1]), int(m[2])) if m else None
    if fmt_id == "dmy":
        m = _RE_SLASH_YEAR_LAST.match(s)
        return _mk(int(m[3]), int(m[2]), int(m[1])) if m else None
    raise ValueError(f"unknown date format id: {fmt_id!r}")


@dataclass(frozen=True)
class DateCandidate:
    """One interpretation offered to the user when a column is ambiguous."""

    id: str
    label: str
    example_in: str    # a raw value from the column that distinguishes the candidates
    example_out: str   # its ISO reading under THIS candidate


@dataclass(frozen=True)
class DateColumnResult:
    """Outcome of :func:`resolve_date_column`.

    ``dates`` has one entry per input value (None = unparseable under the resolved format, or
    all-None when ``ambiguous``).  ``format_id`` is the resolved/pinned format (None when
    ambiguous or when the column is empty).  When ``ambiguous`` the caller must NOT write —
    it surfaces ``candidates`` + ``samples`` and asks the user to pin a format.
    """

    dates: list[date | None]
    format_id: str | None
    ambiguous: bool
    candidates: list[DateCandidate] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


def _canonical_pick(ids: list[str]) -> str:
    for fid in _CANONICAL_ORDER:
        if fid in ids:
            return fid
    return ids[0]


def _resolved(stripped: list[str], fid: str) -> DateColumnResult:
    dates = [parse_one(v, fid) if v else None for v in stripped]
    return DateColumnResult(dates=dates, format_id=fid, ambiguous=False)


def _all_identical(nonblank: list[str], ids: list[str]) -> bool:
    """True when every value reads to the SAME date under all *ids* (e.g. 3/3 both ways)."""
    for v in nonblank:
        if len({parse_one(v, fid) for fid in ids}) > 1:
            return False
    return True


def _distinguishing_samples(nonblank: list[str], ids: list[str], limit: int = 5) -> list[str]:
    diff = [
        v for v in nonblank
        if len({d for fid in ids if (d := parse_one(v, fid)) is not None}) > 1
    ]
    return (diff or nonblank)[:limit]


def _ambiguous(stripped: list[str], nonblank: list[str], ids: list[str]) -> DateColumnResult:
    samples = _distinguishing_samples(nonblank, ids)
    example = samples[0]
    candidates = [
        DateCandidate(
            id=fid, label=format_label(fid), example_in=example,
            example_out=(d.isoformat() if (d := parse_one(example, fid)) is not None else ""),
        )
        for fid in ids
    ]
    # Never guess: leave every date unresolved until the user pins a format.
    return DateColumnResult(
        dates=[None] * len(stripped), format_id=None, ambiguous=True,
        candidates=candidates, samples=samples,
    )


def _resolve_mixed(stripped: list[str], nonblank: list[str]) -> DateColumnResult:
    """No single format fits every value (mixed / malformed column).

    Pick the best-covering format so the good rows still parse and the offending values are
    reported per row — EXCEPT when the two locale slash forms tie at the top and disagree on a
    shared value, which is a genuine M/D-vs-D/M question that must be asked, not guessed.
    """
    coverage = {
        fid: sum(1 for v in nonblank if parse_one(v, fid) is not None) for fid in FORMAT_IDS
    }
    top = max(coverage.values())
    if top == 0:
        # Nothing parses under any known format -> every row errors, no format inferred.
        return DateColumnResult(dates=[None] * len(stripped), format_id=None, ambiguous=False)
    contenders = [fid for fid in FORMAT_IDS if coverage[fid] == top]
    if "mdy" in contenders and "dmy" in contenders:
        shared = [
            v for v in nonblank
            if parse_one(v, "mdy") is not None and parse_one(v, "dmy") is not None
        ]
        if any(parse_one(v, "mdy") != parse_one(v, "dmy") for v in shared):
            return _ambiguous(stripped, nonblank, ["mdy", "dmy"])
    return _resolved(stripped, _canonical_pick(contenders))


def resolve_date_column(values: list[str], *, pinned: str | None = None) -> DateColumnResult:
    """Infer ONE date format for a whole column and parse every value under it.

    Cases (spec FU-D19): exactly one format fits every value -> auto; several fit with
    identical readings -> auto (canonical); several fit with conflicting readings -> AMBIGUOUS
    (candidates returned, nothing parsed); no format fits some values -> per-row errors on the
    offending values.  ISO input takes a zero-overhead fast path with identical behaviour.

    A non-None *pinned* format id (from the frontend chooser / a re-validated commit) skips
    inference entirely and parses every value under that format.
    """
    stripped = [v.strip() for v in values]
    nonblank = [v for v in stripped if v]

    if pinned is not None:
        if pinned not in FORMAT_IDS:
            raise ValueError(f"unknown date format id: {pinned!r}")
        return _resolved(stripped, pinned)

    if not nonblank:
        return DateColumnResult(dates=[None] * len(stripped), format_id=None, ambiguous=False)

    # ISO fast path — the overwhelmingly common case, behaviour identical to date.fromisoformat.
    if all(parse_one(v, "iso") is not None for v in nonblank):
        return _resolved(stripped, "iso")

    # Formats that parse EVERY value (by construction: a single format, or exactly {mdy, dmy}).
    full_fit = [
        fid for fid in FORMAT_IDS if all(parse_one(v, fid) is not None for v in nonblank)
    ]
    if len(full_fit) == 1:
        return _resolved(stripped, full_fit[0])
    if len(full_fit) >= 2:
        if _all_identical(nonblank, full_fit):
            return _resolved(stripped, _canonical_pick(full_fit))
        return _ambiguous(stripped, nonblank, full_fit)

    return _resolve_mixed(stripped, nonblank)
