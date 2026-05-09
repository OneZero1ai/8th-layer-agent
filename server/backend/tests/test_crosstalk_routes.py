"""Tests for crosstalk endpoints (#124).

Coverage:
- Send creates a thread + first message; recipient sees in inbox
- Reply on existing thread fans to other participants
- Tenancy enforcement (can't read another enterprise's threads)
- Participant scoping (non-admin can't read threads they're not in)
- Admin sees all threads in tenant (audit shape)
- Activity log captures crosstalk_send / crosstalk_reply / crosstalk_close
- Close transitions; second close returns 409
- Inbox unread filtering + mark_read

Auth pattern: each test seeds 2-3 users via the same recipe used by
test_propose_tenancy_regression, mints an API key per persona, calls
the endpoint with the persona's bearer token.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.auth import hash_password


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "crosstalk.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


def _seed_user(*, username: str, password: str, enterprise_id: str, group_id: str, role: str = "user") -> None:
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ?, role = ? WHERE username = ?",
            (enterprise_id, group_id, role, username),
        )


def _login_and_mint(client: TestClient, username: str, password: str) -> str:
    """Log in, mint an API key, return the API-key bearer token."""
    jwt_resp = client.post("/auth/login", json={"username": username, "password": password})
    assert jwt_resp.status_code == 200, jwt_resp.text
    jwt = jwt_resp.json()["token"]

    key_resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": f"{username}-test-key", "ttl": "30d"},
    )
    assert key_resp.status_code == 201, key_resp.text
    return key_resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ============================================================================


def test_send_creates_thread_and_recipient_sees_in_inbox(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    bob_key = _login_and_mint(client, "bob", "pw")

    resp = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "ping from alice", "subject": "hi"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["thread_id"].startswith("thread_")
    assert body["message_id"].startswith("msg_")

    # Bob's inbox should show 1 unread
    inbox = client.get("/crosstalk/inbox", headers=_bearer(bob_key)).json()
    assert inbox["count"] == 1
    assert inbox["items"][0]["from_username"] == "alice"
    assert inbox["items"][0]["content"] == "ping from alice"


def test_reply_appends_to_existing_thread(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    bob_key = _login_and_mint(client, "bob", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "ping"},
    )
    thread_id = send.json()["thread_id"]

    reply = client.post(
        f"/crosstalk/threads/{thread_id}/messages",
        headers=_bearer(bob_key),
        json={"content": "pong"},
    )
    assert reply.status_code == 201, reply.text

    # Alice retrieves the thread; should see both messages
    thread = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(alice_key)).json()
    assert thread["thread"]["status"] == "open"
    assert len(thread["messages"]) == 2
    assert thread["messages"][0]["content"] == "ping"
    assert thread["messages"][1]["content"] == "pong"


def test_non_participant_cannot_read_thread(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="charlie", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    charlie_key = _login_and_mint(client, "charlie", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "private to bob"},
    )
    thread_id = send.json()["thread_id"]

    resp = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(charlie_key))
    assert resp.status_code == 403


def test_admin_sees_all_threads_in_tenant(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="rootadmin", password="pw", enterprise_id="acme", group_id="eng", role="admin")

    alice_key = _login_and_mint(client, "alice", "pw")
    admin_key = _login_and_mint(client, "rootadmin", "pw")

    client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "alice -> bob"},
    )

    threads = client.get("/crosstalk/threads", headers=_bearer(admin_key)).json()
    assert threads["count"] >= 1
    # Admin can read the thread even though they're not a participant
    thread_id = threads["items"][0]["id"]
    resp = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(admin_key))
    assert resp.status_code == 200


def test_cross_enterprise_isolation(client: TestClient) -> None:
    """A user in enterprise A cannot see threads from enterprise B."""
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="ext-eve", password="pw", enterprise_id="megacorp", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    eve_key = _login_and_mint(client, "ext-eve", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "intra-acme private"},
    )
    thread_id = send.json()["thread_id"]

    # Eve (different enterprise) cannot see the thread
    resp = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(eve_key))
    assert resp.status_code == 404


def test_send_to_unknown_recipient_returns_404(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    alice_key = _login_and_mint(client, "alice", "pw")

    resp = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "ghost-user", "content": "hi"},
    )
    assert resp.status_code == 404


def test_close_thread_then_double_close_returns_409(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "hi"},
    )
    thread_id = send.json()["thread_id"]

    first = client.post(
        f"/crosstalk/threads/{thread_id}/close",
        headers=_bearer(alice_key),
        json={"reason": "resolved"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/crosstalk/threads/{thread_id}/close",
        headers=_bearer(alice_key),
        json={"reason": "resolved"},
    )
    assert second.status_code == 409


def test_inbox_mark_read_clears_unread(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    bob_key = _login_and_mint(client, "bob", "pw")

    client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "hi"},
    )

    inbox = client.get("/crosstalk/inbox?mark_read=true", headers=_bearer(bob_key)).json()
    assert inbox["count"] == 1

    inbox2 = client.get("/crosstalk/inbox", headers=_bearer(bob_key)).json()
    assert inbox2["count"] == 0  # marked read; no longer unread


def test_activity_log_captures_crosstalk_events(client: TestClient, tmp_path: Path) -> None:
    """Send + reply + close should each fire the corresponding activity event."""
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="rootadmin", password="pw", enterprise_id="acme", group_id="eng", role="admin")

    alice_key = _login_and_mint(client, "alice", "pw")
    bob_key = _login_and_mint(client, "bob", "pw")
    admin_key = _login_and_mint(client, "rootadmin", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "alice greeting"},
    )
    thread_id = send.json()["thread_id"]

    client.post(
        f"/crosstalk/threads/{thread_id}/messages",
        headers=_bearer(bob_key),
        json={"content": "bob reply"},
    )
    client.post(
        f"/crosstalk/threads/{thread_id}/close",
        headers=_bearer(alice_key),
        json={"reason": "wrap"},
    )

    # Activity log entries arrive via background tasks; confirm by
    # asking the admin's view (it's filterable but the smallest reliable
    # check is to read on disk).
    db_path = str(tmp_path / "crosstalk.db")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event_type, persona FROM activity_log WHERE thread_or_chain_id = ? ORDER BY ts ASC",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()

    event_types = [r[0] for r in rows]
    assert "crosstalk_send" in event_types
    assert "crosstalk_reply" in event_types
    assert "crosstalk_close" in event_types

    # Admin can also read the activity log via API
    api_resp = client.get(
        "/activity?event_type=crosstalk_send",
        headers=_bearer(admin_key),
    ).json()
    assert any(e["thread_or_chain_id"] == thread_id for e in api_resp["items"])


def test_idempotency_send_with_client_thread_and_message_ids(client: TestClient) -> None:
    """Client-provided thread_id + message_id round-trip + retry-safe."""
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    alice_key = _login_and_mint(client, "alice", "pw")

    client_thread_id = "thread_clientprovided001"
    client_message_id = "msg_clientprovided001"

    first = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={
            "to": "bob",
            "content": "first send",
            "thread_id": client_thread_id,
            "message_id": client_message_id,
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["thread_id"] == client_thread_id
    assert first.json()["message_id"] == client_message_id

    # Retry with same IDs — should return existing record, no duplicate
    second = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={
            "to": "bob",
            "content": "second send (should be ignored)",
            "thread_id": client_thread_id,
            "message_id": client_message_id,
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["message_id"] == client_message_id
    assert second.json()["sent_at"] == first.json()["sent_at"]

    # Thread has only 1 message
    thread = client.get(f"/crosstalk/threads/{client_thread_id}", headers=_bearer(alice_key)).json()
    assert len(thread["messages"]) == 1
    assert thread["messages"][0]["content"] == "first send"


def test_idempotency_append_to_existing_thread_via_send(client: TestClient) -> None:
    """If client-provided thread_id matches existing, append (don't recreate)."""
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")
    alice_key = _login_and_mint(client, "alice", "pw")

    first = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "first"},
    )
    thread_id = first.json()["thread_id"]

    # Same thread_id, NEW message_id → should append
    second = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "second", "thread_id": thread_id},
    )
    assert second.status_code == 201, second.text
    assert second.json()["thread_id"] == thread_id
    assert second.json()["message_id"] != first.json()["message_id"]

    thread = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(alice_key)).json()
    assert len(thread["messages"]) == 2


def test_reply_to_closed_thread_returns_409(client: TestClient) -> None:
    _seed_user(username="alice", password="pw", enterprise_id="acme", group_id="eng")
    _seed_user(username="bob", password="pw", enterprise_id="acme", group_id="eng")

    alice_key = _login_and_mint(client, "alice", "pw")
    bob_key = _login_and_mint(client, "bob", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(alice_key),
        json={"to": "bob", "content": "hi"},
    )
    thread_id = send.json()["thread_id"]

    client.post(
        f"/crosstalk/threads/{thread_id}/close",
        headers=_bearer(alice_key),
        json={"reason": "done"},
    )

    resp = client.post(
        f"/crosstalk/threads/{thread_id}/messages",
        headers=_bearer(bob_key),
        json={"content": "too late"},
    )
    assert resp.status_code == 409
