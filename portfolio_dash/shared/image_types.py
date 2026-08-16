"""Image magic-byte sniffing: ONE type judgement, shared by the door and the transport.

There were two, and they disagreed. ``api/routers/input_center`` sniffed an uploaded
screenshot's real format (PNG / JPEG / WebP) to reject non-images before they ever reached a
vision model — and then ``shared/llm._build_messages`` labelled every one of them
``data:image/png`` on the wire. The app identified the type correctly and then told the
provider something else. Lenient providers sniff the bytes and shrug; a strict one rejects a
JPEG that claims to be a PNG, and the failure surfaces as a generic parse/vision error far
from its cause.

A client-declared MIME is never trusted here: the bytes decide. Both callers go through
:func:`sniff_image_mime`, so "is this an image?" and "what do we call it?" can no longer be
answered differently.
"""

from typing import Final

#: The formats the vision path accepts. PNG and JPEG cover every screenshot tool the owner is
#: likely to use; WebP is what a browser hands over when a screenshot is pasted from a web page.
PNG: Final = "image/png"
JPEG: Final = "image/jpeg"
WEBP: Final = "image/webp"

SUPPORTED_IMAGE_MIMES: Final[tuple[str, ...]] = (PNG, JPEG, WEBP)


def sniff_image_mime(data: bytes) -> str | None:
    """Return the MIME type implied by *data*'s magic bytes, or ``None`` if unrecognised.

    ``None`` means "not one of the formats we accept" — it is the rejection signal at the
    door and, at the transport, the reason to fall back rather than to guess.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    # RIFF containers carry their format in bytes 8-12; only the WEBP flavour is an image.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def is_supported_image(data: bytes) -> bool:
    """True when *data* opens with a supported image signature."""
    return sniff_image_mime(data) is not None
