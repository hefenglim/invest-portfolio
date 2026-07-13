"""Contract: GET/POST /api/whats-new — the feature-announcement panel + seen-state (WP-WN)."""

from fastapi.testclient import TestClient

import portfolio_dash
from portfolio_dash.shared.whatsnew import _version_key

_KEYS = {"current_version", "seen_version", "unseen_count", "versions"}


def _assert_shape(body: dict[str, object]) -> None:
    assert set(body.keys()) == _KEYS
    assert isinstance(body["current_version"], str)
    assert isinstance(body["seen_version"], str)
    assert isinstance(body["unseen_count"], int)
    assert isinstance(body["versions"], list)
    for grp in body["versions"]:
        assert set(grp.keys()) == {"version", "date", "unseen", "features"}
        assert isinstance(grp["version"], str)
        assert grp["date"] is None or isinstance(grp["date"], str)
        assert isinstance(grp["unseen"], bool)
        for feat in grp["features"]:
            assert set(feat.keys()) == {"id", "title", "desc", "href", "area", "target"}
            assert feat["href"] is None or isinstance(feat["href"], str)
            # target: optional CSS selector (string-or-null); when set it is non-empty and
            # only ever accompanies an href (presentation metadata, not standalone).
            assert feat["target"] is None or (
                isinstance(feat["target"], str) and bool(feat["target"])
            )
            if feat["target"] is not None:
                assert feat["href"] is not None


def test_get_shape_and_default_unseen_math(api_client: TestClient) -> None:
    r = api_client.get("/api/whats-new")
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)
    assert body["current_version"] == portfolio_dash.__version__
    assert body["seen_version"] == "0"  # fresh install
    # v0.1.18 (the unshipped what's-new entry) stays hidden while current == 0.1.17.
    versions = [g["version"] for g in body["versions"]]
    assert "0.1.18" not in versions
    # newest first.
    assert versions == sorted(versions, key=_version_key, reverse=True)
    # with seen "0", every visible group is unseen and the count is their total features.
    assert all(g["unseen"] for g in body["versions"])
    expected = sum(len(g["features"]) for g in body["versions"] if g["unseen"])
    assert body["unseen_count"] == expected
    assert body["unseen_count"] > 0


def test_settings_features_carry_seeded_targets(api_client: TestClient) -> None:
    # The in-page callout/flash points at a precise element per feature; the settings-page
    # features carry the seeded selectors, and non-settings features stay target-null.
    body = api_client.get("/api/whats-new").json()
    feats = {f["id"]: f for g in body["versions"] for f in g["features"]}
    assert feats["market-risk-alerts"]["target"] == "#alert-rules-wrap"
    assert feats["target-weights"]["target"] == "#target-weights-panel"
    assert feats["push-channels"]["target"] == ".nt-cards"
    assert feats["quiet-hours"]["target"] == "#nt-qh-enabled"
    assert feats["per-rule-subscriptions"]["target"] == "#nt-subs"
    # a non-settings feature (instruments page) has no precise anchor yet.
    assert feats["rules-engine"]["target"] is None


def test_post_seen_round_trip_clears_unseen(api_client: TestClient) -> None:
    current = portfolio_dash.__version__
    r = api_client.post("/api/whats-new/seen", json={"version": current})
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)  # POST returns the same shape as GET
    assert body["seen_version"] == current
    assert body["unseen_count"] == 0
    assert all(not g["unseen"] for g in body["versions"])
    # a fresh GET reflects the persisted acknowledgement.
    after = api_client.get("/api/whats-new").json()
    assert after["seen_version"] == current
    assert after["unseen_count"] == 0


def test_post_lower_version_does_not_regress(api_client: TestClient) -> None:
    current = portfolio_dash.__version__
    api_client.post("/api/whats-new/seen", json={"version": current})
    # A lower version must not un-acknowledge newer features (monotonic).
    r = api_client.post("/api/whats-new/seen", json={"version": "0.1.13"})
    assert r.status_code == 200
    body = r.json()
    assert body["seen_version"] == current
    assert body["unseen_count"] == 0


def test_post_above_current_is_clamped(api_client: TestClient) -> None:
    # Acknowledging "beyond" the running version would permanently suppress the badge
    # for every FUTURE release; the router clamps the advance to current_version.
    current = portfolio_dash.__version__
    r = api_client.post("/api/whats-new/seen", json={"version": "999.0"})
    assert r.status_code == 200
    body = r.json()
    assert body["seen_version"] == current
    assert body["unseen_count"] == 0
    assert api_client.get("/api/whats-new").json()["seen_version"] == current


def test_post_bad_format_is_rejected(api_client: TestClient) -> None:
    r = api_client.post("/api/whats-new/seen", json={"version": "not-a-version"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["field"] == "version"
    # nothing written on refusal.
    assert api_client.get("/api/whats-new").json()["seen_version"] == "0"
