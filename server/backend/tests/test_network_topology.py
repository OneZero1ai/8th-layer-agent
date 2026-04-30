"""Phase 6 step 4 / Lane J: POST /network/topology aggregator tests.

Pins:
  - Happy path: all 6 L2s reachable -> grouped-by-Enterprise response
    with peer/persona counts populated.
  - Partial-fleet failure: a single L2 returning 500 yields a row with
    ``peer_count=null`` rather than failing the whole call.
  - 3-second in-process cache: a second call within the TTL returns the
    same generated_at without re-fanning out.

The tests stub network.fan_out via a monkeypatched
``_fan_out_all`` so we don't need a live ALB to exercise the aggregator.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server import network
from cq_server.app import _get_store, app

ALICE = "alice"


def _snapshot(
    *,
    slug: str,
    enterprise: str,
    group: str,
    ku_count: int = 7,
    peer_count: int | None = 5,
    active: list[dict] | None = None,
    reachable: bool = True,
) -> network._L2Snapshot:
    """Build an _L2Snapshot stub mirroring what _fetch_one_l2 would yield."""
    snap = network._L2Snapshot(
        slug=slug,
        enterprise=enterprise,
        group=group,
        endpoint=f"http://stub-{slug}.local",
        reachable=reachable,
    )
    if peer_count is not None:
        snap.peers = [
            {"l2_id": f"{enterprise}/peer-{i}", "last_signature_at": "2026-04-30T00:00:00+00:00"}
            for i in range(peer_count)
        ]
        snap.signature = {
            "l2_id": f"{enterprise}/{group}",
            "ku_count": ku_count,
            "domain_count": 12,
            "computed_at": "2026-04-30T01:00:00+00:00",
            "embedding_centroid_b64": None,
            "embedding_model": "amazon.titan-embed-text-v2:0",
        }
    snap.active_personas = active or []
    return snap


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "topology.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    # Reset the in-process topology cache between tests.
    network._TOPOLOGY_CACHE["value"] = None
    network._TOPOLOGY_CACHE["expires_at"] = 0.0
    network._PEER_KEY_OVERRIDES.clear()
    network._PEER_KEY_OVERRIDES["orion"] = "stub-orion-key"
    network._PEER_KEY_OVERRIDES["acme"] = "stub-acme-key"
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.create_user(ALICE, pw)
        yield c


def _login(client: TestClient, username: str = ALICE) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestTopologyHappyPath:
    def test_all_six_l2s_aggregated(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_fan_out(fleet):
            return [
                _snapshot(slug=l2["slug"], enterprise=l2["enterprise"], group=l2["group"])
                for l2 in fleet
            ]

        monkeypatch.setattr(network, "_fan_out_all", _fake_fan_out)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/topology",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "generated_at" in body
        # 2 enterprises, 3 L2s each.
        names = {e["enterprise"] for e in body["enterprises"]}
        assert names == {"orion", "acme"}
        for ent in body["enterprises"]:
            assert len(ent["l2s"]) == 3
            for l2 in ent["l2s"]:
                assert l2["peer_count"] == 5
                assert l2["ku_count"] == 7
                assert l2["l2_id"].startswith(ent["enterprise"] + "/")
                assert isinstance(l2["peers"], list)
                assert isinstance(l2["active_personas"], list)
        assert isinstance(body["cross_enterprise_consents"], list)


class TestPartialFleetFailure:
    def test_one_l2_down_yields_null_peer_count_not_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_fan_out(fleet):
            snaps = []
            for l2 in fleet:
                if l2["slug"] == "acme-fin":
                    # Unreachable — no peers, no signature.
                    snap = network._L2Snapshot(
                        slug=l2["slug"],
                        enterprise=l2["enterprise"],
                        group=l2["group"],
                        endpoint=l2["endpoint"],
                        reachable=False,
                    )
                    snaps.append(snap)
                else:
                    snaps.append(
                        _snapshot(slug=l2["slug"], enterprise=l2["enterprise"], group=l2["group"])
                    )
            return snaps

        monkeypatch.setattr(network, "_fan_out_all", _fake_fan_out)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/topology",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Find acme/finance — must have peer_count=null + ku_count=0.
        acme = next(e for e in body["enterprises"] if e["enterprise"] == "acme")
        fin = next(row for row in acme["l2s"] if row["group"] == "finance")
        assert fin["peer_count"] is None
        assert fin["ku_count"] == 0
        # Other rows still healthy.
        eng = next(row for row in acme["l2s"] if row["group"] == "engineering")
        assert eng["peer_count"] == 5


class TestTopologyCache:
    def test_second_call_within_ttl_returns_cached_response(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = {"n": 0}

        async def _fake_fan_out(fleet):
            call_count["n"] += 1
            return [
                _snapshot(slug=l2["slug"], enterprise=l2["enterprise"], group=l2["group"])
                for l2 in fleet
            ]

        monkeypatch.setattr(network, "_fan_out_all", _fake_fan_out)
        jwt = _login(client)
        r1 = client.post(
            "/api/v1/network/topology",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r2 = client.post(
            "/api/v1/network/topology",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        # generated_at is identical because the second call hit the cache.
        assert r1.json()["generated_at"] == r2.json()["generated_at"]
        assert call_count["n"] == 1


class TestTopologyAuth:
    def test_no_auth_required_public_read(self, client: TestClient) -> None:
        # Topology is intentionally public for the marketing site at
        # 8thlayer.onezero1.ai — no JWT required.
        resp = client.post("/api/v1/network/topology")
        assert resp.status_code == 200

    def test_get_method_also_works(self, client: TestClient) -> None:
        # GET supported alongside POST so the marketing site can poll
        # without preflight CORS gymnastics.
        resp = client.get("/api/v1/network/topology")
        assert resp.status_code == 200
