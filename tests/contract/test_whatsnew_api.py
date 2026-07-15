"""Contract: GET/POST /api/whats-new (per-feature seen) + GET /api/whats-new/history (WP-WN)."""

from typing import Any

from fastapi.testclient import TestClient

import portfolio_dash
from portfolio_dash.shared.whatsnew import _version_key

_KEYS = {"current_version", "unseen_count", "versions"}
_FEAT_KEYS = {"key", "id", "title", "desc", "href", "area", "target", "seen"}


def _assert_shape(body: dict[str, object]) -> None:
    assert set(body.keys()) == _KEYS
    assert isinstance(body["current_version"], str)
    assert isinstance(body["unseen_count"], int)
    assert isinstance(body["versions"], list)
    for grp in body["versions"]:
        assert set(grp.keys()) == {"version", "date", "unseen", "features"}
        assert isinstance(grp["version"], str)
        assert grp["date"] is None or isinstance(grp["date"], str)
        assert isinstance(grp["unseen"], bool)
        for feat in grp["features"]:
            assert set(feat.keys()) == _FEAT_KEYS
            assert feat["key"] == f"{grp['version']}:{feat['id']}"
            assert isinstance(feat["seen"], bool)
            assert feat["href"] is None or isinstance(feat["href"], str)
            # target: optional CSS selector (string-or-null); when set it is non-empty and
            # only ever accompanies an href (presentation metadata, not standalone).
            assert feat["target"] is None or (
                isinstance(feat["target"], str) and bool(feat["target"])
            )
            if feat["target"] is not None:
                assert feat["href"] is not None


def _all_feature_keys(body: dict[str, Any]) -> list[str]:
    return [f["key"] for g in body["versions"] for f in g["features"]]


def test_get_shape_and_default_unseen_math(api_client: TestClient) -> None:
    r = api_client.get("/api/whats-new")
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)
    assert body["current_version"] == portfolio_dash.__version__
    assert "seen_version" not in body  # dropped in round 3
    # Version-agnostic: nothing ABOVE the running version is ever visible (an unshipped
    # catalog entry stays hidden until the release bump makes it current).
    versions = [g["version"] for g in body["versions"]]
    current_key = _version_key(portfolio_dash.__version__)
    assert all(_version_key(v) <= current_key for v in versions)
    assert versions == sorted(versions, key=_version_key, reverse=True)  # newest first
    # fresh install: every feature unseen; count == total visible features.
    assert all(g["unseen"] for g in body["versions"])
    assert all(not f["seen"] for g in body["versions"] for f in g["features"])
    total = sum(len(g["features"]) for g in body["versions"])
    assert body["unseen_count"] == total > 0


def test_settings_features_carry_seeded_targets(api_client: TestClient) -> None:
    # The in-page callout/flash points at a precise element per feature. The SPECIFIC
    # seeded selectors are asserted against the CATALOG (version-independent — wire
    # assertions on named features age out as the 6-version panel window slides past
    # their release; that broke this test at the 0.1.19 bump). The wire keeps the
    # generic round-4 guarantee: every href feature in the payload has a target.
    from portfolio_dash.shared.whatsnew import CATALOG

    cat = {f.id: f for f in CATALOG}
    assert cat["market-risk-alerts"].target == "#alert-rules-wrap"
    assert cat["target-weights"].target == "#target-weights-panel"
    assert cat["push-channels"].target == ".nt-cards"
    assert cat["quiet-hours"].target == "#nt-qh-enabled"
    assert cat["per-rule-subscriptions"].target == "#nt-subs"
    # a non-settings feature (instruments page) carries its own stable panel selector.
    assert cat["rules-engine"].target == 'section[data-screen-label="標的清單"]'
    # the round-4 guarantee, enforced end-to-end on the live wire: every href feature
    # currently in the panel window has a non-empty target.
    body = api_client.get("/api/whats-new").json()
    feats = {f["id"]: f for g in body["versions"] for f in g["features"]}
    assert feats, "panel window unexpectedly empty"
    for f in feats.values():
        if f["href"] is not None:
            assert f["target"] and f["target"].strip(), f["id"]


def test_post_all_marks_every_feature_seen(api_client: TestClient) -> None:
    r = api_client.post("/api/whats-new/seen", json={"all": True})
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)  # POST returns the same shape as GET
    assert body["unseen_count"] == 0
    assert all(not g["unseen"] for g in body["versions"])
    assert all(f["seen"] for g in body["versions"] for f in g["features"])
    # persisted: a fresh GET reflects it.
    after = api_client.get("/api/whats-new").json()
    assert after["unseen_count"] == 0


def test_post_features_marks_only_those(api_client: TestClient) -> None:
    before = api_client.get("/api/whats-new").json()
    keys = _all_feature_keys(before)
    target = keys[0]  # the newest group's first feature
    r = api_client.post("/api/whats-new/seen", json={"features": [target]})
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)
    # exactly one feature flipped to seen; the count dropped by exactly one.
    assert body["unseen_count"] == before["unseen_count"] - 1
    seen_map = {f["key"]: f["seen"] for g in body["versions"] for f in g["features"]}
    assert seen_map[target] is True
    assert sum(1 for v in seen_map.values() if v) == 1
    # its group is still unseen (sibling features remain unread).
    grp0 = body["versions"][0]
    assert grp0["unseen"] is True
    # persisted across a fresh GET.
    after = api_client.get("/api/whats-new").json()
    after_seen = {f["key"]: f["seen"] for g in after["versions"] for f in g["features"]}
    assert after_seen[target] is True


def test_post_features_is_idempotent(api_client: TestClient) -> None:
    keys = _all_feature_keys(api_client.get("/api/whats-new").json())
    target = keys[0]
    first = api_client.post("/api/whats-new/seen", json={"features": [target]}).json()
    second = api_client.post("/api/whats-new/seen", json={"features": [target]}).json()
    assert first["unseen_count"] == second["unseen_count"]


def test_post_unknown_key_400_writes_nothing(api_client: TestClient) -> None:
    before = api_client.get("/api/whats-new").json()
    # an unknown key and an out-of-window (backfilled) key both fail; nothing is written.
    for bad in (["9.9.9:nope"], ["0.1.0:dashboard-launch"], [_all_feature_keys(before)[0], "x:y"]):
        r = api_client.post("/api/whats-new/seen", json={"features": bad})
        assert r.status_code == 400, bad
        err = r.json()["error"]
        assert err["code"] == "validation_error"
        assert err["field"] == "features"
    # nothing written on any refusal — the count is unchanged.
    after = api_client.get("/api/whats-new").json()
    assert after["unseen_count"] == before["unseen_count"]


# --- history browser --------------------------------------------------------


def _assert_history_shape(body: dict[str, object]) -> None:
    assert set(body.keys()) == {"total", "offset", "versions"}
    assert isinstance(body["total"], int)
    assert isinstance(body["offset"], int)
    assert isinstance(body["versions"], list)
    for grp in body["versions"]:
        assert set(grp.keys()) == {"version", "date", "features"}
        assert grp["date"] is None or isinstance(grp["date"], str)
        for feat in grp["features"]:
            # history features carry NO seen-state, key, or href — just the user copy.
            assert set(feat.keys()) == {"title", "desc", "area"}


def test_history_shape_and_full_coverage(api_client: TestClient) -> None:
    r = api_client.get("/api/whats-new/history", params={"offset": 0, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    _assert_history_shape(body)
    assert body["offset"] == 0
    # history pages the FULL catalog (uncapped) — strictly more than the ✦ panel's 6.
    assert body["total"] > 6
    versions = [g["version"] for g in body["versions"]]
    assert versions == sorted(versions, key=_version_key, reverse=True)
    # nothing above the running version appears here either (version-agnostic).
    current_key = _version_key(portfolio_dash.__version__)
    assert all(_version_key(v) <= current_key for v in versions)


def test_history_paging_stitches_without_overlap_or_gap(api_client: TestClient) -> None:
    limit = 5
    p1 = api_client.get("/api/whats-new/history", params={"offset": 0, "limit": limit}).json()
    p2 = api_client.get("/api/whats-new/history", params={"offset": limit, "limit": limit}).json()
    assert p1["total"] == p2["total"]  # total is constant across pages
    v1 = [g["version"] for g in p1["versions"]]
    v2 = [g["version"] for g in p2["versions"]]
    assert len(v1) == limit  # a full first page
    stitched = v1 + v2
    assert len(set(stitched)) == len(stitched)  # no overlap, no dup
    # contiguous + strictly newest-first across the seam (no gap).
    assert stitched == sorted(stitched, key=_version_key, reverse=True)


def test_history_offset_past_end_is_empty_not_error(api_client: TestClient) -> None:
    total = api_client.get("/api/whats-new/history").json()["total"]
    body = api_client.get("/api/whats-new/history", params={"offset": total, "limit": 5}).json()
    assert body["versions"] == []
    assert body["total"] == total


def test_history_validation_400s(api_client: TestClient) -> None:
    for params, field in (
        ({"offset": -1, "limit": 5}, "offset"),
        ({"offset": 0, "limit": 0}, "limit"),
        ({"offset": 0, "limit": 21}, "limit"),
    ):
        r = api_client.get("/api/whats-new/history", params=params)
        assert r.status_code == 400, params
        err = r.json()["error"]
        assert err["code"] == "validation_error"
        assert err["field"] == field
