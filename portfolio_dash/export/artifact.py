"""Export artifact value object + CSV/zip writers.

Reconciliation-grade output: UTF-8 *with BOM*, CRLF line endings, and raw cell
strings (no rounding/thousands separators — callers pass full-precision Decimal
strings). Display-value export is the frontend's job; this module is the audit file.
"""

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

_BOM = "\ufeff"
# Characters kept verbatim in the ASCII `filename=` fallback; everything else (quotes,
# CR/LF, ';', spaces, non-ASCII) is replaced with '_' so a user-derived name (symbol /
# date range) can never inject a header or crash latin-1 header encoding.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def content_disposition(filename: str) -> str:
    """A safe ``attachment`` Content-Disposition value for *filename*.

    Filename components can be user-derived (a symbol, a date range) and must never break
    the response header. Emit an ASCII-only ``filename=`` fallback (unsafe bytes stripped)
    PLUS an RFC 5987 ``filename*=UTF-8''`` copy carrying the full Unicode name. This blocks
    CRLF header injection and the latin-1 encode crash Starlette raises on a non-ASCII
    header value, while a compliant client (incl. ``web/api.js``) still recovers the exact
    name from ``filename*``.
    """
    ascii_fallback = _SAFE_FILENAME_RE.sub("_", filename.encode("ascii", "ignore").decode())
    if not ascii_fallback.strip("_"):
        ascii_fallback = "download"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


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
