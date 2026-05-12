"""Tests for AS-1 persona management endpoints.

Covers:
* auth gating — non-admin gets 403 on all endpoints
* GET /admin/personas — list (empty + populated)
* POST /admin/personas — happy path + 409 duplicate
* PATCH /admin/personas/{username} — change persona + 404 unknown
* POST /admin/personas/{username}/disable — disable + 409 already disabled + 404 unknown
* invite_sent flag behaviour (mock email sender)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.email_sender import MockEmailSender
from cq_server.invite_routes import get_email_sender

ADMIN = "admin@persona-test"
NON_ADMIN = "user@persona-test"
PASSWORD = "password123!"


@pytest.fixture
def mock_sender() -> MockEmailSender:
    return MockEmailSender()


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sender: MockEmailSender,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "personas.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PUBLIC_HOST", "https://test.8th-layer.ai")

    app.dependency_overrides[get_email_sender] = lambda: mock_sender
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN, pw)
        store.sync.create_user(NON_ADMIN, pw)
        store.sync.set_user_role(ADMIN, "admin")
        yield c
    app.dependency_overrides.pop(get_email_sender, None)


def _login(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_list_personas_requires_admin(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.get("/api/v1/admin/personas", headers=headers)
    assert resp.status_code == 403


def test_create_persona_requires_admin(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.post(
        "/api/v1/admin/personas",
        headers=headers,
        json={"email": "new@example.com", "username": "new_user", "persona": "viewer"},
    )
    assert resp.status_code == 403


def test_patch_persona_requires_admin(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.patch(
        "/api/v1/admin/personas/someuser",
        headers=headers,
        json={"persona": "agent"},
    )
    assert resp.status_code == 403


def test_disable_persona_requires_admin(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.post(
        "/api/v1/admin/personas/someuser/disable",
        headers=headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /admin/personas
# ---------------------------------------------------------------------------


def test_list_personas_empty(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    resp = client.get("/api/v1/admin/personas", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_personas_populated(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    # Create two assignments directly via the store.
    store = _get_store()
    import asyncio
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    asyncio.new_event_loop().run_until_complete(
        store.upsert_persona_assignment(
            username=NON_ADMIN,
            persona="viewer",
            assigned_at=now,
            assigned_by=ADMIN,
        )
    )

    resp = client.get("/api/v1/admin/personas", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["username"] == NON_ADMIN
    assert item["persona"] == "viewer"
    assert item["disabled_at"] is None


# ---------------------------------------------------------------------------
# POST /admin/personas
# ---------------------------------------------------------------------------


def test_create_persona_happy_path(
    client: TestClient, mock_sender: MockEmailSender
) -> None:
    headers = _login(client, ADMIN)
    resp = client.post(
        "/api/v1/admin/personas",
        headers=headers,
        json={
            "email": "alice@example.com",
            "username": "alice",
            "persona": "agent",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "alice"
    assert body["persona"] == "agent"
    assert body["assigned_by"] == ADMIN
    # Invite email should have been captured by the mock sender.
    assert body["invite_sent"] is True
    assert len(mock_sender.sent) == 1
    assert mock_sender.sent[0].to == "alice@example.com"


def test_create_persona_duplicate_409(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    payload = {
        "email": "bob@example.com",
        "username": "bob",
        "persona": "viewer",
    }
    resp1 = client.post("/api/v1/admin/personas", headers=headers, json=payload)
    assert resp1.status_code == 201

    resp2 = client.post("/api/v1/admin/personas", headers=headers, json=payload)
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# PATCH /admin/personas/{username}
# ---------------------------------------------------------------------------


def test_patch_persona_happy_path(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    # Seed a persona.
    client.post(
        "/api/v1/admin/personas",
        headers=headers,
        json={"email": "carol@example.com", "username": "carol", "persona": "viewer"},
    )
    # Patch to admin.
    resp = client.patch(
        "/api/v1/admin/personas/carol",
        headers=headers,
        json={"persona": "admin"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["persona"] == "admin"
    assert body["username"] == "carol"


def test_patch_persona_404_unknown(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    resp = client.patch(
        "/api/v1/admin/personas/nonexistent_user",
        headers=headers,
        json={"persona": "viewer"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/personas/{username}/disable
# ---------------------------------------------------------------------------


def test_disable_persona_happy_path(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    client.post(
        "/api/v1/admin/personas",
        headers=headers,
        json={"email": "dave@example.com", "username": "dave", "persona": "viewer"},
    )
    resp = client.post("/api/v1/admin/personas/dave/disable", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "dave"
    assert body["disabled_at"] is not None

    # The list should show the row as disabled.
    list_resp = client.get("/api/v1/admin/personas", headers=headers)
    items = list_resp.json()["items"]
    dave = next(i for i in items if i["username"] == "dave")
    assert dave["disabled_at"] is not None


def test_disable_persona_404_unknown(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    resp = client.post("/api/v1/admin/personas/ghost/disable", headers=headers)
    assert resp.status_code == 404


def test_disable_persona_409_already_disabled(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    client.post(
        "/api/v1/admin/personas",
        headers=headers,
        json={"email": "eve@example.com", "username": "eve", "persona": "external-collaborator"},
    )
    client.post("/api/v1/admin/personas/eve/disable", headers=headers)
    resp = client.post("/api/v1/admin/personas/eve/disable", headers=headers)
    assert resp.status_code == 409
