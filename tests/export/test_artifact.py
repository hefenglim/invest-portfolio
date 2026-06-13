import csv
import io
import zipfile

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact, zip_artifact


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


def test_zip_round_trips_named_files() -> None:
    art = zip_artifact("bundle.zip", {"a.csv": b"hi", "m.json": b"{}"})
    assert art.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        assert set(zf.namelist()) == {"a.csv", "m.json"}
        assert zf.read("a.csv") == b"hi"
