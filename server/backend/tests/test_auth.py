"""Tests for authentication module."""

import time
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.auth import create_token, hash_password, verify_password, verify_token
from cq_server.deps import require_api_key


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides[require_api_key] = lambda: "test-user"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(require_api_key, None)


def _seed_user(client: TestClient, username: str = "peter", password: str = "secret123") -> None:
    """Seed a user directly via the store."""
    from cq_server.app import _get_store
    from cq_server.auth import hash_password

    store = _get_store()
    store.sync.create_user(username, hash_password(password))


class TestPasswordHashing:
    def test_verify_correct_password(self) -> None:
        hashed = hash_password("secret123")
        assert verify_password("secret123", hashed) is True

    def test_verify_wrong_password(self) -> None:
        hashed = hash_password("secret123")
        assert verify_password("wrong", hashed) is False


class TestJWT:
    def test_create_and_verify_token(self) -> None:
        test_secret = "test-secret"  # pragma: allowlist secret
        token = create_token("peter", secret=test_secret, ttl_hours=24)
        payload = verify_token(token, secret=test_secret)
        assert payload["sub"] == "peter"

    def test_expired_token_rejected(self) -> None:
        test_secret = "test-secret"  # pragma: allowlist secret
        token = create_token("peter", secret=test_secret, ttl_hours=0)
        time.sleep(1)
        with pytest.raises(jwt.ExpiredSignatureError):
            verify_token(token, secret=test_secret)

    def test_invalid_token_rejected(self) -> None:
        test_secret = "test-secret"  # pragma: allowlist secret
        with pytest.raises(jwt.DecodeError):
            verify_token("not.a.token", secret=test_secret)

    def test_wrong_secret_rejected(self) -> None:
        secret_a = "secret-a"  # pragma: allowlist secret
        secret_b = "secret-b"  # pragma: allowlist secret
        token = create_token("peter", secret=secret_a)
        with pytest.raises(jwt.InvalidSignatureError):
            verify_token(token, secret=secret_b)


class TestLoginEndpoint:
    test_password = "secret123"  # pragma: allowlist secret

    def test_login_success(self, client: TestClient) -> None:
        _seed_user(client)
        resp = client.post("/auth/login", json={"username": "peter", "password": self.test_password})
        assert resp.status_code == 200
        body = resp.json()
        assert "token" in body
        assert body["username"] == "peter"

    def test_login_wrong_password(self, client: TestClient) -> None:
        _seed_user(client)
        resp = client.post(
            "/auth/login",
            json={"username": "peter", "password": "wrong"},  # pragma: allowlist secret
        )
        assert resp.status_code == 401

    def test_login_unknown_user(self, client: TestClient) -> None:
        resp = client.post("/auth/login", json={"username": "nobody", "password": self.test_password})
        assert resp.status_code == 401


class TestAuthMe:
    test_password = "secret123"  # pragma: allowlist secret

    def test_me_with_valid_token(self, client: TestClient) -> None:
        _seed_user(client)
        login = client.post("/auth/login", json={"username": "peter", "password": self.test_password})
        token = login.json()["token"]
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "peter"
        assert body["auth_kind"] == "jwt"
        assert body["api_key_id"] is None
        assert body["expires_at"] is None
        assert body["issued_at"] is None
        # Tenancy claims always populated (default values when user row lacks them).
        assert body["enterprise_id"]
        assert body["group_id"]
        assert body["l2_id"] == f"{body['enterprise_id']}/{body['group_id']}"
        assert body["role"]

    def test_me_without_token(self, client: TestClient) -> None:
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_with_invalid_token(self, client: TestClient) -> None:
        resp = client.get("/auth/me", headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401

    def test_me_with_api_key(self, api_key_client: TestClient) -> None:
        # Mint an API key via JWT, then call /me with the API key bearer.
        jwt_token = _login(api_key_client)
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json={"name": "laptop", "ttl": "30d"},
        )
        assert created.status_code == 201
        api_key_token = created.json()["token"]
        api_key_id = created.json()["id"]

        resp = api_key_client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {api_key_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "peter"
        assert body["auth_kind"] == "api_key"
        assert body["api_key_id"] == api_key_id
        assert body["expires_at"] is not None
        assert body["issued_at"] is not None

    def test_review_stats_accepts_api_key(self, api_key_client: TestClient) -> None:
        """Regression for issue #92 — Bran reported API keys minted via
        ``POST /auth/api-keys`` worked on ``/propose`` but 401'd with
        ``Invalid or expired token`` on ``/review/stats``.

        ``/review/stats`` uses ``Depends(require_admin)`` which chains
        through ``get_current_user``. Pre-PR-#99 ``get_current_user``
        only verified JWTs, so an API-key bearer hit the JWT failure
        path. Lock that in: an API key issued to an admin user must
        be accepted on ``/review/stats``.
        """
        # Seed an admin user, get a JWT, mint an API key with it.
        from cq_server.app import _get_store

        _login(api_key_client)
        _get_store().sync.set_user_role("peter", "admin")
        jwt_token = api_key_client.post(
            "/auth/login",
            json={"username": "peter", "password": "secret123"},
        ).json()["token"]
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json={"name": "regression-92", "ttl": "30d"},
        )
        assert created.status_code == 201
        api_key_token = created.json()["token"]
        assert api_key_token.startswith("cqa.v1.")

        resp = api_key_client.get(
            "/api/v1/review/stats",
            headers={"Authorization": f"Bearer {api_key_token}"},
        )
        # Pre-fix: 401 "Invalid or expired token". Post-fix: 200.
        assert resp.status_code == 200, resp.text
        assert "counts" in resp.json()

    def test_get_current_user_accepts_api_key(self, api_key_client: TestClient) -> None:
        """Regression for issue #86 — Bran-style API-key call against a
        ``get_current_user``-protected endpoint must not fall back to the
        JWT path's ``Invalid or expired token`` error.

        Before the fix: ``get_current_user`` only verified JWTs; an API
        key (``cqa.v1.<keyid>.<secret>``) was passed to PyJWT which raised
        ``DecodeError`` and the caller saw 401 ``Invalid or expired token``.
        After: ``get_current_user`` delegates to ``_resolve_caller`` and
        accepts both bearer shapes. The consults inbox endpoint is the
        canonical user-protected route to exercise this through.
        """
        jwt_token = _login(api_key_client)
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json={"name": "regression-86", "ttl": "30d"},
        )
        assert created.status_code == 201
        api_key_token = created.json()["token"]
        # Sanity: this token would have triggered Bran's failure mode pre-fix.
        assert api_key_token.startswith("cqa.v1.")

        # /consults/inbox uses get_current_user (JWT-only pre-fix).
        resp = api_key_client.get(
            "/api/v1/consults/inbox",
            headers={"Authorization": f"Bearer {api_key_token}"},
        )
        # Pre-fix: 401 "Invalid or expired token". Post-fix: 200 with empty inbox.
        assert resp.status_code == 200, resp.text
        assert resp.json()["self_l2_id"]  # tenancy populated from API-key user


@pytest.fixture()
def api_key_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Client fixture with real API key enforcement (no dep override)."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, username: str = "peter", password: str = "secret123") -> str:
    _seed_user(client, username=username, password=password)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["token"]


class TestApiKeyCreate:
    def test_create_returns_plaintext_once(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        resp = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "laptop", "ttl": "30d"},
        )
        assert resp.status_code == 201
        body = resp.json()
        parts = body["token"].split(".")
        assert parts[0] == "cqa"
        assert parts[1] == "v1"
        assert len(parts[2]) == 32
        assert len(parts[3]) == 52
        assert body["prefix"] == parts[3][:8]
        assert body["name"] == "laptop"
        assert body["is_active"] is True
        assert body["is_expired"] is False

    def test_create_requires_jwt(self, api_key_client: TestClient) -> None:
        resp = api_key_client.post("/auth/api-keys", json={"name": "x", "ttl": "30d"})
        assert resp.status_code == 401

    def test_create_rejects_invalid_ttl(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        resp = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "x", "ttl": "3mo"},
        )
        assert resp.status_code == 422

    def test_create_rejects_empty_name(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        resp = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "", "ttl": "30d"},
        )
        assert resp.status_code == 422

    def test_create_hits_max_active_cap(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        for i in range(20):
            resp = api_key_client.post(
                "/auth/api-keys",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": f"k{i}", "ttl": "30d"},
            )
            assert resp.status_code == 201
        resp = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "k21", "ttl": "30d"},
        )
        assert resp.status_code == 409


class TestApiKeyList:
    def test_list_hides_plaintext(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "laptop", "ttl": "30d"},
        )
        resp = api_key_client.get("/auth/api-keys", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert len(body["data"]) == 1
        assert "token" not in body["data"][0]
        assert body["data"][0]["name"] == "laptop"

    def test_list_requires_jwt(self, api_key_client: TestClient) -> None:
        resp = api_key_client.get("/auth/api-keys")
        assert resp.status_code == 401

    def test_list_scoped_to_caller(self, api_key_client: TestClient) -> None:
        token_a = _login(api_key_client, username="alice")
        token_b = _login(api_key_client, username="bob")
        api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"name": "alice-key", "ttl": "30d"},
        )
        api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"name": "bob-key", "ttl": "30d"},
        )
        resp = api_key_client.get("/auth/api-keys", headers={"Authorization": f"Bearer {token_a}"})
        body = resp.json()
        assert body["count"] == 1
        assert [k["name"] for k in body["data"]] == ["alice-key"]


class TestApiKeyRevoke:
    def test_revoke_success(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "x", "ttl": "30d"},
        ).json()
        resp = api_key_client.post(
            f"/auth/api-keys/{created['id']}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"message": "API key revoked."}

        body = api_key_client.get("/auth/api-keys", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["data"][0]["revoked_at"] is not None
        assert body["data"][0]["is_active"] is False

    def test_revoke_is_idempotent(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "x", "ttl": "30d"},
        ).json()
        first = api_key_client.post(
            f"/auth/api-keys/{created['id']}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        second = api_key_client.post(
            f"/auth/api-keys/{created['id']}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert first.status_code == 200
        assert second.status_code == 200

    def test_revoke_other_users_key_returns_404(self, api_key_client: TestClient) -> None:
        token_a = _login(api_key_client, username="alice")
        token_b = _login(api_key_client, username="bob")
        created = api_key_client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"name": "alice-key", "ttl": "30d"},
        ).json()
        resp = api_key_client.post(
            f"/auth/api-keys/{created['id']}/revoke",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

    def test_revoke_unknown_key_returns_404(self, api_key_client: TestClient) -> None:
        token = _login(api_key_client)
        resp = api_key_client.post(
            "/auth/api-keys/nonexistent/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_revoke_requires_jwt(self, api_key_client: TestClient) -> None:
        resp = api_key_client.post("/auth/api-keys/anything/revoke")
        assert resp.status_code == 401
