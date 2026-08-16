"""One type judgement for the intake door and the wire (AI-3).

The door sniffed a screenshot's real format to decide whether to accept it, and the transport
then labelled every accepted image ``data:image/png``. The app identified the type correctly
and told the provider something else. These tests pin the two halves to the same function.
"""

import base64

from portfolio_dash.shared.image_types import (
    JPEG,
    PNG,
    WEBP,
    is_supported_image,
    sniff_image_mime,
)
from portfolio_dash.shared.llm import _build_messages

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


def test_each_signature_reports_its_own_mime() -> None:
    assert sniff_image_mime(_PNG_BYTES) == PNG
    assert sniff_image_mime(_JPEG_BYTES) == JPEG
    assert sniff_image_mime(_WEBP_BYTES) == WEBP


def test_a_non_image_is_none_and_unsupported() -> None:
    assert sniff_image_mime(b"%PDF-1.7\n") is None
    assert sniff_image_mime(b"") is None
    # A RIFF container that is NOT WebP (e.g. a WAV) must not pass as an image.
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 32) is None
    assert not is_supported_image(b"%PDF-1.7\n")


def test_a_jpeg_goes_out_labelled_as_a_jpeg() -> None:
    """★ AI-3 disproof: this said ``data:image/png`` for every payload."""
    messages = _build_messages("讀這張對帳單", [_JPEG_BYTES])
    content = messages[0]["content"]
    assert isinstance(content, list)
    url = content[1]["image_url"]["url"]
    assert url.startswith(f"data:{JPEG};base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == _JPEG_BYTES


def test_mixed_formats_keep_their_own_labels() -> None:
    """Four screenshots pasted from four places is the ordinary case, not the exotic one."""
    messages = _build_messages("x", [_PNG_BYTES, _JPEG_BYTES, _WEBP_BYTES])
    content = messages[0]["content"]
    assert isinstance(content, list)
    mimes = [c["image_url"]["url"].split(";", 1)[0] for c in content[1:]]
    assert mimes == [f"data:{PNG}", f"data:{JPEG}", f"data:{WEBP}"]


def test_an_unrecognised_payload_still_produces_a_message() -> None:
    """The door rejects non-images, so this branch means a direct caller skipped it. A wrong
    label beats no message: degrade, do not raise."""
    messages = _build_messages("x", [b"not an image at all"])
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[1]["image_url"]["url"].startswith(f"data:{PNG};base64,")


def test_no_images_stays_a_plain_string_message() -> None:
    assert _build_messages("hello", None) == [{"role": "user", "content": "hello"}]
    assert _build_messages("hello", []) == [{"role": "user", "content": "hello"}]
