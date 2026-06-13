from portfolio_dash.export.artifact import csv_blob


def test_csv_blob_has_bom_and_crlf() -> None:
    blob = csv_blob(["a", "b"], [["1", "2"]])
    assert blob[:3] == b"\xef\xbb\xbf"
    assert b"\r\n" in blob
