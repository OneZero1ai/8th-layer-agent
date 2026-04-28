"""Tests for the admin DELETE /review/{ku_id} endpoint."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.deps import require_api_key

TEST_USERNAME = "test-user"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides[require_api_key] = lambda: TEST_USERNAME
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(require_api_key, None)


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
    from cq_server.app import _get_store

    store = _get_store()
    pw_hash = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode()
    try:
        store.create_user("admin", pw_hash)
    except Exception:
        pass  # already exists
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
        assert store.get_any(ku_id) is not None

        jwt = _admin_jwt(client)
        client.delete(f"/review/{ku_id}", headers={"Authorization": f"Bearer {jwt}"})
        assert store.get_any(ku_id) is None

    def test_store_delete_returns_false_for_missing_id(self, client: TestClient) -> None:
        # The fixture initializes the store via app startup; need it to exist
        # before calling _get_store().
        from cq_server.app import _get_store

        _ = client  # pull fixture so app is initialized
        store = _get_store()
        assert store.delete("ku_definitelynotreal") is False
