"""Phase 6 step 4 / Lane J: POST /network/demo/{scenario} tests.

Pins:
  - cross-group-query: trace has 3 events (lookup, dsn, forward-query),
    target is same-Enterprise different-Group.
  - cross-enterprise-blocked: trace shows policy_if_queried=denied at
    DSN; final results empty.
  - cross-enterprise-consented: 412 when no consent row, 200 with
    summary_only forward-query result when consent exists.
  - 404 on unknown scenario; 422 on unknown requester slug.
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

ALICE = "alice"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _b64_centroid(axis: int, dim: int = 8) -> str:
    import base64

    v = [0.0] * dim
    v[axis] = 1.0
    return base64.b64encode(_pack(v)).decode("ascii")


def _stub_embed(monkeypatch: pytest.MonkeyPatch, axis: int = 0, dim: int = 8) -> None:
    v = [0.0] * dim
    v[axis] = 1.0

    def _fake(text: str):
        return _pack(v), "stub-model"

    monkeypatch.setattr(network, "embed_text", _fake)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "demo.db"))
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
        store.sync.create_user(ALICE, pw)
        yield c


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"username": ALICE, "password": "pw"}
    )
    return resp.json()["token"]


def _patch_fleet_calls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_axis_for: dict[str, int],
    forward_query_response: dict | None = None,
    forward_query_status: int = 200,
) -> dict[str, list]:
    """Patch httpx.AsyncClient at the per-call helpers so the demo flow
    runs without any real ALB. Captures forward-query targets so tests
    can assert what the orchestrator chose.

    ``target_axis_for`` maps slug -> axis index for the L2's centroid.
    ``forward_query_response`` is what the responder L2 returns for the
    /aigrp/forward-query call; defaults to a single summary_only KU.
    """
    captured: dict[str, list] = {"forward_query_targets": [], "lookups": []}

    async def _fake_fan_out(fleet):
        snaps = []
        for l2 in fleet:
            snap = network._L2Snapshot(
                slug=l2["slug"],
                enterprise=l2["enterprise"],
                group=l2["group"],
                endpoint=l2["endpoint"],
                reachable=True,
            )
            snap.peers = []
            snap.signature = {
                "l2_id": f"{l2['enterprise']}/{l2['group']}",
                "ku_count": 3,
                "domain_count": 4,
                "computed_at": "2026-04-30T01:00:00+00:00",
                "embedding_centroid_b64": _b64_centroid(target_axis_for[l2["slug"]]),
                "embedding_model": "amazon.titan-embed-text-v2:0",
            }
            snap.active_personas = []
            snaps.append(snap)
        return snaps

    monkeypatch.setattr(network, "_fan_out_all", _fake_fan_out)

    async def _fake_lookup(client, l2, *, intent, persona):
        captured["lookups"].append(l2["slug"])
        return ({"results": []}, 5)

    async def _fake_forward(
        client, target, *, requester, requester_persona, query_vec, query_text
    ):
        captured["forward_query_targets"].append(target["slug"])
        if forward_query_status != 200:
            return None, 7
        body = forward_query_response or {
            "responder_l2_id": f"{target['enterprise']}/{target['group']}",
            "responder_enterprise": target["enterprise"],
            "responder_group": target["group"],
            "policy_applied": "summary_only",
            "results": [
                {
                    "ku_id": "ku_demo_1",
                    "summary": "CloudFront cache key omits query strings by default",
                    "detail": None,
                    "action": None,
                    "domains": ["aws", "cloudfront"],
                    "sim_score": 0.91,
                    "redacted_fields": ["detail", "action"],
                }
            ],
            "result_count": 1,
        }
        return body, 9

    monkeypatch.setattr(network, "_call_aigrp_lookup", _fake_lookup)
    monkeypatch.setattr(network, "_call_forward_query", _fake_forward)
    return captured


class TestCrossGroupScenario:
    def test_routes_to_same_enterprise_different_group(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        # Make acme/solutions the top candidate (axis 0); requester is acme/eng.
        captured = _patch_fleet_calls(
            monkeypatch,
            target_axis_for={
                "orion-eng": 1, "orion-sol": 2, "orion-gtm": 3,
                "acme-eng": 5, "acme-sol": 0, "acme-fin": 6,
            },
        )
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/cross-group-query",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-cloudfront-asker",
                "requester_l2_slug": "acme-eng",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["scenario"] == "cross-group-query"
        assert len(body["trace"]) == 3
        actions = [e["action"] for e in body["trace"]]
        assert actions == ["aigrp_lookup", "dsn_resolve", "aigrp_forward_query"]
        # forward-query was sent to acme-sol (same Enterprise, different Group).
        assert captured["forward_query_targets"] == ["acme-sol"]
        # final_results carry redaction info so the frontend can render.
        assert body["final_results"]
        assert body["final_results"][0]["redacted_fields"] == ["detail", "action"]


class TestCrossEnterpriseBlocked:
    def test_top_candidate_is_orion_with_denied_policy(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        captured = _patch_fleet_calls(
            monkeypatch,
            target_axis_for={
                "orion-eng": 0, "orion-sol": 2, "orion-gtm": 3,
                "acme-eng": 5, "acme-sol": 4, "acme-fin": 6,
            },
            forward_query_response={
                "responder_l2_id": "orion/engineering",
                "responder_enterprise": "orion",
                "responder_group": "engineering",
                "policy_applied": "denied",
                "results": [],
                "result_count": 0,
            },
        )
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/cross-enterprise-blocked",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-bedrock-asker",
                "requester_l2_slug": "acme-eng",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert captured["forward_query_targets"] == ["orion-eng"]
        # DSN step exposes the denied policy in its result_summary.
        dsn_step = body["trace"][1]
        assert "denied" in dsn_step["result_summary"]
        assert body["final_results"] == []


class TestCrossEnterpriseConsentedPrecondition:
    def test_412_when_no_consent_row(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        _patch_fleet_calls(
            monkeypatch,
            target_axis_for={
                "orion-eng": 0, "orion-sol": 2, "orion-gtm": 3,
                "acme-eng": 5, "acme-sol": 4, "acme-fin": 6,
            },
        )
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/cross-enterprise-consented",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-bedrock-asker",
                "requester_l2_slug": "acme-eng",
            },
        )
        assert resp.status_code == 412, resp.text
        body = resp.json()
        # FastAPI wraps custom dict detail under "detail".
        detail = body["detail"]
        assert detail["error"] == "no_consent"

    def test_200_when_consent_exists(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed a consent row from acme/eng -> orion/eng.
        store = _get_store()
        store.sync.insert_cross_enterprise_consent(
            consent_id="consent_test_xx",
            requester_enterprise="acme",
            responder_enterprise="orion",
            requester_group="engineering",
            responder_group="engineering",
            policy="summary_only",
            signed_by_admin="test-admin",
            signed_at="2026-04-30T00:00:00+00:00",
            expires_at=None,
            audit_log_id="aud_test_xx",
        )
        _stub_embed(monkeypatch, axis=0)
        _patch_fleet_calls(
            monkeypatch,
            target_axis_for={
                "orion-eng": 0, "orion-sol": 2, "orion-gtm": 3,
                "acme-eng": 5, "acme-sol": 4, "acme-fin": 6,
            },
        )
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/cross-enterprise-consented",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-bedrock-asker",
                "requester_l2_slug": "acme-eng",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Final results carry the summary_only redaction.
        assert body["final_results"]
        assert body["final_results"][0]["redacted_fields"] == ["detail", "action"]


class TestUnknownScenario:
    def test_unknown_scenario_returns_404(self, client: TestClient) -> None:
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/not-a-real-scenario",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-x",
                "requester_l2_slug": "acme-eng",
            },
        )
        assert resp.status_code == 404


class TestUnknownRequesterSlug:
    def test_422_on_unknown_slug(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embed(monkeypatch, axis=0)
        jwt = _login(client)
        resp = client.post(
            "/api/v1/network/demo/cross-group-query",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_persona": "persona-x",
                "requester_l2_slug": "not-a-fleet-l2",
            },
        )
        assert resp.status_code == 422
