"""Tests for cold-start funnel signals emitted by cq-server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cq_server import directory_client as dc
from cq_server.app import _get_store, app
from cq_server.auth import get_current_user
from cq_server.deps import require_api_key
from cq_server.email_sender import MockEmailSender
from cq_server.invite_routes import get_email_sender

ADMIN = "admin@8th-layer"
TEST_USER = "test-user"
INVITEE_EMAIL = "founder@example.com"


@pytest.fixture(autouse=True)
def reset_first_ku_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "_first_ku_funnel_emitted", False, raising=False)


@pytest.fixture()
def privkey_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "enterprise-root.key"
    path.write_bytes(key.private_bytes_raw())
    monkeypatch.setenv("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", str(path))
    return path


@pytest.fixture()
def mock_sender() -> MockEmailSender:
    return MockEmailSender()


@pytest.fixture()
def invite_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sender: MockEmailSender,
    privkey_path: Path,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "funnel-invite.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PUBLIC_HOST", "https://test.8th-layer.ai")
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "true")
    monkeypatch.setenv("CQ_DIRECTORY_URL", "https://directory.test")

    app.dependency_overrides[get_email_sender] = lambda: mock_sender
    with TestClient(app) as c:
        store = _get_store()
        from cq_server.auth import hash_password

        store.sync.create_user(ADMIN, hash_password("password123"))
        store.sync.set_user_role(ADMIN, "admin")
        yield c
    app.dependency_overrides.pop(get_email_sender, None)


@pytest.fixture()
def propose_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sender: MockEmailSender,
    privkey_path: Path,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "funnel.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PUBLIC_HOST", "https://test.8th-layer.ai")
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "true")
    monkeypatch.setenv("CQ_DIRECTORY_URL", "https://directory.test")

    app.dependency_overrides[get_email_sender] = lambda: mock_sender
    app.dependency_overrides[require_api_key] = lambda: TEST_USER
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as c:
        store = _get_store()
        from cq_server.auth import hash_password

        store.sync.create_user(ADMIN, hash_password("password123"))
        store.sync.set_user_role(ADMIN, "admin")
        store.sync.create_user(TEST_USER, hash_password("password123"))
        yield c
    app.dependency_overrides.pop(get_email_sender, None)
    app.dependency_overrides.pop(require_api_key, None)
    app.dependency_overrides.pop(get_current_user, None)


def _login(client: TestClient, username: str = ADMIN) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _admin_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client)}"}


def _propose_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "domains": ["funnel-test"],
        "insight": {
            "summary": "First knowledge unit for cold-start funnel coverage.",
            "detail": "Detailed enough to pass the propose quality guard in tests.",
            "action": "Emit first_ku_proposed once to the directory.",
        },
    }
    base.update(overrides)
    return base


class TestInviteClaimFunnel:
    @patch("cq_server.invite_routes.schedule_funnel_event")
    def test_admin_claim_schedules_admin_claimed(
        self,
        mock_schedule: AsyncMock,
        invite_client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        invite_client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "enterprise_admin"},
            headers=_admin_headers(invite_client),
        )
        token = mock_sender.sent[0].jwt

        resp = invite_client.post(
            f"/api/v1/invites/{token}/claim",
            json={"password": "password123"},
        )
        assert resp.status_code == 200, resp.text
        mock_schedule.assert_called_once_with("admin_claimed")

    @patch("cq_server.invite_routes.schedule_funnel_event")
    def test_user_claim_does_not_schedule_admin_claimed(
        self,
        mock_schedule: AsyncMock,
        invite_client: TestClient,
        mock_sender: MockEmailSender,
    ) -> None:
        invite_client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "user", "target_l2_id": "acme/eng"},
            headers=_admin_headers(invite_client),
        )
        token = mock_sender.sent[0].jwt

        resp = invite_client.post(
            f"/api/v1/invites/{token}/claim",
            json={"password": "password123"},
        )
        assert resp.status_code == 200, resp.text
        mock_schedule.assert_not_called()

    @patch("cq_server.invite_routes.schedule_funnel_event")
    def test_claim_noop_when_directory_disabled(
        self,
        mock_schedule: AsyncMock,
        invite_client: TestClient,
        mock_sender: MockEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "false")
        invite_client.post(
            "/api/v1/admin/invites",
            json={"email": INVITEE_EMAIL, "role": "enterprise_admin"},
            headers=_admin_headers(invite_client),
        )
        token = mock_sender.sent[0].jwt

        resp = invite_client.post(
            f"/api/v1/invites/{token}/claim",
            json={"password": "password123"},
        )
        assert resp.status_code == 200, resp.text
        mock_schedule.assert_not_called()


class TestProposeFunnel:
    @patch("cq_server.app.schedule_funnel_event")
    def test_first_propose_schedules_first_ku_proposed(
        self,
        mock_schedule: AsyncMock,
        propose_client: TestClient,
    ) -> None:
        resp = propose_client.post("/propose", json=_propose_payload())
        assert resp.status_code == 201, resp.text
        mock_schedule.assert_called_once_with("first_ku_proposed", enterprise_id="acme")

    @patch("cq_server.app.schedule_funnel_event")
    def test_second_propose_does_not_schedule_again(
        self,
        mock_schedule: AsyncMock,
        propose_client: TestClient,
    ) -> None:
        first = propose_client.post("/propose", json=_propose_payload(domains=["funnel-a"]))
        assert first.status_code == 201
        second = propose_client.post("/propose", json=_propose_payload(domains=["funnel-b"]))
        assert second.status_code == 201
        assert mock_schedule.call_count == 1
        mock_schedule.assert_called_with("first_ku_proposed", enterprise_id="acme")

    @patch("cq_server.app.schedule_funnel_event")
    def test_propose_noop_when_directory_disabled(
        self,
        mock_schedule: AsyncMock,
        propose_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "false")
        resp = propose_client.post("/propose", json=_propose_payload())
        assert resp.status_code == 201, resp.text
        mock_schedule.assert_not_called()
