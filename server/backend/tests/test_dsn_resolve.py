"""Phase 6 step 4 / Lane I: POST /network/dsn/resolve tests.

Pins:
  - Embed + fan-out + cosine ranking pipeline runs end-to-end.
  - Same-Enterprise + same-Group candidate -> ``full_body`` policy.
  - Cross-Enterprise without consent -> ``denied`` + ``cross_enterprise_no_consent``.
  - resolution_path is populated with three timing steps (embed, fan_out, rank).
  - 503 when embedding is unavailable.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server import network
from cq_server.app import _get_store, app

ALICE = "alice"  # acme/engineering


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _b64_centroid(axis: int, dim: int = 8) -> str:
    """Build a base64 centroid that points along a unit axis — easy cosine arithmetic."""
    import base64

    v = [0.0] * dim
    v[axis] = 1.0
    return base64.b64encode(_pack(v)).decode("ascii")


def _snapshot_with_centroid(
    *, slug: str, enterprise: str, group: str, axis: int, ku_count: int = 5
) -> network._L2Snapshot:
    snap = network._L2Snapshot(
        slug=slug,
        enterprise=enterprise,
        group=group,
        endpoint=f"http://stub-{slug}.local",
        reachable=True,
    )
    snap.peers = []
    snap.signature = {
        "l2_id": f"{enterprise}/{group}",
        "ku_count": ku_count,
        "domain_count": 4,
        "computed_at": "2026-04-30T01:00:00+00:00",
        "embedding_centroid_b64": _b64_centroid(axis),
        "embedding_model": "amazon.titan-embed-text-v2:0",
    }
    snap.active_personas = []
    return snap


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "dsn.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    network._TOPOLOGY_CACHE["value"] = None
    network._TOPOLOGY_CACHE["expires_at"] = 0.0
    network._PEER_KEY_OVERRIDES.clear()
    network._PEER_KEY_OVERRIDES["orion"] = "stub-orion-key"
    network._PEER_KEY_OVERRIDES["acme"] = "stub-acme-key"
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.create_user(ALICE, pw)
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'engineering' WHERE username = ?",
                (ALICE,),
            )
        yield c


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"username": ALICE, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _stub_fanout_six_l2s(monkeypatch: pytest.MonkeyPatch, target_axis: int = 0) -> None:
    """Stub the fleet so acme/solutions has the highest cosine to query axis 0."""
    # Configure each L2 with a distinct axis. acme/solutions on the target
    # axis, others scattered.
    axis_map = {
        "orion-eng": 1,
        "orion-sol": 2,
        "orion-gtm": 3,
        "acme-eng": 4,
        "acme-sol": target_axis,  # match the query
        "acme-fin": 5,
    }

    async def _fake(fleet):
        return [
            _snapshot_with_centroid(
                slug=l2["slug"],
                enterprise=l2["enterprise"],
                group=l2["group"],
                axis=axis_map[l2["slug"]],
            )
            for l2 in fleet
        ]

    monkeypatch.setattr(network, "_fan_out_all", _fake)


def _stub_embed(monkeypatch: pytest.MonkeyPatch, axis: int = 0, dim: int = 8) -> None:
    """Stub embed_text in the network module so we can drive cosine deterministically."""
    v = [0.0] * dim
    v[axis] = 1.0

    def _fake_embed(text: str):
        return _pack(v), "stub-model"

    monkeypatch.setattr(network, "embed_text", _fake_embed)


class TestDsnResolveHappyPath:
    def test_returns_ranked_candidates_with_resolution_path(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        _stub_fanout_six_l2s(monkeypatch, target_axis=0)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"intent": "I need help with CloudFront", "max_candidates": 3},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "I need help with CloudFront"
        assert body["embedding_dims"] == 8
        candidates = body["candidates"]
        assert len(candidates) == 3
        # acme/solutions sits on the same axis, so it ranks first.
        assert candidates[0]["l2_id"] == "acme/solutions"
        # resolution_path has three steps.
        steps = [s["step"] for s in body["resolution_path"]]
        assert steps == ["embed", "fan_out_signatures", "rank"]
        # fan_out step exposes the L2 count.
        fan_step = next(s for s in body["resolution_path"] if s["step"] == "fan_out_signatures")
        assert fan_step["l2_count"] == 6


class TestDsnPolicyDecisions:
    def test_same_enterprise_same_group_is_full_body(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Alice = acme/engineering. Make her own L2 the top candidate.
        _stub_embed(monkeypatch, axis=0)

        async def _fake(fleet):
            return [
                _snapshot_with_centroid(
                    slug=l2["slug"],
                    enterprise=l2["enterprise"],
                    group=l2["group"],
                    axis=(0 if l2["slug"] == "acme-eng" else 1 + i),
                )
                for i, l2 in enumerate(fleet)
            ]

        monkeypatch.setattr(network, "_fan_out_all", _fake)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"intent": "anything", "max_candidates": 6, "caller_enterprise": "acme", "caller_group": "engineering"},
        )
        body = resp.json()
        own = next(c for c in body["candidates"] if c["l2_id"] == "acme/engineering")
        assert own["policy_if_queried"] == "full_body"
        assert own["policy_reason"] == "same_enterprise_same_group"

    def test_cross_enterprise_no_consent_is_denied(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        _stub_fanout_six_l2s(monkeypatch, target_axis=0)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "intent": "anything",
                "max_candidates": 6,
                "include_consented_cross_enterprise": True,
            },
        )
        body = resp.json()
        # Find an orion candidate from Alice's POV (acme/engineering).
        orion = next(c for c in body["candidates"] if c["enterprise"] == "orion")
        assert orion["policy_if_queried"] == "denied"
        assert orion["policy_reason"] == "cross_enterprise_no_consent"

    def test_same_enterprise_xgroup_is_summary_only(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        _stub_fanout_six_l2s(monkeypatch, target_axis=0)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"intent": "anything", "max_candidates": 6, "caller_enterprise": "acme", "caller_group": "engineering"},
        )
        body = resp.json()
        # acme/solutions vs Alice (acme/engineering) -> summary_only.
        sol = next(c for c in body["candidates"] if c["l2_id"] == "acme/solutions")
        assert sol["policy_if_queried"] == "summary_only"
        assert sol["policy_reason"] == "same_enterprise_xgroup_summary"


class TestDsnEmbedFailure:
    def test_503_when_embedding_unavailable(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(network, "embed_text", lambda text: None)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/dsn/resolve",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"intent": "anything"},
        )
        assert resp.status_code == 503
