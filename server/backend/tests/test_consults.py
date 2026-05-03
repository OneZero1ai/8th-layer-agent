"""Sprint 2 / Issue #20 — L3 consults · same-L2 path.

Pins:
  - Open thread + drop opening message in one POST /consults/request.
  - Inbox of recipient shows the open thread.
  - Either participant can append messages on /consults/{id}/messages.
  - Non-participants get 403.
  - Either participant can close; closing a thread blocks further messages (409).
  - Cross-L2 target returns 501 (next PR).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server import network
from cq_server.app import _get_store, app

ALICE = "alice"  # acme/engineering
BOB = "bob"      # acme/engineering — same L2 as Alice
CARLA = "carla"  # acme/solutions  — different L2 from Alice (cross-team)
DAN = "dan"      # also acme/engineering — third-party for participant gating


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "consults.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0
    monkeypatch.setattr(network, "DSN_CACHE_REFRESH_SECS", 86_400)

    async def _initial_noop(fleet):
        return []

    monkeypatch.setattr(network, "_fan_out_all", _initial_noop)

    with TestClient(app) as c:
        store = _get_store()
        for user, ent, grp in [
            (ALICE, "acme", "engineering"),
            (BOB, "acme", "engineering"),
            (CARLA, "acme", "solutions"),
            (DAN, "acme", "engineering"),
        ]:
            pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
            store.sync.create_user(user, pw)
            with store._engine.begin() as _c:
                _c.exec_driver_sql(
                    "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
                    (ent, grp, user),
                )
        yield c


def _login(client: TestClient, who: str) -> str:
    r = client.post("/api/v1/auth/login", json={"username": who, "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(client: TestClient, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, who)}"}


# ---------------------------------------------------------------------------
# Happy path — same-L2 consult between Alice and Bob
# ---------------------------------------------------------------------------


def test_alice_opens_thread_to_bob_drops_opening_message(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/engineering",
            "to_persona": BOB,
            "subject": "have you seen the cloudfront origin failover thing?",
            "content": "We're hitting a 503 cascade when origin-shield expires.",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["from_persona"] == ALICE
    assert body["to_persona"] == BOB
    assert body["from_l2_id"] == "acme/engineering"
    assert body["to_l2_id"] == "acme/engineering"
    assert body["status"] == "open"
    thread_id = body["thread_id"]

    # The opening message landed in /messages
    msgs = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
    )
    assert msgs.status_code == 200, msgs.text
    msg_list = msgs.json()["messages"]
    assert len(msg_list) == 1
    assert msg_list[0]["from_persona"] == ALICE
    assert "503 cascade" in msg_list[0]["content"]


def test_recipient_sees_thread_in_inbox(client: TestClient) -> None:
    # Alice opens
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/engineering",
            "to_persona": BOB,
            "content": "ping",
        },
    )
    assert r.status_code == 201
    thread_id = r.json()["thread_id"]

    # Bob's inbox includes it
    inbox = client.get("/api/v1/consults/inbox", headers=_headers(client, BOB))
    assert inbox.status_code == 200, inbox.text
    body = inbox.json()
    assert body["self_l2_id"] == "acme/engineering"
    assert body["self_persona"] == BOB
    thread_ids = [t["thread_id"] for t in body["threads"]]
    assert thread_id in thread_ids


def test_either_participant_can_reply(client: TestClient) -> None:
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "hi"},
    )
    thread_id = open_resp.json()["thread_id"]

    # Bob replies
    bob_reply = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, BOB),
        json={"content": "saw your runbook — origin-shield TTL?"},
    )
    assert bob_reply.status_code == 201, bob_reply.text
    assert bob_reply.json()["from_persona"] == BOB

    # Alice replies again
    alice_reply = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
        json={"content": "yeah, set 48h — but pin the secondary first"},
    )
    assert alice_reply.status_code == 201

    # Three messages total (open + Bob + Alice)
    msgs = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
    ).json()["messages"]
    assert len(msgs) == 3
    assert [m["from_persona"] for m in msgs] == [ALICE, BOB, ALICE]


def test_non_participant_403_on_messages(client: TestClient) -> None:
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "hi"},
    )
    thread_id = open_resp.json()["thread_id"]

    # Dan (same L2 as alice/bob, but not a participant) tries to reply
    r = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, DAN),
        json={"content": "eavesdrop"},
    )
    assert r.status_code == 403, r.text

    # Dan tries to read messages
    r = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, DAN),
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------


def test_either_participant_can_close_blocks_further_messages(client: TestClient) -> None:
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "hi"},
    )
    thread_id = open_resp.json()["thread_id"]

    # Bob closes
    close = client.post(
        f"/api/v1/consults/{thread_id}/close",
        headers=_headers(client, BOB),
        json={"reason": "got what I needed", "resolution_summary": "set TTL=48h, pin secondary"},
    )
    assert close.status_code == 200, close.text
    assert close.json()["status"] == "closed"
    assert close.json()["resolution_summary"] == "set TTL=48h, pin secondary"

    # Alice tries to reply on closed thread
    r = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
        json={"content": "one more thing"},
    )
    assert r.status_code == 409, r.text


def test_closed_thread_excluded_from_inbox_by_default(client: TestClient) -> None:
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "hi"},
    )
    thread_id = open_resp.json()["thread_id"]
    client.post(
        f"/api/v1/consults/{thread_id}/close",
        headers=_headers(client, BOB),
        json={"reason": "done"},
    )

    # Default — closed not shown
    inbox = client.get("/api/v1/consults/inbox", headers=_headers(client, BOB)).json()
    assert thread_id not in [t["thread_id"] for t in inbox["threads"]]

    # include_closed=true — audit view includes it
    inbox_all = client.get(
        "/api/v1/consults/inbox?include_closed=true", headers=_headers(client, BOB)
    ).json()
    assert thread_id in [t["thread_id"] for t in inbox_all["threads"]]


# ---------------------------------------------------------------------------
# Cross-L2 → 501 (next PR will implement this)
# ---------------------------------------------------------------------------


def test_cross_l2_unreachable_target_is_rejected(client: TestClient) -> None:
    """Cross-L2 to an unreachable peer is rejected with 403 or 404.

    The test fixture's enterprise is the default — so ``acme/solutions``
    is a cross-Enterprise target with no active directory peering →
    403 'no active peering'. (Pre-sprint-4: was 501 'AI-BGP roadmap'.
    Pre-Track-A: was 404 'AIGRP peer table'.) Both flavours of
    'unreachable' are rejected before any side effects.
    """
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/solutions",
            "to_persona": CARLA,
            "content": "hi from engineering",
        },
    )
    assert r.status_code == 403, r.text
    assert "no active peering" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Auth — anonymous calls rejected
# ---------------------------------------------------------------------------


def test_anonymous_request_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/request",
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "hi"},
    )
    assert r.status_code in (401, 403)


def test_anonymous_inbox_rejected(client: TestClient) -> None:
    r = client.get("/api/v1/consults/inbox")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# SEC-HIGH #37 — content max_length=4096 to prevent EFS-fill DoS
# ---------------------------------------------------------------------------


def test_consult_request_oversize_content_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={
            "to_l2_id": "acme/engineering",
            "to_persona": BOB,
            "content": "x" * 4097,
        },
    )
    assert r.status_code == 422


def test_consult_message_oversize_content_rejected(client: TestClient) -> None:
    open_resp = client.post(
        "/api/v1/consults/request",
        headers=_headers(client, ALICE),
        json={"to_l2_id": "acme/engineering", "to_persona": BOB, "content": "ok"},
    )
    thread_id = open_resp.json()["thread_id"]
    r = client.post(
        f"/api/v1/consults/{thread_id}/messages",
        headers=_headers(client, ALICE),
        json={"content": "x" * 4097},
    )
    assert r.status_code == 422
