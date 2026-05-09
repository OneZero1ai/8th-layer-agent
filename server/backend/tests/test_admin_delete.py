"""Tests for the admin DELETE /review/{ku_id} endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.auth import _resolve_caller, get_current_user
from cq_server.deps import get_store, require_api_key

TEST_USERNAME = "test-user"


async def _smart_get_current_user(  # type: ignore[no-untyped-def]
    request: Request,
    store=Depends(get_store),  # noqa: B008 — FastAPI dep
) -> str:
    """Header-aware test override.

    When the caller passes an Authorization header, fall through to the
    live JWT/API-key path so admin-gated /review/* tests pin the
    real caller. With no header, return ``TEST_USERNAME`` so the
    auth-less /propose helper in this file continues to work.
    """
    if request.headers.get("Authorization"):
        caller = await _resolve_caller(request, store)
        return caller.username
    return TEST_USERNAME


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides[require_api_key] = lambda: TEST_USERNAME
    app.dependency_overrides[get_current_user] = _smart_get_current_user
    with TestClient(app) as c:
        from cq_server.app import _get_store
        from cq_server.auth import hash_password

        store = _get_store()
        if store.sync.get_user(TEST_USERNAME) is None:
            store.sync.create_user(TEST_USERNAME, hash_password("test-pw"))
        yield c
    app.dependency_overrides.pop(require_api_key, None)
    app.dependency_overrides.pop(get_current_user, None)


def _propose_payload(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "domains": ["databases", "performance"],
        "insight": {
            "summary": "Use connection pooling for DB clients",
            "detail": "Database connections are expensive to create at request time.",
            "action": "Configure a connection pool with a max size of 10.",
        },
    }
    return {**defaults, **overrides}


def _admin_jwt(client: TestClient) -> str:
    """Bootstrap an admin user + return a JWT for /review/* endpoints."""
    import contextlib

    from cq_server.app import _get_store

    store = _get_store()
    pw_hash = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode()
    with contextlib.suppress(Exception):
        store.sync.create_user("admin", pw_hash)  # already exists is OK
    store.sync.set_user_role("admin", "admin")
    resp = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestAdminDelete:
    def test_delete_existing_unit_returns_204(self, client: TestClient) -> None:
        proposed = client.post("/propose", json=_propose_payload()).json()
        ku_id = proposed["id"]
        jwt = _admin_jwt(client)
        resp = client.delete(f"/review/{ku_id}", headers={"Authorization": f"Bearer {jwt}"})
        assert resp.status_code == 204

    def test_delete_makes_unit_unreachable_via_review(self, client: TestClient) -> None:
        proposed = client.post("/propose", json=_propose_payload()).json()
        ku_id = proposed["id"]
        jwt = _admin_jwt(client)
        client.delete(f"/review/{ku_id}", headers={"Authorization": f"Bearer {jwt}"})
        resp = client.get(f"/review/{ku_id}", headers={"Authorization": f"Bearer {jwt}"})
        assert resp.status_code == 404

    def test_delete_nonexistent_unit_returns_404(self, client: TestClient) -> None:
        jwt = _admin_jwt(client)
        resp = client.delete(
            "/review/ku_doesnotexist",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert resp.status_code == 404

    def test_delete_without_jwt_rejected(self, client: TestClient) -> None:
        proposed = client.post("/propose", json=_propose_payload()).json()
        ku_id = proposed["id"]
        resp = client.delete(f"/review/{ku_id}")
        assert resp.status_code in (401, 403)

    def test_delete_removes_from_underlying_store(self, client: TestClient) -> None:
        from cq_server.app import _get_store

        proposed = client.post("/propose", json=_propose_payload()).json()
        ku_id = proposed["id"]
        store = _get_store()
        assert store.sync.get_any(ku_id) is not None

        jwt = _admin_jwt(client)
        client.delete(f"/review/{ku_id}", headers={"Authorization": f"Bearer {jwt}"})
        assert store.sync.get_any(ku_id) is None

    def test_store_delete_returns_false_for_missing_id(self, client: TestClient) -> None:
        # The fixture initializes the store via app startup; need it to exist
        # before calling _get_store().
        from cq_server.app import _get_store

        _ = client  # pull fixture so app is initialized
        store = _get_store()
        assert store.sync.delete("ku_definitelynotreal") is False
