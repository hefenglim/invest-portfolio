import csv
import io
import zipfile

from portfolio_dash.export.artifact import (
    ExportArtifact,
    content_disposition,
    csv_artifact,
    zip_artifact,
)


def test_csv_has_bom_and_crlf_and_raw_decimals() -> None:
    art = csv_artifact(
        "x.csv",
        header=["symbol", "shares"],
        rows=[["2330", "1000.000000"], ["AAPL", "10"]],
        footer_lines=["as_of=2026-06-11"],
    )
    assert isinstance(art, ExportArtifact)
    assert art.filename == "x.csv"
    assert art.media_type == "text/csv; charset=utf-8"
    assert art.content[:3] == b"\xef\xbb\xbf"          # UTF-8 BOM
    text = art.content[3:].decode("utf-8")
    assert "\r\n" in text                               # CRLF
    assert "1000.000000" in text                        # raw decimal, untouched
    assert "# as_of=2026-06-11\r\n" in text             # footer comment line
    body = text[: text.index("# ")]
    parsed = list(csv.reader(io.StringIO(body)))
    assert parsed[0] == ["symbol", "shares"]


def test_content_disposition_plain_ascii_filename() -> None:
    cd = content_disposition("2330_dividends.csv")
    assert cd == (
        "attachment; filename=\"2330_dividends.csv\"; "
        "filename*=UTF-8''2330_dividends.csv"
    )


def test_content_disposition_strips_crlf_and_quote_injection() -> None:
    """A user-derived filename component must not inject a header: CR/LF/quote never appear
    raw in the value (the ASCII fallback replaces them; filename* percent-encodes them)."""
    cd = content_disposition('X"\r\nSet-Cookie: a=b_dividends.csv')
    assert "\r" not in cd and "\n" not in cd
    # ASCII fallback part carries no raw double-quote past the opening filename=" quote.
    fallback = cd.split("filename=\"", 1)[1].split("\";", 1)[0]
    assert '"' not in fallback and ":" not in fallback
    assert "%0D%0A" in cd  # CRLF survives only percent-encoded, in filename*


def test_content_disposition_non_ascii_is_latin1_safe() -> None:
    """A non-ASCII (CJK) filename must not crash latin-1 header encoding: the value is pure
    ASCII, with the full name recoverable from the RFC 5987 filename* param."""
    cd = content_disposition("台積電_dividends.csv")
    cd.encode("latin-1")  # would raise if any non-ASCII leaked into the header value
    assert "filename*=UTF-8''" in cd
    assert "%E5%8F%B0" in cd  # percent-encoded CJK


def test_content_disposition_all_unsafe_falls_back_to_download() -> None:
    cd = content_disposition("台積電")  # no ASCII-safe chars -> generic fallback name
    assert 'filename="download"' in cd


def test_zip_round_trips_named_files() -> None:
    art = zip_artifact("bundle.zip", {"a.csv": b"hi", "m.json": b"{}"})
    assert art.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        assert set(zf.namelist()) == {"a.csv", "m.json"}
        assert zf.read("a.csv") == b"hi"
