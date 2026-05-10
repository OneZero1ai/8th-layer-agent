"""Tests for FO-1c JWT aud-claim discriminant on /auth/me.

Covers:
* aud="session" tokens validate at /auth/me
* aud="invite" tokens are rejected at /auth/me (the whole point of the
  discriminant — an invite bearer must not authenticate as a user)
* legacy audless tokens accepted under the grace flag, rejected when
  CQ_DEPRECATED_AUDLESS_TOKENS=false
* legacy M-4 tokens (aud=self_l2_id()) accepted under the grace flag
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from cq_server import aigrp
from cq_server.app import _get_store, app

USERNAME = "alice"
PASSWORD = "password123"  # noqa: S105 — test fixture
SECRET = "test-secret-thirty-two-chars-min!"  # noqa: S105 — test fixture


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "aud.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", SECRET)
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        store = _get_store()
        store.sync.create_user(USERNAME, bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode())
        yield c


def _mint(payload: dict, secret: str = SECRET) -> str:
    """Sign a JWT with the given payload (no extra claims merged)."""
    return pyjwt.encode(payload, secret, algorithm="HS256")


class TestSessionAud:
    def test_session_aud_token_accepted(self, client: TestClient) -> None:
        # /auth/login mints aud="session" (FO-1c). Sanity that /auth/me works.
        login = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 200
        token = login.json()["token"]
        # Confirm the aud is what we expect.
        decoded = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_aud": False})
        assert decoded["aud"] == "session"
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["username"] == USERNAME


class TestInviteAudRejected:
    def test_invite_aud_rejected_at_auth_me(self, client: TestClient) -> None:
        """Critical: an invite-purpose token must not authenticate as a user."""
        now = datetime.now(UTC)
        invite_token = _mint(
            {
                "sub": USERNAME,
                "iss": "8th-layer.ai",
                "aud": "invite",
                "iat": now,
                "exp": now + timedelta(hours=1),
                "jti": "abc123",
            }
        )
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {invite_token}"})
        assert me.status_code == 401


class TestLegacyTokensGrace:
    def test_audless_token_accepted_during_grace(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Audless legacy tokens validate while CQ_DEPRECATED_AUDLESS_TOKENS=true (default)."""
        monkeypatch.delenv("CQ_DEPRECATED_AUDLESS_TOKENS", raising=False)
        now = datetime.now(UTC)
        legacy = _mint(
            {
                "sub": USERNAME,
                "iat": now,
                "exp": now + timedelta(hours=1),
            }
        )
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
        assert me.status_code == 200
        assert me.json()["username"] == USERNAME

    def test_audless_token_rejected_when_strict(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Audless tokens fail when the operator flips strict-mode."""
        monkeypatch.setenv("CQ_DEPRECATED_AUDLESS_TOKENS", "false")
        now = datetime.now(UTC)
        legacy = _mint(
            {
                "sub": USERNAME,
                "iat": now,
                "exp": now + timedelta(hours=1),
            }
        )
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
        assert me.status_code == 401

    def test_legacy_m4_token_accepted_during_grace(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pre-FO-1c M-4 shape (aud=self_l2_id) validates during grace."""
        monkeypatch.delenv("CQ_DEPRECATED_AUDLESS_TOKENS", raising=False)
        self_l2 = aigrp.self_l2_id()
        now = datetime.now(UTC)
        m4 = _mint(
            {
                "sub": USERNAME,
                "iat": now,
                "exp": now + timedelta(hours=1),
                "iss": self_l2,
                "aud": self_l2,
            }
        )
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {m4}"})
        assert me.status_code == 200
        assert me.json()["username"] == USERNAME

    def test_legacy_m4_token_rejected_when_strict(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_DEPRECATED_AUDLESS_TOKENS", "false")
        self_l2 = aigrp.self_l2_id()
        now = datetime.now(UTC)
        m4 = _mint(
            {
                "sub": USERNAME,
                "iat": now,
                "exp": now + timedelta(hours=1),
                "iss": self_l2,
                "aud": self_l2,
            }
        )
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {m4}"})
        assert me.status_code == 401


class TestUnitVerifyToken:
    """Direct unit tests of verify_token's grace logic, no FastAPI app."""

    def test_verify_session_aud(self) -> None:
        from cq_server.auth import create_token, verify_token

        token = create_token("alice", secret=SECRET, aud="session")
        payload = verify_token(token, secret=SECRET, expected_aud="session")
        assert payload["sub"] == "alice"
        assert payload["aud"] == "session"

    def test_verify_invite_aud_rejected_at_session(self) -> None:
        from cq_server.auth import verify_token

        now = datetime.now(UTC)
        invite_token = _mint(
            {
                "sub": "alice",
                "iss": "8th-layer.ai",
                "aud": "invite",
                "iat": now,
                "exp": now + timedelta(hours=1),
            }
        )
        with pytest.raises(pyjwt.InvalidAudienceError):
            verify_token(invite_token, secret=SECRET, expected_aud="session")

    def test_verify_audless_grace_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cq_server.auth import verify_token

        monkeypatch.delenv("CQ_DEPRECATED_AUDLESS_TOKENS", raising=False)
        now = datetime.now(UTC)
        token = _mint({"sub": "alice", "iat": now, "exp": now + timedelta(hours=1)})
        payload = verify_token(token, secret=SECRET, expected_aud="session")
        assert payload["sub"] == "alice"

    def test_verify_audless_grace_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cq_server.auth import verify_token

        monkeypatch.setenv("CQ_DEPRECATED_AUDLESS_TOKENS", "false")
        now = datetime.now(UTC)
        token = _mint({"sub": "alice", "iat": now, "exp": now + timedelta(hours=1)})
        with pytest.raises(pyjwt.MissingRequiredClaimError):
            verify_token(token, secret=SECRET, expected_aud="session")

    def test_verify_legacy_path_still_works(self) -> None:
        """expected_aud=None path mints+verifies in the M-4 shape."""
        from cq_server.auth import create_token, verify_token

        token = create_token("alice", secret=SECRET)  # no aud → legacy shape
        payload = verify_token(token, secret=SECRET)  # no expected_aud → legacy verify
        assert payload["sub"] == "alice"
