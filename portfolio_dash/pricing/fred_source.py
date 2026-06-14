"""FRED macro-series client — pending (spec 20.9).

pending — validated when a key is entered (spec 20.9). A light, key-gated client for
FRED economic series (the macro variable's future data panel). ``available()`` is
False without a key and ``fetch_series`` short-circuits to ``None`` before any
network call, so it stays inert until a key is set. Numbers parse via
``Decimal(str(x))``; HTTP I/O goes through ``requests.get`` (never exercised unkeyed).
"""

import os
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import requests

_URL = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT_S = 20


class FredSource:
    """Key-gated FRED series client (macro; future panel)."""

    def __init__(
        self,
        token: str | None = None,
        *,
        token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._token = token if token is not None else os.environ.get("FRED_API_KEY")
        self._token_getter = token_getter

    def _resolve_token(self) -> str | None:
        if self._token_getter is not None:
            token = self._token_getter()
            if token:
                return token
        return self._token

    def available(self) -> bool:
        """True only when a key is configured (the registry/panel gate)."""
        return self._resolve_token() is not None

    def fetch_series(self, series_id: str) -> list[tuple[str, Decimal]] | None:
        """Latest observations for a FRED series, or ``None`` when no key is set.

        Returns ``(date, value)`` pairs with ``Decimal`` values (missing values, FRED's
        ``"."`` sentinel, are skipped). Never makes a request without a key.
        """
        token = self._resolve_token()
        if not token:
            return None
        resp = requests.get(
            _URL,
            params={"series_id": series_id, "api_key": token, "file_type": "json"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        out: list[tuple[str, Decimal]] = []
        for obs in resp.json().get("observations") or []:
            raw = obs.get("value")
            if raw in (None, "", "."):
                continue
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue
            out.append((obs.get("date", ""), value))
        return out
