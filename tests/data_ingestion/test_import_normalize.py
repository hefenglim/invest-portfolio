"""Unit tests for the import-seam normalization (FU-D19): header canonicalization + the
CSV date-column rewrite that feeds the ISO-only per-kind builders."""

from portfolio_dash.data_ingestion.csv_import import (
    canonical_header,
    normalize_import_csv,
)

_BOM = "\ufeff"


# --- canonical_header --------------------------------------------------------------------


def test_canonical_header_strips_halfwidth_annotation() -> None:
    assert canonical_header("date(YYYY-MM-DD)") == "date"
    assert canonical_header("fee(選填)") == "fee"


def test_canonical_header_strips_fullwidth_annotation() -> None:
    assert canonical_header("fee（選填）") == "fee"
    assert canonical_header("build_date（YYYY-MM-DD）") == "build_date"


def test_canonical_header_lowercases_and_trims_whitespace() -> None:
    assert canonical_header("  Account ") == "account"
    assert canonical_header("Symbol") == "symbol"
    assert canonical_header("From_CCY") == "from_ccy"


def test_canonical_header_strips_leading_bom() -> None:
    assert canonical_header(_BOM + "account") == "account"
    assert canonical_header(_BOM + "date(YYYY-MM-DD)") == "date"


def test_canonical_header_plain_is_unchanged() -> None:
    for name in ("account", "symbol", "side", "shares", "price", "build_date"):
        assert canonical_header(name) == name


# --- normalize_import_csv ----------------------------------------------------------------


def test_normalize_canonicalizes_annotated_headers() -> None:
    text = "account,date(YYYY-MM-DD),fee(選填)\r\ntw_broker,2026-07-10,20\r\n"
    out = normalize_import_csv(text, "date")
    assert out.ambiguity is None
    header = out.text.splitlines()[0]
    assert header == "account,date,fee"


def test_normalize_rewrites_slash_dates_to_iso() -> None:
    text = "account,date\r\ntw_broker,2026/7/10\r\ntw_broker,2026/12/1\r\n"
    out = normalize_import_csv(text, "date")
    assert out.ambiguity is None
    body = out.text.splitlines()
    assert body[1].endswith("2026-07-10")
    assert body[2].endswith("2026-12-01")


def test_normalize_iso_passthrough_is_unchanged_values() -> None:
    text = "account,date\r\ntw_broker,2026-07-10\r\n"
    out = normalize_import_csv(text, "date")
    assert out.ambiguity is None
    assert out.text.splitlines()[1] == "tw_broker,2026-07-10"


def test_normalize_ambiguous_column_reports_and_leaves_dates() -> None:
    text = "account,date\r\ntw_broker,3/4/2026\r\ntw_broker,5/6/2026\r\n"
    out = normalize_import_csv(text, "date")
    assert out.ambiguity is not None
    assert out.ambiguity.column == "date"
    assert {c.id for c in out.ambiguity.candidates} == {"mdy", "dmy"}
    # the date cells are left as-is (not guessed) so the ISO builder errors each row
    assert "3/4/2026" in out.text and "5/6/2026" in out.text


def test_normalize_pinned_format_resolves_ambiguity() -> None:
    text = "account,date\r\ntw_broker,3/4/2026\r\ntw_broker,5/6/2026\r\n"
    out = normalize_import_csv(text, "date", date_format="dmy")
    assert out.ambiguity is None
    body = out.text.splitlines()
    assert body[1].endswith("2026-04-03")  # D/M reading
    assert body[2].endswith("2026-06-05")


def test_normalize_openings_uses_build_date_column() -> None:
    text = "account,symbol,shares,original_cost_total,build_date(YYYY-MM-DD)\r\n" \
           "tw_broker,2330,1000,500000,2026/1/2\r\n"
    out = normalize_import_csv(text, "build_date")
    assert out.ambiguity is None
    lines = out.text.splitlines()
    assert lines[0].split(",")[-1] == "build_date"     # annotation stripped
    assert lines[1].endswith("2026-01-02")             # slash -> ISO


def test_normalize_bad_row_keeps_offending_value() -> None:
    text = "account,date\r\ntw_broker,2026-07-10\r\ntw_broker,garbage\r\n"
    out = normalize_import_csv(text, "date")
    assert out.ambiguity is None
    body = out.text.splitlines()
    assert body[1].endswith("2026-07-10")
    assert body[2].endswith("garbage")  # left for the builder to error with the value


def test_normalize_empty_text_is_noop() -> None:
    out = normalize_import_csv("", "date")
    assert out.ambiguity is None
    assert out.text == ""


def test_normalize_strips_leading_bom_on_first_header() -> None:
    text = _BOM + "account,date(YYYY-MM-DD)\r\ntw_broker,2026-07-10\r\n"
    out = normalize_import_csv(text, "date")
    assert out.text.splitlines()[0] == "account,date"
