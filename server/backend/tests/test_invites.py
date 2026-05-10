"""Tests for FO-1b magic-link invites.

Covers:

* mint → email captured by ``MockEmailSender``
* validate-good / validate-bad-signature / validate-expired
* claim-once-succeeds / double-claim-fails-409
* expired-fails-410 / revoked-fails-410
* list with status filter (pending / claimed / revoked)
* admin-only gating on POST /api/v1/admin/invites
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app
from cq_server.auth import _get_jwt_secret, hash_password
from cq_server.email_sender import MockEmailSender
from cq_server.invite_routes import get_email_sender
from cq_server.invites import (
    INVITE_AUDIENCE,
    INVITE_ISSUER,
    claim_invite,
    list_invites,
    mint_invite,
    revoke_invite,
    validate_invite_jwt,
)

ADMIN = "admin@8th-layer"
NON_ADMIN = "regular@8th-layer"
INVITEE_EMAIL = "newuser@example.com"


@pytest.fixture
def mock_sender() -> MockEmailSender:
    return MockEmailSender()


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sender: MockEmailSender,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "invites.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PUBLIC_HOST", "https://test.8th-layer.ai")

    app.dependency_overrides[get_email_sender] = lambda: mock_sender
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN, pw)
        store.sync.create_user(NON_ADMIN, pw)
        store.sync.set_user_role(ADMIN, "admin")
        yield c
    app.dependency_overrides.pop(get_email_sender, None)


def _login(client: TestClient, username: str, password: str = "password123") -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _admin_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, ADMIN)}"}


def _non_admin_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, NON_ADMIN)}"}


# ---------------------------------------------------------------------------
# Unit-level tests against the invites module.
# ---------------------------------------------------------------------------


class TestMintAndValidate:
    def _admin_id(self, store: object) -> int:
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": ADMIN},
            ).fetchone()
        assert row is not None
        return int(row[0])

    def test_mint_returns_jwt_with_correct_claims(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        invite, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        payload = pyjwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=["HS256"],
            audience=INVITE_AUDIENCE,
            issuer=INVITE_ISSUER,
        )
        assert payload["sub"] == INVITEE_EMAIL
        assert payload["role"] == "user"
        assert payload["target_l2_id"] == "acme/eng"
        assert payload["jti"] == invite.jti

    def test_validate_good_token(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        invite, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="l2_admin",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        result = validate_invite_jwt(token, store)
        assert result is not None
        assert result.id == invite.id
        assert result.email == INVITEE_EMAIL

    def test_validate_bad_signature(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        _, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        # Tamper the signature.
        head, payload, _sig = token.rsplit(".", 2)
        bad = f"{head}.{payload}.AAAAAAAAAAAAAAAAAAAA"
        assert validate_invite_jwt(bad, store) is None

    def test_validate_expired(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        _, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
            ttl_hours=0,
        )
        time.sleep(1.1)
        assert validate_invite_jwt(token, store) is None


class TestClaim:
    def _admin_id(self, store: object) -> int:
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": ADMIN},
            ).fetchone()
        assert row is not None
        return int(row[0])

    def test_claim_once_succeeds(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        _, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        # Pretend a user already exists; provide their id.
        store.sync.create_user("claimer", hash_password("password123"))
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": "claimer"},
            ).fetchone()
        assert row is not None
        outcome = claim_invite(store, token=token, claiming_user_id=int(row[0]))
        assert outcome.kind == "ok"
        assert outcome.invite is not None
        assert outcome.invite.claimed_at is not None

    def test_double_claim_fails(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        _, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        store.sync.create_user("claimer", hash_password("password123"))
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": "claimer"},
            ).fetchone()
        assert row is not None
        first = claim_invite(store, token=token, claiming_user_id=int(row[0]))
        assert first.kind == "ok"
        second = claim_invite(store, token=token, claiming_user_id=int(row[0]))
        assert second.kind == "already_claimed"

    def test_claim_after_revoke_fails(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        invite, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        revoke_invite(store, invite_id=invite.id, by_user_id=admin_id)
        store.sync.create_user("claimer", hash_password("password123"))
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": "claimer"},
            ).fetchone()
        assert row is not None
        outcome = claim_invite(store, token=token, claiming_user_id=int(row[0]))
        assert outcome.kind == "revoked"

    def test_claim_expired_fails(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)
        _, token = mint_invite(
            store,
            email=INVITEE_EMAIL,
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
            ttl_hours=0,
        )
        time.sleep(1.1)
        store.sync.create_user("claimer", hash_password("password123"))
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": "claimer"},
            ).fetchone()
        assert row is not None
        outcome = claim_invite(store, token=token, claiming_user_id=int(row[0]))
        assert outcome.kind == "expired"


class TestList:
    def _admin_id(self, store: object) -> int:
        with store._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": ADMIN},
            ).fetchone()
        assert row is not None
        return int(row[0])

    def test_list_filter_by_status(self, client: TestClient) -> None:
        store = _get_store()
        admin_id = self._admin_id(store)

        # Pending
        mint_invite(
            store,
            email="pending@example.com",
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        # Revoked
        rev_invite, _ = mint_invite(
            store,
            email="revoked@example.com",
            role="user",
            target_l2_id="acme/eng",
            issued_by=admin_id,
        )
        revoke_invite(store, invite_id=rev_invite.id, by_user_id=admin_id)

        all_invites = list_invites(store)
        assert len(all_invites) == 2
        pending_only = list_invites(store, status="pending")
        assert len(pending_only) == 1
        assert pending_only[0].email == "pending@example.com"
        revoked_only = list_invites(store, status="revoked")
        assert len(revoked_only) == 1
        assert revoked_only[0].email == "revoked@example.com"


# ---------------------------------------------------------------------------
# HTTP-level tests.
# ---------------------------------------------------------------------------


class TestAdminInviteHTTP:
    def test_mint_sends_email_and_omits_jwt(
        self,
        client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        resp = client.post(
            "/api/v1/admin/invites",
            json={
                "email": INVITEE_EMAIL,
                "role": "user",
                "target_l2_id": "acme/eng",
                "enterprise_name": "Acme",
            },
            headers=_admin_headers(client),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["email"] == INVITEE_EMAIL
        assert body["status"] == "pending"
        # No JWT bearer should appear in the response.
        assert "token" not in body
        assert "jwt" not in body
        # Email was captured.
        assert len(mock_sender.sent) == 1
        captured = mock_sender.sent[0]
        assert captured.to == INVITEE_EMAIL
        assert "Acme" in captured.subject
        assert ADMIN in captured.subject
        assert captured.claim_url.startswith("https://test.8th-layer.ai/invite/")

    def test_mint_requires_admin(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_non_admin_headers(client),
        )
        assert resp.status_code == 403

    def test_mint_unauthenticated(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
        )
        assert resp.status_code == 401

    def test_mint_target_l2_required_for_non_enterprise_admin(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user"},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 422

    def test_list_with_status_filter(
        self,
        client: TestClient,
        mock_sender: MockEmailSender,  # noqa: ARG002
    ) -> None:
        # Mint two; revoke one.
        for email in ("a@example.com", "b@example.com"):
            client.post(
                "/api/v1/admin/invites",
                json={"email": email, "role": "user", "target_l2_id": "acme/eng"},
                headers=_admin_headers(client),
            )
        resp = client.get(
            "/api/v1/admin/invites",
            headers=_admin_headers(client),
        )
        assert resp.status_code == 200
        all_body = resp.json()
        assert all_body["count"] == 2

        # Revoke the first one and confirm filter works.
        first_id = all_body["data"][-1]["id"]  # ordered DESC; oldest is last
        del_resp = client.delete(
            f"/api/v1/admin/invites/{first_id}",
            headers=_admin_headers(client),
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "revoked"

        pending_resp = client.get(
            "/api/v1/admin/invites?status=pending",
            headers=_admin_headers(client),
        )
        assert pending_resp.status_code == 200
        assert pending_resp.json()["count"] == 1

        revoked_resp = client.get(
            "/api/v1/admin/invites?status=revoked",
            headers=_admin_headers(client),
        )
        assert revoked_resp.status_code == 200
        assert revoked_resp.json()["count"] == 1


class TestPublicClaimHTTP:
    def test_get_metadata_then_claim(
        self,
        client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_admin_headers(client),
        )
        token = mock_sender.sent[0].jwt

        meta_resp = client.get(f"/api/v1/invites/{token}")
        assert meta_resp.status_code == 200, meta_resp.text
        meta = meta_resp.json()
        assert meta["email"] == INVITEE_EMAIL
        assert meta["role"] == "user"
        assert meta["inviter_username"] == ADMIN

        claim_resp = client.post(
            f"/api/v1/invites/{token}/claim",
            json={"username": "newcomer", "password": "password123"},
        )
        assert claim_resp.status_code == 200, claim_resp.text
        claim_body = claim_resp.json()
        assert claim_body["username"] == "newcomer"
        assert claim_body["token"]
        # FO-1c: claim response sets cq_session cookie.
        assert "cq_session" in claim_resp.cookies
        assert claim_resp.cookies["cq_session"] == claim_body["token"]

    def test_double_claim_returns_409(
        self,
        client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_admin_headers(client),
        )
        token = mock_sender.sent[0].jwt

        first = client.post(
            f"/api/v1/invites/{token}/claim",
            json={"username": "newcomer", "password": "password123"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/v1/invites/{token}/claim",
            json={"username": "newcomer2", "password": "password123"},
        )
        assert second.status_code == 409

    def test_expired_returns_410(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        mock_sender: MockEmailSender,
    ) -> None:
        monkeypatch.setenv("CQ_INVITE_TTL_HOURS", "0")
        client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_admin_headers(client),
        )
        token = mock_sender.sent[0].jwt
        time.sleep(1.1)

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 410
        claim_resp = client.post(
            f"/api/v1/invites/{token}/claim",
            json={"username": "newcomer", "password": "password123"},
        )
        assert claim_resp.status_code == 410

    def test_revoked_returns_410(
        self,
        client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        mint_resp = client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_admin_headers(client),
        )
        invite_id = mint_resp.json()["id"]
        token = mock_sender.sent[0].jwt
        client.delete(
            f"/api/v1/admin/invites/{invite_id}",
            headers=_admin_headers(client),
        )

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 410
        claim_resp = client.post(
            f"/api/v1/invites/{token}/claim",
            json={"username": "newcomer", "password": "password123"},
        )
        assert claim_resp.status_code == 410

    def test_bad_signature_returns_404(self, client: TestClient) -> None:
        # Build a token signed with a different secret.
        now = datetime.now(UTC)
        bogus = pyjwt.encode(
            {
                "sub": "fake@example.com",
                "role": "user",
                "target_l2_id": None,
                "iss": INVITE_ISSUER,
                "aud": INVITE_AUDIENCE,
                "iat": now,
                "exp": now + timedelta(hours=1),
                "jti": "deadbeef",
            },
            "wrong-secret",
            algorithm="HS256",
        )
        resp = client.get(f"/api/v1/invites/{bogus}")
        assert resp.status_code == 404


class TestOpenAPIVisibility:
    def test_invite_endpoints_in_openapi(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        # Routes are mounted under both / and /api/v1; assert the v1 mount.
        assert "/api/v1/admin/invites" in paths
        assert "/api/v1/admin/invites/{invite_id}" in paths
        assert "/api/v1/invites/{token}" in paths
        assert "/api/v1/invites/{token}/claim" in paths
