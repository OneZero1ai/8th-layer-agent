"""Issue #22 — Bloom prefilter at DSN query time.

Pins:
  - bloom_contains/bloom_matches_any helpers in aigrp.py work on real blooms.
  - When DsnResolveRequest.query_domains is empty, behavior unchanged
    (no prefilter step in resolution_path).
  - When non-empty, peers whose Bloom doesn't claim ANY of the query
    domains are dropped before ranking; trace step records dropped count.
  - False-negative impossibility: a peer that DID add a domain is never
    dropped for that query.
  - Peers with no Bloom field (older signature shape) are kept — Bloom
    is only ever a tightener, never a hard gate.
"""

from __future__ import annotations

import base64
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server import aigrp, network
from cq_server.app import _get_store, app

ALICE = "alice"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _b64_centroid(axis: int, dim: int = 8) -> str:
    v = [0.0] * dim
    v[axis] = 1.0
    return base64.b64encode(_pack(v)).decode("ascii")


def _snap_with_bloom(
    *,
    slug: str,
    enterprise: str,
    group: str,
    axis: int,
    domains: list[str],
) -> network._L2Snapshot:
    s = network._L2Snapshot(
        slug=slug,
        enterprise=enterprise,
        group=group,
        endpoint=f"http://stub-{slug}.local",
        reachable=True,
    )
    s.peers = []
    bloom = aigrp.compute_domain_bloom(domains)
    s.signature = {
        "l2_id": f"{enterprise}/{group}",
        "ku_count": 5,
        "domain_count": len(domains),
        "computed_at": "2026-05-01T00:00:00+00:00",
        "embedding_centroid_b64": _b64_centroid(axis),
        "domain_bloom_b64": base64.b64encode(bloom).decode("ascii"),
        "embedding_model": "amazon.titan-embed-text-v2:0",
    }
    s.active_personas = []
    return s


# ---------------------------------------------------------------------------
# Helper-level tests — no FastAPI plumbing needed
# ---------------------------------------------------------------------------


class TestBloomHelpers:
    def test_added_domain_is_contained(self) -> None:
        bloom = aigrp.compute_domain_bloom(["cloudfront", "lambda", "cdn"])
        assert aigrp.bloom_contains(bloom, "cloudfront") is True
        assert aigrp.bloom_contains(bloom, "lambda") is True
        # Case-insensitive
        assert aigrp.bloom_contains(bloom, "CDN") is True

    def test_unrelated_domain_likely_misses(self) -> None:
        bloom = aigrp.compute_domain_bloom(["cloudfront", "lambda"])
        # With 8192 bits and 5 hashes for ~2 entries, false-positive rate is
        # essentially zero. 'kubernetes' should miss.
        assert aigrp.bloom_contains(bloom, "kubernetes") is False

    def test_matches_any_returns_on_first_hit(self) -> None:
        bloom = aigrp.compute_domain_bloom(["cloudfront"])
        assert aigrp.bloom_matches_any(bloom, ["kubernetes", "cloudfront"]) is True
        assert aigrp.bloom_matches_any(bloom, ["kubernetes", "redis"]) is False

    def test_empty_or_truncated_bloom_returns_false(self) -> None:
        assert aigrp.bloom_contains(b"", "anything") is False
        assert aigrp.bloom_contains(b"\x00" * 5, "anything") is False  # truncated


# ---------------------------------------------------------------------------
# Resolver-level tests — full /network/dsn/resolve roundtrip
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "bloom-prefilter.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    network._TOPOLOGY_CACHE["value"] = None
    network._TOPOLOGY_CACHE["expires_at"] = 0.0
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0
    network._PEER_KEY_OVERRIDES.clear()
    network._PEER_KEY_OVERRIDES["orion"] = "stub-orion-key"
    network._PEER_KEY_OVERRIDES["acme"] = "stub-acme-key"

    monkeypatch.setattr(network, "DSN_CACHE_REFRESH_SECS", 86_400)

    async def _initial_noop(fleet):
        return []

    monkeypatch.setattr(network, "_fan_out_all", _initial_noop)

    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ALICE, pw)
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'engineering' WHERE username = ?",
                (ALICE,),
            )
        yield c


def _login(client: TestClient) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": ALICE, "password": "pw"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _stub_embed(monkeypatch: pytest.MonkeyPatch, axis: int = 0, dim: int = 8) -> None:
    v = [0.0] * dim
    v[axis] = 1.0

    def _fake_embed(text: str):
        return _pack(v), "stub-model"

    monkeypatch.setattr(network, "embed_text", _fake_embed)


def _seed_diverse_fleet() -> list[network._L2Snapshot]:
    """6 L2s, each with a distinct domain set so the prefilter is exercised."""
    return [
        _snap_with_bloom(slug="orion-eng", enterprise="orion", group="engineering",
                         axis=0, domains=["cloudfront", "lambda"]),
        _snap_with_bloom(slug="orion-sol", enterprise="orion", group="solutions",
                         axis=1, domains=["bedrock", "titan"]),
        _snap_with_bloom(slug="orion-gtm", enterprise="orion", group="gtm",
                         axis=2, domains=["sales", "outbound"]),
        _snap_with_bloom(slug="acme-eng", enterprise="acme", group="engineering",
                         axis=3, domains=["kubernetes", "istio"]),
        _snap_with_bloom(slug="acme-sol", enterprise="acme", group="solutions",
                         axis=4, domains=["cloudfront", "edge", "cdn"]),
        _snap_with_bloom(slug="acme-fin", enterprise="acme", group="finance",
                         axis=5, domains=["sap", "quickbooks"]),
    ]


def test_no_query_domains_means_no_prefilter_step(client, monkeypatch) -> None:
    """Public visitor case: empty query_domains = pure cosine, no Bloom step."""
    _stub_embed(monkeypatch, axis=0)
    network._signature_cache.clear()
    for s in _seed_diverse_fleet():
        network._signature_cache[s.slug] = s
    network._signature_cache_filled_at = time.monotonic()

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "anything"},  # no query_domains
    )
    assert resp.status_code == 200
    steps = [s["step"] for s in resp.json()["resolution_path"]]
    assert "bloom_prefilter" not in steps


def test_query_domains_drops_peers_whose_bloom_doesnt_match(client, monkeypatch) -> None:
    _stub_embed(monkeypatch, axis=0)
    network._signature_cache.clear()
    for s in _seed_diverse_fleet():
        network._signature_cache[s.slug] = s
    network._signature_cache_filled_at = time.monotonic()

    jwt = _login(client)
    # Only orion-eng (cloudfront,lambda) and acme-sol (cloudfront,edge,cdn) have cloudfront.
    # SEC-MED M-6 — pass caller_enterprise/group so bloom_dropped is reported.
    # Anonymous (marketing/public) callers get null'd bloom_dropped to suppress
    # the topic-discovery oracle. Internal callers see the full count.
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={
            "intent": "cloudfront origin failover",
            "query_domains": ["cloudfront"],
            "max_candidates": 10,
            "caller_enterprise": "orion",
            "caller_group": "engineering",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Bloom prefilter step is present, dropped 4 peers (kept 2)
    bloom_step = next(s for s in body["resolution_path"] if s["step"] == "bloom_prefilter")
    assert bloom_step["bloom_dropped"] == 4
    assert bloom_step["l2_count"] == 2

    # The candidates are exactly the two L2s that include cloudfront
    candidate_l2s = sorted(c["l2_id"] for c in body["candidates"])
    assert candidate_l2s == ["acme/solutions", "orion/engineering"]


def test_added_domain_never_dropped_false_negative_safety(client, monkeypatch) -> None:
    """Ensures no peer that ACTUALLY has a domain gets dropped for that domain.

    Bloom filters guarantee no false negatives. We exercise this by
    asking each peer's primary domain in turn and asserting it's kept.
    """
    _stub_embed(monkeypatch, axis=0)
    network._signature_cache.clear()
    fleet = _seed_diverse_fleet()
    for s in fleet:
        network._signature_cache[s.slug] = s
    network._signature_cache_filled_at = time.monotonic()

    jwt = _login(client)
    for snap in fleet:
        first_domain = snap.signature["l2_id"].split("/")[0]  # not the actual domain — fix
        # Take the first actual domain we know we encoded
        domain = {
            "orion-eng": "cloudfront",
            "orion-sol": "bedrock",
            "orion-gtm": "sales",
            "acme-eng": "kubernetes",
            "acme-sol": "edge",
            "acme-fin": "sap",
        }[snap.slug]
        del first_domain
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"intent": "x", "query_domains": [domain], "max_candidates": 10},
        )
        assert resp.status_code == 200
        body = resp.json()
        candidate_l2s = [c["l2_id"] for c in body["candidates"]]
        assert snap.signature["l2_id"] in candidate_l2s, (
            f"peer {snap.slug} (domains include {domain!r}) was dropped — false negative!"
        )


def test_peer_without_bloom_is_kept(client, monkeypatch) -> None:
    """Older signature shape (no domain_bloom_b64) must not be dropped — bloom is a tightener."""
    _stub_embed(monkeypatch, axis=0)
    fleet = _seed_diverse_fleet()
    # Strip the bloom from one peer to simulate older signature shape
    fleet[0].signature.pop("domain_bloom_b64", None)
    network._signature_cache.clear()
    for s in fleet:
        network._signature_cache[s.slug] = s
    network._signature_cache_filled_at = time.monotonic()

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "x", "query_domains": ["random-unrelated-domain"], "max_candidates": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    candidate_l2s = [c["l2_id"] for c in body["candidates"]]
    # The peer without a bloom (orion-eng) is kept regardless of query_domains
    assert "orion/engineering" in candidate_l2s
