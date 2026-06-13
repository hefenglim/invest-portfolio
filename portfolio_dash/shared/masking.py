"""Single shared secret masker (review I-2).

One implementation for every place that displays an API key / token. Both
``pricing/datasources_store`` and ``api/routers/llm_settings`` delegate here so the
masking algorithm cannot drift again. The short-key guard is essential: without it a
key of length <= 6 would overlap its prefix and suffix slices (e.g. ``"abcde"`` ->
``"abc•••cde"``), re-exposing characters and lengthening the string.
"""


def mask_secret(value: str | None) -> str | None:
    """Mask a secret for display.

    - ``None`` or empty -> ``None`` (nothing to show).
    - ``len <= 6`` -> ``"•••"`` (fully masked; never partially reveal a short key).
    - otherwise -> ``prefix(3) + "•••" + suffix(3)``.
    """
    if not value:
        return None
    if len(value) <= 6:
        return "•••"
    return f"{value[:3]}•••{value[-3:]}"
