"""Export artifact value object + CSV/zip writers.

Reconciliation-grade output: UTF-8 *with BOM*, CRLF line endings, and raw cell
strings (no rounding/thousands separators — callers pass full-precision Decimal
strings). Display-value export is the frontend's job; this module is the audit file.
"""

import csv
import io
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_BOM = "\ufeff"


@dataclass(frozen=True)
class ExportArtifact:
    """A ready-to-serve download: filename, MIME type, and the bytes."""

    filename: str
    media_type: str
    content: bytes


def _csv_text(
    header: Sequence[str], rows: Iterable[Sequence[str]], footer_lines: Sequence[str]
) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    for line in footer_lines:
        buf.write(f"# {line}\r\n")
    return buf.getvalue()


def csv_artifact(
    filename: str,
    *,
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
    footer_lines: Sequence[str] = (),
) -> ExportArtifact:
    """Build a UTF-8-with-BOM, CRLF CSV artifact. Footer lines become `# ...` comments."""
    text = _BOM + _csv_text(header, rows, footer_lines)
    return ExportArtifact(filename, "text/csv; charset=utf-8", text.encode("utf-8"))


def csv_blob(
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
    footer_lines: Sequence[str] = (),
) -> bytes:
    """A standalone CSV byte blob (BOM + CRLF) for embedding inside a zip member."""
    return (_BOM + _csv_text(header, rows, footer_lines)).encode("utf-8")


def zip_artifact(filename: str, files: Mapping[str, bytes]) -> ExportArtifact:
    """Build a zip artifact from member name -> bytes (deterministic member order)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in files:  # insertion order; callers build dicts in stable order
            zf.writestr(name, files[name])
    return ExportArtifact(filename, "application/zip", buf.getvalue())
