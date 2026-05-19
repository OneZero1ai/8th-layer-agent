"""Cross-L2 AIGRP federation — outbound forward-query fan-out (agent#316).

Covers the four pieces built for issue #316:

  1. Outbound forward-query client — issues a signed POST to a peer's
     ``/aigrp/forward-query`` and parses the response into RemoteHits.
  2. Fan-out in ``aigrp_lookup`` — local + remote hits merge and re-rank
     by similarity; local-only behaviour unchanged when there are no
     peers.
  3. Bloom prefilter — peers whose ``domain_bloom`` claims none of the
     query's domain tags are skipped before the HTTP call.
  4. ``confidence`` on the forward-query response hit — the field
     round-trips so a forwarder's ``min_confidence`` filter applies
     consistently to remote hits.

The peer-timeout invariant — a slow/dead peer degrades to local-only,
never errors the lookup — is pinned in ``TestPeerTimeout``.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import cq_server.app as app_module
from cq_server.aigrp import federation
from cq_server.aigrp._legacy import compute_domain_bloom
from cq_server.app import _get_store, app
from cq_server.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "federation.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")  # pragma: allowlist secret
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")  # pragma: allowlist secret
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "group-a")
    monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "test-peer-key")  # pragma: allowlist secret
    with TestClient(app) as c:
        yield c


def _seed_user(*, username: str, password: str) -> None:
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            ("acme", "group-a", username),
        )


def _login_jwt(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _mint_api_key(client: TestClient, jwt_token: str) -> str:
    resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "federation-test", "ttl": "30d"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _propose_one(client: TestClient, api_key: str, summary: str) -> str:
    resp = client.post(
        "/propose",
        json={
            "domains": ["test-fleet", "iam"],
            "insight": {
                "summary": summary,
                "detail": "Local KU body for federation merge test.",
                "action": "See tests/test_aigrp_federation.py.",
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _stub_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    vec = [1.0] + [0.0] * 7
    packed = struct.pack(f"<{len(vec)}f", *vec)

    def _fake(_text: str):
        return packed, "stub-model"

    monkeypatch.setattr(app_module, "embed_text", _fake)


def _peer_row(*, l2_id: str, domains: list[str] | None, endpoint: str = "https://peer.example/") -> dict:
    """Build an ``aigrp_peers``-shaped dict. ``domains=None`` => no Bloom."""
    bloom = compute_domain_bloom(domains) if domains is not None else None
    return {
        "l2_id": l2_id,
        "enterprise": "acme",
        "group": l2_id.split("/", 1)[-1],
        "endpoint_url": endpoint,
        "embedding_centroid": None,
        "domain_bloom": bloom,
        "ku_count": 1,
        "domain_count": len(domains or []),
        "embedding_model": "stub-model",
        "first_seen_at": "2026-05-19T00:00:00Z",
        "last_seen_at": "2026-05-19T00:00:00Z",
        "last_signature_at": None,
        "public_key_ed25519": None,
    }


# ---------------------------------------------------------------------------
# 3. Bloom prefilter
# ---------------------------------------------------------------------------


class TestBloomPrefilter:
    def test_peer_with_no_domain_overlap_skipped(self) -> None:
        peer_match = _peer_row(l2_id="acme/group-b", domains=["iam", "aws"])
        peer_miss = _peer_row(l2_id="acme/group-c", domains=["frontend", "css"])
        selected = federation.select_peers_for_query(
            [peer_match, peer_miss],
            query_domains=["iam"],
        )
        assert [p["l2_id"] for p in selected] == ["acme/group-b"]

    def test_empty_query_domains_keeps_all_peers(self) -> None:
        peer_a = _peer_row(l2_id="acme/group-b", domains=["iam"])
        peer_b = _peer_row(l2_id="acme/group-c", domains=["frontend"])
        selected = federation.select_peers_for_query([peer_a, peer_b], query_domains=[])
        assert {p["l2_id"] for p in selected} == {"acme/group-b", "acme/group-c"}

    def test_stub_peer_without_endpoint_skipped(self) -> None:
        stub = _peer_row(l2_id="acme/group-b", domains=["iam"], endpoint="")
        selected = federation.select_peers_for_query([stub], query_domains=["iam"])
        assert selected == []

    def test_peer_without_bloom_is_kept(self) -> None:
        """A peer that hasn't reported a signature yet (domain_bloom NULL)
        cannot be prefiltered — keep it so a fresh peer is still reachable."""
        peer = _peer_row(l2_id="acme/group-b", domains=None)
        selected = federation.select_peers_for_query([peer], query_domains=["iam"])
        assert [p["l2_id"] for p in selected] == ["acme/group-b"]


# ---------------------------------------------------------------------------
# 1. Outbound forward-query client
# ---------------------------------------------------------------------------


class TestForwardQueryClient:
    def test_client_posts_signed_request_and_parses_hits(self) -> None:
        captured: dict = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["forwarder"] = request.headers.get("x-8l-forwarder-l2-id")
            return httpx.Response(
                200,
                json={
                    "responder_l2_id": "acme/group-b",
                    "responder_enterprise": "acme",
                    "responder_group": "group-b",
                    "policy_applied": "summary_only",
                    "result_count": 1,
                    "results": [
                        {
                            "ku_id": "ku-remote-1",
                            "summary": "Remote KU from sibling L2",
                            "detail": None,
                            "action": None,
                            "domains": ["iam"],
                            "sim_score": 0.74,
                            "confidence": 0.82,
                            "redacted_fields": ["detail", "action"],
                        }
                    ],
                },
            )

        async def _run() -> list[federation.RemoteHit]:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as mock_client:
                return await federation.forward_query_peer(
                    _peer_row(l2_id="acme/group-b", domains=["iam"]),
                    query_vec=[1.0, 0.0],
                    query_text="iam key rotation",
                    requester_l2_id="acme/group-a",
                    requester_enterprise="acme",
                    requester_group="group-a",
                    requester_persona="agent-a1",
                    max_results=5,
                    bearer_resolver=lambda _l2: "derived-bearer",
                    client=mock_client,
                )

        hits = asyncio.run(_run())
        assert captured["url"] == "https://peer.example/api/v1/aigrp/forward-query"
        assert captured["auth"] == "Bearer derived-bearer"
        assert captured["forwarder"] == "acme/group-a"
        assert len(hits) == 1
        assert hits[0].ku_id == "ku-remote-1"
        assert hits[0].confidence == pytest.approx(0.82)
        assert hits[0].similarity == pytest.approx(0.74)
        assert hits[0].policy_applied == "summary_only"

    def test_client_returns_empty_on_peer_error(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        async def _run() -> list[federation.RemoteHit]:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as mock_client:
                return await federation.forward_query_peer(
                    _peer_row(l2_id="acme/group-b", domains=["iam"]),
                    query_vec=[1.0],
                    query_text="x",
                    requester_l2_id="acme/group-a",
                    requester_enterprise="acme",
                    requester_group="group-a",
                    requester_persona="agent-a1",
                    max_results=5,
                    bearer_resolver=lambda _l2: "b",
                    client=mock_client,
                )

        assert asyncio.run(_run()) == []


# ---------------------------------------------------------------------------
# 1b. Forward signing — the request carries a valid Ed25519 signature
#     that the receiver's verification path accepts.
# ---------------------------------------------------------------------------


class TestForwardQuerySigning:
    def test_request_carries_signature_the_receiver_accepts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A converged-mesh receiver has the peer's pubkey on file and
        hard-403s a forward-query with a missing/invalid signature
        (see _legacy.require_forwarder_identity). This pins that
        forward_query_peer emits a SIGNATURE_HEADER whose bytes verify
        against the matching pubkey — i.e. exactly what the receiver
        checks via verify_forward_signature."""
        from cq_server import forward_sign

        # Give this L2 a real Ed25519 keypair on disk.
        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(tmp_path / "l2_key.bin"))
        forward_sign.reload_l2_privkey()
        pubkey_b64u = forward_sign.self_public_key_b64u()
        assert pubkey_b64u is not None  # signing must be enabled

        captured: dict = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured["sig"] = request.headers.get(forward_sign.SIGNATURE_HEADER)
            captured["forwarder"] = request.headers.get("x-8l-forwarder-l2-id")
            captured["body"] = _json.loads(request.content)
            return httpx.Response(
                200,
                json={"policy_applied": "full_body", "result_count": 0, "results": []},
            )

        async def _run() -> None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as mock_client:
                await federation.forward_query_peer(
                    _peer_row(l2_id="acme/group-b", domains=["iam"]),
                    query_vec=[1.0, 0.0],
                    query_text="iam key rotation",
                    requester_l2_id="acme/group-a",
                    requester_enterprise="acme",
                    requester_group="group-a",
                    requester_persona="agent-a1",
                    max_results=5,
                    bearer_resolver=lambda _l2: "derived-bearer",
                    client=mock_client,
                )

        asyncio.run(_run())

        # The header is present...
        assert captured["sig"], "forward-query must carry an Ed25519 signature header"
        # ...and verifies exactly the way the receiver verifies it:
        # JCS(body_for_sig) || forwarder_l2_id, body_for_sig being the
        # posted JSON body 1:1.
        assert forward_sign.verify_forward_signature(
            pubkey_b64u,
            captured["body"],
            captured["forwarder"],
            captured["sig"],
        ), "signature must verify against the matching pubkey"

        # A signature over a different forwarder id must NOT verify —
        # confirms the forwarder id is bound into the signed input.
        assert not forward_sign.verify_forward_signature(
            pubkey_b64u,
            captured["body"],
            "acme/group-evil",
            captured["sig"],
        )

        forward_sign.reload_l2_privkey()  # reset cache for other tests

    def test_no_signature_header_when_signing_disabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no L2 keypair is available, sign_forward_request returns
        None and the header is omitted (legacy receivers still accept)."""
        from cq_server import forward_sign

        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(tmp_path / "missing_key.bin"))
        monkeypatch.setattr(forward_sign, "load_or_create_l2_privkey", lambda: None)
        forward_sign.reload_l2_privkey()
        assert forward_sign.get_l2_privkey() is None

        captured: dict = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["sig"] = request.headers.get(forward_sign.SIGNATURE_HEADER)
            return httpx.Response(
                200,
                json={"policy_applied": "full_body", "result_count": 0, "results": []},
            )

        async def _run() -> None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as mock_client:
                await federation.forward_query_peer(
                    _peer_row(l2_id="acme/group-b", domains=["iam"]),
                    query_vec=[1.0],
                    query_text="x",
                    requester_l2_id="acme/group-a",
                    requester_enterprise="acme",
                    requester_group="group-a",
                    requester_persona="agent-a1",
                    max_results=5,
                    bearer_resolver=lambda _l2: "b",
                    client=mock_client,
                )

        asyncio.run(_run())
        assert captured["sig"] is None

        forward_sign.reload_l2_privkey()  # reset cache for other tests


# ---------------------------------------------------------------------------
# SSRF guard — peers with unsafe endpoint_url are skipped
# ---------------------------------------------------------------------------


class TestEndpointSsrfGuard:
    def test_non_https_and_private_hosts_skipped(self) -> None:
        unsafe_endpoints = [
            "http://peer.example/",  # non-https scheme
            "https://localhost/",
            "https://127.0.0.1:8080/",
            "https://10.1.2.3/",
            "https://192.168.1.5/",
            "https://169.254.169.254/",  # cloud metadata endpoint
            "https://172.16.0.1/",
            "https://172.31.255.1/",
        ]
        peers = [
            _peer_row(l2_id=f"acme/group-{i}", domains=["iam"], endpoint=ep)
            for i, ep in enumerate(unsafe_endpoints)
        ]
        selected = federation.select_peers_for_query(peers, query_domains=["iam"])
        assert selected == [], f"unsafe endpoints were not all skipped: {selected}"

    def test_public_https_peer_kept(self) -> None:
        # 172.32.x is outside RFC1918 172.16/12 — must NOT be skipped.
        for ep in ("https://peer.example/", "https://172.32.0.1/"):
            peer = _peer_row(l2_id="acme/group-b", domains=["iam"], endpoint=ep)
            selected = federation.select_peers_for_query([peer], query_domains=["iam"])
            assert [p["l2_id"] for p in selected] == ["acme/group-b"], ep


# ---------------------------------------------------------------------------
# 2. Fan-out in aigrp_lookup — local + remote merge and re-rank
# ---------------------------------------------------------------------------


class TestLookupFanOut:
    def test_remote_hits_merge_and_rerank_with_local(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user(username="a1", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "a1", "pw"))
        local_ku = _propose_one(client, api_key, "Local IAM key rotation knowledge unit for federation merge")
        store = _get_store()
        store.sync.set_review_status(local_ku, "approved", "a1")
        _stub_embed(monkeypatch)

        unit = asyncio.run(store.get(local_ku))
        assert unit is not None

        # Local hit at similarity 0.60.
        async def _fake_semantic_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return [(unit, 0.60)]

        monkeypatch.setattr(store, "semantic_query", _fake_semantic_query)

        # One sibling peer in the table.
        async def _fake_list_peers(_enterprise: str):
            return [_peer_row(l2_id="acme/group-b", domains=["iam"])]

        monkeypatch.setattr(store, "list_aigrp_peers", _fake_list_peers)

        # Remote hit at similarity 0.88 — should out-rank the local one.
        async def _fake_fan_out(_peers, **_kwargs):
            return [
                federation.RemoteHit(
                    ku_id="ku-remote-hi",
                    summary="Remote IAM KU, higher similarity",
                    detail="body",
                    action="do the thing",
                    domains=["iam"],
                    similarity=0.88,
                    confidence=0.9,
                    created_by="agent-b1",
                    peer_l2_id="acme/group-b",
                    policy_applied="full_body",
                )
            ]

        monkeypatch.setattr(app_module.aigrp, "fan_out_forward_query", _fake_fan_out)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "context": "how do I rotate an IAM key",
                "persona": "a1",
                "min_confidence": 0.0,
                "min_similarity": 0.0,
                "exclude_self": False,
            },
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert [r["ku_id"] for r in results] == ["ku-remote-hi", local_ku]
        # Remote hit kept its confidence + action.
        assert results[0]["confidence"] == pytest.approx(0.9)
        assert results[0]["action"] == "do the thing"

    def test_local_only_unchanged_when_no_peers(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With an empty peer table the lookup must behave exactly as the
        former local-only path: no fan-out call, local hits only."""
        _seed_user(username="solo", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "solo", "pw"))
        local_ku = _propose_one(client, api_key, "Solo-L2 IAM key rotation knowledge unit, local-only path")
        store = _get_store()
        store.sync.set_review_status(local_ku, "approved", "solo")
        _stub_embed(monkeypatch)
        unit = asyncio.run(store.get(local_ku))
        assert unit is not None

        async def _fake_semantic_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return [(unit, 0.71)]

        monkeypatch.setattr(store, "semantic_query", _fake_semantic_query)

        # fan_out must not even be reached — if it is, fail loudly.
        async def _explode(_peers, **_kwargs):
            raise AssertionError("fan_out_forward_query called with no peers")

        monkeypatch.setattr(app_module.aigrp, "fan_out_forward_query", _explode)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "context": "iam",
                "persona": "solo",
                "min_confidence": 0.0,
                "min_similarity": 0.0,
                "exclude_self": False,
            },
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert [r["ku_id"] for r in results] == [local_ku]


# ---------------------------------------------------------------------------
# Peer-timeout invariant — slow/dead peer degrades to local-only
# ---------------------------------------------------------------------------


class TestPeerTimeout:
    def test_lookup_returns_local_results_when_peer_times_out(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user(username="t1", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "t1", "pw"))
        local_ku = _propose_one(client, api_key, "Local KU that survives a dead-peer forward-query timeout")
        store = _get_store()
        store.sync.set_review_status(local_ku, "approved", "t1")
        _stub_embed(monkeypatch)
        unit = asyncio.run(store.get(local_ku))
        assert unit is not None

        async def _fake_semantic_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return [(unit, 0.55)]

        monkeypatch.setattr(store, "semantic_query", _fake_semantic_query)

        async def _fake_list_peers(_enterprise: str):
            return [_peer_row(l2_id="acme/group-b", domains=["iam"])]

        monkeypatch.setattr(store, "list_aigrp_peers", _fake_list_peers)

        # The real fan_out runs, but the peer's HTTP transport raises a
        # timeout — fan_out_forward_query must swallow it and return [].
        def _timeout_handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("peer is dead")

        real_forward = federation.forward_query_peer

        async def _forward_with_mock_transport(peer, **kwargs):
            transport = httpx.MockTransport(_timeout_handler)
            async with httpx.AsyncClient(transport=transport) as mock_client:
                kwargs["client"] = mock_client
                return await real_forward(peer, **kwargs)

        monkeypatch.setattr(federation, "forward_query_peer", _forward_with_mock_transport)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "context": "iam",
                "persona": "t1",
                "min_confidence": 0.0,
                "min_similarity": 0.0,
                "exclude_self": False,
            },
        )
        # Lookup still 200s with the local hit — the dead peer is skipped.
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert [r["ku_id"] for r in results] == [local_ku]


# ---------------------------------------------------------------------------
# 4. confidence round-trip through /aigrp/forward-query
# ---------------------------------------------------------------------------


class TestConfidenceRoundTrip:
    def test_forward_query_response_carries_confidence(
        self,
        client: TestClient,
    ) -> None:
        """A KU served by /aigrp/forward-query reports its evidence
        confidence so a forwarding L2 can apply min_confidence to remote
        hits the same way it does to local ones."""
        _seed_user(username="owner", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "owner", "pw"))
        ku_id = _propose_one(client, api_key, "Forward-query confidence field round-trip knowledge unit")
        store = _get_store()
        store.sync.set_review_status(ku_id, "approved", "owner")
        # Give the KU a known, non-default confidence.
        with store._engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE knowledge_units SET embedding = ? WHERE id = ?",
                (struct.pack("<8f", 1.0, 0, 0, 0, 0, 0, 0, 0), ku_id),
            )
        unit = asyncio.run(store.get(ku_id))
        assert unit is not None
        expected_conf = unit.evidence.confidence

        resp = client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": "Bearer test-peer-key",
                "x-8l-forwarder-l2-id": "acme/group-a",
            },
            json={
                "query_vec": [1.0, 0, 0, 0, 0, 0, 0, 0],
                "query_text": "confidence",
                "requester_l2_id": "acme/group-a",
                "requester_enterprise": "acme",
                "requester_group": "group-a",
                "requester_persona": "agent-a1",
                "max_results": 5,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result_count"] == 1
        hit = body["results"][0]
        assert hit["ku_id"] == ku_id
        assert "confidence" in hit
        assert hit["confidence"] == pytest.approx(expected_conf)
