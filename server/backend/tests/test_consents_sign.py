"""Phase 6 step 3 / Lane D: POST /consents/sign tests.

Pins:
  - Happy path inserts a consent + paired audit row.
  - Intra-Enterprise (req_ent == resp_ent) returns 422.
  - Unsupported policies return 422.
  - Duplicate active consent for same tuple returns 409.
  - Admin-only — non-admin users get 403.
  - expires_at is honored (not echoed as the row's signed_at).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app

ADMIN = "admin@acme"
USER = "regular-user"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "consents.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN, pw)
        store.sync.create_user(USER, pw)
        store.sync.set_user_role(ADMIN, "admin")
        yield c


def _login(client: TestClient, username: str) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "pw"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestSignHappyPath:
    def test_sign_returns_201_and_inserts_row(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        resp = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "engineering",
                "responder_group": "engineering",
                "policy": "summary_only",
                "expires_at": "2027-04-30T00:00:00+00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["consent_id"].startswith("consent_")
        assert body["signed_by_admin"] == ADMIN
        assert body["audit_log_id"].startswith("aud_")
        # Row landed.
        store = _get_store()
        row = store.sync.get_cross_enterprise_consent(body["consent_id"])
        assert row is not None
        assert row["requester_enterprise"] == "initech"
        assert row["responder_enterprise"] == "acme"
        assert row["policy"] == "summary_only"
        assert row["expires_at"] == "2027-04-30T00:00:00+00:00"

    def test_sign_writes_paired_audit_row(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        resp = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        assert resp.status_code == 201
        consent_id = resp.json()["consent_id"]
        store = _get_store()
        with store._engine.begin() as _c:
            rows = _c.exec_driver_sql(
                "SELECT policy_applied, consent_id FROM cross_l2_audit WHERE consent_id = ?",
                (consent_id,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "consent_signed"


class TestSignValidation:
    def test_intra_enterprise_returns_422(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        resp = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "acme",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        assert resp.status_code == 422
        assert "two distinct" in resp.json()["detail"]

    def test_unsupported_policy_returns_422(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        resp = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "full_body",
            },
        )
        assert resp.status_code == 422
        assert "summary_only" in resp.json()["detail"]


class TestSignDuplicate:
    def test_duplicate_active_tuple_returns_409(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        first = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "rd",
                "responder_group": "engineering",
                "policy": "summary_only",
            },
        )
        assert first.status_code == 201
        second = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "rd",
                "responder_group": "engineering",
                "policy": "summary_only",
            },
        )
        assert second.status_code == 409
        assert first.json()["consent_id"] in second.json()["detail"]

    def test_different_group_pair_is_not_duplicate(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        a = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "rd",
                "responder_group": "engineering",
                "policy": "summary_only",
            },
        )
        b = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "rd",
                "responder_group": "solutions",  # different
                "policy": "summary_only",
            },
        )
        assert a.status_code == 201
        assert b.status_code == 201

    def test_wildcard_and_specific_can_coexist(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        wild = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",  # null wildcards
            },
        )
        specific = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "requester_group": "rd",
                "responder_group": "engineering",
                "policy": "summary_only",
            },
        )
        assert wild.status_code == 201
        assert specific.status_code == 201


class TestSignAuth:
    def test_non_admin_returns_403(self, client: TestClient) -> None:
        jwt = _login(client, USER)
        resp = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        assert resp.status_code == 403

    def test_no_auth_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/consents/sign",
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        assert resp.status_code == 401


class TestList:
    def test_list_returns_signed_consents(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        resp = client.get(
            "/api/v1/consents",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["consents"][0]["responder_enterprise"] == "acme"

    def test_list_excludes_expired_by_default(self, client: TestClient) -> None:
        jwt = _login(client, ADMIN)
        # Sign + then manually backdate expires_at into the past.
        signed = client.post(
            "/api/v1/consents/sign",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "requester_enterprise": "initech",
                "responder_enterprise": "acme",
                "policy": "summary_only",
            },
        )
        cid = signed.json()["consent_id"]
        store = _get_store()
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE cross_enterprise_consents SET expires_at = ? WHERE consent_id = ?",
                ("2020-01-01T00:00:00+00:00", cid),
            )
        active = client.get(
            "/api/v1/consents",
            headers={"Authorization": f"Bearer {jwt}"},
        ).json()
        assert active["count"] == 0
        all_ = client.get(
            "/api/v1/consents",
            headers={"Authorization": f"Bearer {jwt}"},
            params={"include_expired": True},
        ).json()
        assert all_["count"] == 1

    def test_list_requires_admin(self, client: TestClient) -> None:
        jwt = _login(client, USER)
        resp = client.get(
            "/api/v1/consents",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert resp.status_code == 403
