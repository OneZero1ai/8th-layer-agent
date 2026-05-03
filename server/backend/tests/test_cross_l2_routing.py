"""Sprint 2 part 2 — cross-L2 consult routing via AIGRP.

Pins:
  - Same-Enterprise cross-Group: forwards to peer's /consults/forward-request
    with peer-key bearer; thread mirrors locally first, then peer.
  - Internal /consults/forward-request creates the local thread row +
    appends opening message. Idempotent on duplicate thread_id /
    message_id. Peer-key auth required.
  - /consults/forward-message appends a reply, lazily creates the
    thread row if missing.
  - Replies on a cross-L2 thread forward to the OTHER side of the thread
    (sender's side might be either from_ or to_).
  - Peer not in AIGRP table → 404 from /consults/request.
  - Peer key wrong → 401 on the internal /forward-* endpoints.
  - Cross-Enterprise (peer in different Enterprise) → 501.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import bcrypt
import httpx
import pytest
from fastapi.testclient import TestClient

from cq_server import consults, network
from cq_server.app import _get_store, app

ALICE = "alice"  # acme/engineering — this L2
DAN = "dan"      # acme/engineering — also this L2

ACME_PEER_KEY = "test-acme-peer-key-thirty-two-chars"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "xl2.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_AIGRP_PEER_KEY", ACME_PEER_KEY)
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0
    monkeypatch.setattr(network, "DSN_CACHE_REFRESH_SECS", 86_400)

    async def _noop(fleet):
        return []

    monkeypatch.setattr(network, "_fan_out_all", _noop)

    with TestClient(app) as c:
        store = _get_store()
        # Seed users
        for u, ent, grp in [
            (ALICE, "acme", "engineering"),
            (DAN, "acme", "engineering"),
        ]:
            pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
            store.sync.create_user(u, pw)
            with store._lock, store._conn:
                store._conn.execute(
                    "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
                    (ent, grp, u),
                )
        # Seed AIGRP peer table with two siblings on the same Enterprise
        # and one on a different Enterprise (cross-Enterprise should 501).
        now = "2026-05-01T16:00:00+00:00"
        store.sync.upsert_aigrp_peer(
            l2_id="acme/solutions",
            enterprise="acme",
            group="solutions",
            endpoint_url="http://acme-solutions-l2.test:3000",
            embedding_centroid=None,
            domain_bloom=None,
            ku_count=0,
            domain_count=0,
            embedding_model=None,
            signature_received=False,
        )
        store.sync.upsert_aigrp_peer(
            l2_id="rival/eng",
            enterprise="rival",
            group="eng",
            endpoint_url="http://rival-eng-l2.test:3000",
            embedding_centroid=None,
            domain_bloom=None,
            ku_count=0,
            domain_count=0,
            embedding_model=None,
            signature_received=False,
        )
        del now
        yield c


def _login(client: TestClient, who: str) -> str:
    r = client.post("/api/v1/auth/login", json={"username": who, "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(client: TestClient, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, who)}"}


# ---------------------------------------------------------------------------
# Cross-L2 forward — happy path
# ---------------------------------------------------------------------------


def test_cross_l2_request_mirrors_local_and_forwards_to_peer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alice on acme/engineering opens a thread to acme/solutions.

    The asker's L2 (this one) writes the thread + opening message
    locally, then POSTs /consults/forward-request to the peer with the
    AIGRP peer key in the bearer.
    """
    forwarded: list[dict[str, Any]] = []

    class StubResp:
        status_code = 201
        text = "{}"

    class StubClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> "StubClient":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> StubResp:
            forwarded.append({"url": url, "headers": headers, "json": json})
            return StubResp()

    monkeypatch.setattr(consults.httpx, "Client", StubClient)

    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/solutions",
            "to_persona": "carla",
            "subject": "cross-team",
            "content": "saw the cdn runbook?",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["to_l2_id"] == "acme/solutions"
    assert body["from_l2_id"] == "acme/engineering"

    # Forward happened to the peer's endpoint with the peer key bearer
    assert len(forwarded) == 1
    assert forwarded[0]["url"] == "http://acme-solutions-l2.test:3000/api/v1/consults/forward-request"
    assert forwarded[0]["headers"]["authorization"] == f"Bearer {ACME_PEER_KEY}"
    payload = forwarded[0]["json"]
    assert payload["thread_id"] == body["thread_id"]
    assert payload["content"] == "saw the cdn runbook?"
    assert payload["from_l2_id"] == "acme/engineering"
    assert payload["from_persona"] == ALICE


def test_cross_l2_message_forwards_to_other_side(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Alice replies on a cross-L2 thread, the reply forwards to the OTHER L2."""
    forwarded: list[dict[str, Any]] = []

    class StubResp:
        status_code = 201
        text = "{}"

    class StubClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "StubClient":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> StubResp:
            forwarded.append({"url": url, "json": json})
            return StubResp()

    monkeypatch.setattr(consults.httpx, "Client", StubClient)

    # Alice opens a cross-L2 thread
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/solutions", "to_persona": "carla", "content": "hi"},
    )
    assert open_resp.status_code == 201
    thread_id = open_resp.json()["thread_id"]

    forwarded.clear()  # ignore the open's forward; assert on reply's

    # Alice replies — should forward
    reply = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
        json={"content": "follow up"},
    )
    assert reply.status_code == 201, reply.text

    assert len(forwarded) == 1
    assert forwarded[0]["url"].endswith("/consults/forward-message")
    assert forwarded[0]["json"]["thread_id"] == thread_id
    assert forwarded[0]["json"]["from_persona"] == ALICE


def test_cross_enterprise_no_peering_returns_403(client: TestClient) -> None:
    """A peer in a different Enterprise needs an active directory peering.

    Sprint 4 Track A: 501 (AI-BGP roadmap) is now 403 ("no active
    peering"). Without a row in aigrp_directory_peerings between us
    and the target enterprise, the cross-Enterprise forward path
    refuses to route.
    """
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "rival/eng",
            "to_persona": "their_alice",
            "content": "hi from across the boundary",
        },
    )
    assert r.status_code == 403, r.text
    assert "no active peering" in r.json()["detail"].lower()


def test_unknown_peer_returns_404(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/nowhere",
            "to_persona": "ghost",
            "content": "?",
        },
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Internal /forward-request endpoint — peer-key auth + idempotency
# ---------------------------------------------------------------------------


def test_forward_request_mirrors_thread_and_message(client: TestClient) -> None:
    payload = {
        "thread_id": "th_cross_a",
        "message_id": "msg_cross_a",
        "from_l2_id": "acme/solutions",
        "from_persona": "carla",
        "to_l2_id": "acme/engineering",
        "to_persona": ALICE,
        "subject": "from solutions",
        "content": "saw your alert?",
        "created_at": "2026-05-01T16:30:00+00:00",
    }
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={
            "Authorization": f"Bearer {ACME_PEER_KEY}",
            "x-8l-forwarder-l2-id": "acme/solutions",
        },
        json=payload,
    )
    assert r.status_code == 201, r.text

    # Now alice's inbox has the thread
    inbox = client.get("/api/v1/consults/inbox", headers=_headers(client, ALICE)).json()
    thread_ids = [t["thread_id"] for t in inbox["threads"]]
    assert "th_cross_a" in thread_ids


def test_forward_request_idempotent_on_redelivery(client: TestClient) -> None:
    payload = {
        "thread_id": "th_dup_a",
        "message_id": "msg_dup_a",
        "from_l2_id": "acme/solutions",
        "from_persona": "carla",
        "to_l2_id": "acme/engineering",
        "to_persona": ALICE,
        "subject": "dupe-test",
        "content": "first",
        "created_at": "2026-05-01T17:00:00+00:00",
    }
    h = {"Authorization": f"Bearer {ACME_PEER_KEY}", "x-8l-forwarder-l2-id": "acme/solutions"}
    r1 = client.post("/api/v1/consults/forward-request", headers=h, json=payload)
    r2 = client.post("/api/v1/consults/forward-request", headers=h, json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201  # duplicate is a no-op, not a 500


def test_forward_request_rejects_wrong_peer_key(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={
            "Authorization": "Bearer wrong-key",
            "x-8l-forwarder-l2-id": "acme/solutions",
        },
        json={
            "thread_id": "x",
            "message_id": "x",
            "from_l2_id": "acme/solutions",
            "from_persona": "x",
            "to_l2_id": "acme/engineering",
            "to_persona": "x",
            "content": "x",
            "created_at": "x",
        },
    )
    assert r.status_code == 401


def test_forward_request_rejects_anonymous(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/forward-request",
        json={
            "thread_id": "x",
            "message_id": "x",
            "from_l2_id": "acme/solutions",
            "from_persona": "x",
            "to_l2_id": "acme/engineering",
            "to_persona": "x",
            "content": "x",
            "created_at": "x",
        },
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /forward-message — appends, lazily creates thread if missing
# ---------------------------------------------------------------------------


def test_forward_message_appends_to_existing_thread(client: TestClient) -> None:
    h = {"Authorization": f"Bearer {ACME_PEER_KEY}", "x-8l-forwarder-l2-id": "acme/solutions"}
    # First, mirror the thread via /forward-request
    client.post("/api/v1/consults/forward-request", headers=h, json={
        "thread_id": "th_msg_a",
        "message_id": "msg_msg_a_1",
        "from_l2_id": "acme/solutions",
        "from_persona": "carla",
        "to_l2_id": "acme/engineering",
        "to_persona": ALICE,
        "content": "open",
        "created_at": "2026-05-01T18:00:00+00:00",
    })
    # Append a reply via /forward-message — note the reply is from
    # acme/engineering (forwarder identity must match body.from_l2_id).
    h_reply = {"Authorization": f"Bearer {ACME_PEER_KEY}", "x-8l-forwarder-l2-id": "acme/engineering"}
    r = client.post("/api/v1/consults/forward-message", headers=h_reply, json={
        "thread_id": "th_msg_a",
        "message_id": "msg_msg_a_2",
        "from_l2_id": "acme/engineering",
        "from_persona": ALICE,
        "content": "reply",
        "created_at": "2026-05-01T18:05:00+00:00",
    })
    assert r.status_code == 201, r.text

    # Both messages visible to alice
    msgs = client.get(
        "/api/v1/consults/th_msg_a/messages",
        headers=_headers(client, ALICE),
    ).json()["messages"]
    assert [m["message_id"] for m in msgs] == ["msg_msg_a_1", "msg_msg_a_2"]


def test_forward_message_lazily_creates_missing_thread(client: TestClient) -> None:
    """If /forward-request was lost, /forward-message backfills the thread row."""
    h = {"Authorization": f"Bearer {ACME_PEER_KEY}", "x-8l-forwarder-l2-id": "acme/solutions"}
    r = client.post("/api/v1/consults/forward-message", headers=h, json={
        "thread_id": "th_lazy_a",
        "message_id": "msg_lazy_a",
        "from_l2_id": "acme/solutions",
        "from_persona": "carla",
        "content": "reply (asker-side init lost)",
        "created_at": "2026-05-01T19:00:00+00:00",
        "thread_subject": "recovered",
        "thread_to_l2_id": "acme/engineering",
        "thread_to_persona": ALICE,
        "thread_from_l2_id": "acme/solutions",
        "thread_from_persona": "carla",
        "thread_created_at": "2026-05-01T18:55:00+00:00",
    })
    assert r.status_code == 201, r.text

    # Alice's inbox has the lazily-created thread
    inbox = client.get("/api/v1/consults/inbox", headers=_headers(client, ALICE)).json()
    assert "th_lazy_a" in [t["thread_id"] for t in inbox["threads"]]


def test_forward_message_missing_thread_no_metadata_400(client: TestClient) -> None:
    h = {"Authorization": f"Bearer {ACME_PEER_KEY}", "x-8l-forwarder-l2-id": "acme/solutions"}
    r = client.post("/api/v1/consults/forward-message", headers=h, json={
        "thread_id": "th_missing",
        "message_id": "msg_x",
        "from_l2_id": "acme/solutions",
        "from_persona": "carla",
        "content": "no thread no metadata",
        "created_at": "2026-05-01T20:00:00+00:00",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# SEC-CRIT #34 — forwarder identity binding on /forward-* endpoints
# ---------------------------------------------------------------------------


_VALID_FWD_PAYLOAD = {
    "thread_id": "th_sec",
    "message_id": "msg_sec",
    "from_l2_id": "acme/solutions",
    "from_persona": "carla",
    "to_l2_id": "acme/engineering",
    "to_persona": "alice",
    "content": "hello",
    "created_at": "2026-05-01T21:00:00+00:00",
}


def test_forward_request_missing_forwarder_header_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={"Authorization": f"Bearer {ACME_PEER_KEY}"},
        json=dict(_VALID_FWD_PAYLOAD),
    )
    assert r.status_code == 400
    assert "x-8l-forwarder-l2-id" in r.json()["detail"].lower()


def test_forward_request_header_body_mismatch_403(client: TestClient) -> None:
    """Compromised L2 forges body identity but forgets to match the header."""
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={
            "Authorization": f"Bearer {ACME_PEER_KEY}",
            "x-8l-forwarder-l2-id": "acme/finance",  # claims to be finance
        },
        json={**_VALID_FWD_PAYLOAD, "from_l2_id": "acme/solutions"},  # body says solutions
    )
    assert r.status_code == 403
    assert "mismatch" in r.json()["detail"].lower()


def test_forward_request_cross_enterprise_forwarder_rejected(client: TestClient) -> None:
    """A forwarder claiming to belong to a foreign Enterprise is rejected.

    consults.forward-* is intra-Enterprise only (decision 10's mutual
    logging within an Enterprise); cross-Enterprise consults flow through
    /aigrp/forward-query with consent.
    """
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={
            "Authorization": f"Bearer {ACME_PEER_KEY}",
            "x-8l-forwarder-l2-id": "rival/eng",
        },
        json={**_VALID_FWD_PAYLOAD, "from_l2_id": "rival/eng"},
    )
    assert r.status_code == 403
    assert "cross-enterprise" in r.json()["detail"].lower()


def test_forward_request_malformed_forwarder_header_400(client: TestClient) -> None:
    """Forwarder header without enterprise/group separator is invalid."""
    r = client.post(
        "/api/v1/consults/forward-request",
        headers={
            "Authorization": f"Bearer {ACME_PEER_KEY}",
            "x-8l-forwarder-l2-id": "no-slash-here",
        },
        json={**_VALID_FWD_PAYLOAD, "from_l2_id": "no-slash-here"},
    )
    assert r.status_code == 400
    assert "enterprise/group" in r.json()["detail"].lower()


def test_forward_message_missing_forwarder_header_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/forward-message",
        headers={"Authorization": f"Bearer {ACME_PEER_KEY}"},
        json={
            "thread_id": "x",
            "message_id": "y",
            "from_l2_id": "acme/solutions",
            "from_persona": "carla",
            "content": "x",
            "created_at": "x",
        },
    )
    assert r.status_code == 400


# Use _ to suppress unused-import lint
_ = MagicMock
_ = os
_ = httpx
