"""HTTP-level tests for /api/v1/admin/xgroup_consent/* (Phase 1.0b).

Two angles:

1. Smoke — propose with a valid signature lands a 201 + pending row.
2. Tenancy gate (defence-in-depth on top of body-level signatures) —
   an admin in Enterprise A cannot drive a grant whose enterprise_id is
   Enterprise B, even if the body's signature would otherwise verify.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server import xgroup_consent as xgc
from cq_server.app import _get_store, app
from cq_server.crypto import canonicalize, public_key_b64u, sign_raw

ENT_A = "acme"
ENT_B = "globex"
SRC = "acme/engineering"
DST = "acme/sga"
ADMIN_A = "admin@acme"
ADMIN_B = "admin@globex"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "xg.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN_A, pw)
        store.sync.create_user(ADMIN_B, pw)
        store.sync.set_user_role(ADMIN_A, "admin")
        store.sync.set_user_role(ADMIN_B, "admin")
        # Set tenancy directly via UPDATE — the public store API doesn't
        # expose a setter for users.enterprise_id / group_id.
        with store._engine.begin() as conn:  # noqa: SLF001
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": "engineering", "u": ADMIN_A},
            )
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_B, "g": "engineering", "u": ADMIN_B},
            )
        yield c


def _login(client: TestClient, username: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "pw"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _build_signed_propose(*, enterprise_id: str, source_l2: str, target_l2: str) -> tuple[dict, Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    pk = public_key_b64u(sk)
    rec_sk = Ed25519PrivateKey.generate()
    rec_pk = public_key_b64u(rec_sk)
    issued = datetime.now(UTC)
    body = xgc.build_grant_body(
        enterprise_id=enterprise_id,
        source_l2=source_l2,
        target_l2=target_l2,
        scope_kind="domains",
        scope_values=["aws"],
        issued_at=issued.isoformat(),
        expires_at=(issued + timedelta(days=10)).isoformat(),
        recovery_operator_pubkey_b64u=rec_pk,
    )
    sig = sign_raw(sk, canonicalize(body))
    return body, sk, sig, pk  # type: ignore[return-value]


class TestProposeSmoke:
    def test_propose_lands_201(self, client: TestClient) -> None:
        body, _sk, sig, pk = _build_signed_propose(enterprise_id=ENT_A, source_l2=SRC, target_l2=DST)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/xgroup_consent/propose",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "body": body,
                "proposer_l2": SRC,
                "proposer_pubkey_b64u": pk,
                "proposer_signature_b64u": sig,
            },
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["status"] == "proposed"
        assert "pending_id" in out


class TestTenancyGate:
    def test_admin_cannot_propose_for_other_enterprise(self, client: TestClient) -> None:
        # Build a body for Enterprise B but call the API as ADMIN_A.
        body, _sk, sig, pk = _build_signed_propose(enterprise_id=ENT_B, source_l2="globex/x", target_l2="globex/y")
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/xgroup_consent/propose",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "body": body,
                "proposer_l2": "globex/x",
                "proposer_pubkey_b64u": pk,
                "proposer_signature_b64u": sig,
            },
        )
        assert resp.status_code == 403, resp.text
        assert "enterprise" in resp.json()["detail"].lower()

    def test_admin_cannot_list_pending_for_other_enterprise(self, client: TestClient) -> None:
        token = _login(client, ADMIN_A)
        resp = client.get(
            "/api/v1/admin/xgroup_consent/pending",
            headers={"Authorization": f"Bearer {token}"},
            params={"enterprise_id": ENT_B, "target_l2": "globex/y"},
        )
        assert resp.status_code == 403


class TestRequiresAdmin:
    def test_propose_requires_admin_role(self, client: TestClient, tmp_path: Path) -> None:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user("regular@acme", pw)
        with store._engine.begin() as conn:  # noqa: SLF001
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": "engineering", "u": "regular@acme"},
            )
        token = _login(client, "regular@acme")
        body, _sk, sig, pk = _build_signed_propose(enterprise_id=ENT_A, source_l2=SRC, target_l2=DST)
        resp = client.post(
            "/api/v1/admin/xgroup_consent/propose",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "body": body,
                "proposer_l2": SRC,
                "proposer_pubkey_b64u": pk,
                "proposer_signature_b64u": sig,
            },
        )
        assert resp.status_code == 403
