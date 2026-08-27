"""F-18 disposition: the sweep's 「永久移除 is still pressable」 reading does not hold.

The 2026-08-27 sweep recorded, as an observation, that the 移除 dialog explains a symbol
cannot be permanently removed and yet leaves 永久移除 pressable — evidence being a
``DELETE /api/instruments/2330 -> 422``.

Re-checked here, and the premise is wrong on both halves:

* the 422 it saw came from **移除（隱藏）**, not from purge. Hiding is ``DELETE``; purging is
  ``POST …/purge``. The backend refuses to archive a HELD symbol with code ``held``, and
  ``web/instruments.js`` catches exactly that code and opens a 「無法移除」 dialog — the
  designed path, not a dead button.
* 永久移除 is genuinely disabled whenever the warning is shown. ``purgeBtn.disabled`` starts
  ``true`` and is cleared ONLY by the type-to-confirm input's handler, and that input is not
  created at all on the ``has_history`` branch — so nothing can clear it. The click handler
  additionally returns early on ``disabled``.

Nothing was changed for F-18. This test pins the two API facts the conclusion rests on, so
the next reader of that report does not have to re-derive them.
"""

from fastapi.testclient import TestClient


def test_a_symbol_with_ledger_history_is_flagged_as_such(api_client: TestClient) -> None:
    """`has_history` is what makes the dialog hide the type-to-confirm input entirely."""
    rows = api_client.get("/api/instruments").json()["list"]
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["2330"]["has_history"] is True


def test_hiding_a_held_symbol_is_refused_with_the_code_the_ui_handles(
    api_client: TestClient
) -> None:
    """DELETE is the HIDE path. Its 422 is the one the sweep saw and attributed to purge."""
    r = api_client.delete("/api/instruments/2330")
    assert r.status_code == 422
    # `held`, not `has_history` — the frontend branches on this exact code to raise its
    # 「無法移除」 dialog instead of a generic failure toast.
    assert r.json()["error"]["code"] == "held"


def test_purging_a_symbol_with_history_is_refused_by_the_backend_too(
    api_client: TestClient
) -> None:
    """Defence in depth: even if the disabled button were bypassed, the server refuses."""
    r = api_client.post("/api/instruments/2330/purge", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"has_history", "held"}
