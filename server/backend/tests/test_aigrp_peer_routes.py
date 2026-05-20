"""HTTP-level tests for ``POST /api/v1/admin/aigrp/peers`` (agent#337).

Covers:

* Auth gating — non-admin caller gets 403.
* Tenancy gate — admin in Enterprise A POSTing with ``enterprise=B``
  is refused with 422 (cross-Enterprise insertion is reserved for the
  bilateral peering protocol, not this escape hatch).
* Happy path — 201, row landed in ``aigrp_peers``, response carries
  the timestamps the upsert populated, ``cross_l2_audit`` row written
  with ``policy_applied='manual_peer_announce'``.
* Idempotent re-announce — same ``l2_id`` upserts cleanly; ``first_seen_at``
  stays anchored to the original insert while ``last_seen_at`` advances.
* Centroid + Bloom encoding — ``embedding_centroid`` floats packed to
  little-endian float32 bytes; ``domain_bloom`` base64-decoded to BLOB.
* L2-id decomposition — body's ``l2_id`` must equal
  ``<enterprise>/<group>``; mismatched form is 422.
"""

from __future__ import annotations

import base64
import struct
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app

ENT_A = "acme"
ENT_B = "globex"
GRP_A = "engineering"
GRP_B = "engineering"

ADMIN_A = "admin@acme"
ADMIN_B = "admin@globex"
NON_ADMIN = "regular@acme"
PASSWORD = "password123!"  # pragma: allowlist secret

PEER_L2 = f"{ENT_A}/sga"
PEER_ENDPOINT = "https://sga.acme.example.com"
# Realistic Ed25519 pubkey shape — 32 bytes of zeros base64url-encoded.
PEER_PUBKEY_B64U = base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "aigrp_peers.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN_A, pw)
        store.sync.create_user(ADMIN_B, pw)
        store.sync.create_user(NON_ADMIN, pw)
        store.sync.set_user_role(ADMIN_A, "admin")
        store.sync.set_user_role(ADMIN_B, "admin")
        # Tenancy assignment — mirror the xgroup_consent test pattern.
        with store._engine.begin() as conn:  # noqa: SLF001
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": GRP_A, "u": ADMIN_A},
            )
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_B, "g": GRP_B, "u": ADMIN_B},
            )
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": GRP_A, "u": NON_ADMIN},
            )
        yield c


def _login(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _valid_body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "l2_id": PEER_L2,
        "enterprise": ENT_A,
        "group": "sga",
        "endpoint_url": PEER_ENDPOINT,
        "pubkey": PEER_PUBKEY_B64U,
        "ku_count": 0,
        "domain_count": 0,
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_non_admin_gets_403(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=_valid_body())
    assert resp.status_code == 403, resp.text


def test_unauthenticated_gets_401(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/aigrp/peers", json=_valid_body())
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Tenancy gate
# ---------------------------------------------------------------------------


def test_cross_enterprise_insertion_is_422(client: TestClient) -> None:
    """Admin in Enterprise A cannot announce a peer in Enterprise B."""
    headers = _login(client, ADMIN_A)
    body = _valid_body(
        l2_id=f"{ENT_B}/sga",
        enterprise=ENT_B,
        group="sga",
    )
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "cross-Enterprise peer insertion is refused" in resp.json()["detail"]


def test_l2_id_must_decompose_to_enterprise_group(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(l2_id=f"{ENT_A}/research")  # group mismatch
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "decompose" in resp.json()["detail"]


def test_l2_id_without_slash_is_422(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(l2_id="acme-sga")
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_announce_happy_path_lands_row_and_audit(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=_valid_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["l2_id"] == PEER_L2
    assert body["enterprise"] == ENT_A
    assert body["group"] == "sga"
    assert body["endpoint_url"] == PEER_ENDPOINT
    assert body["public_key_ed25519"] == PEER_PUBKEY_B64U
    # Upsert populates both first_seen and last_signature_at (signature_received=True).
    assert body["first_seen_at"]
    assert body["last_seen_at"]
    assert body["last_signature_at"]
    assert body["audit_id"]

    # DB-level confirmation — the row is queryable via the store API.
    store = _get_store()
    peers = store.sync.list_aigrp_peers(ENT_A)
    assert any(p["l2_id"] == PEER_L2 for p in peers)

    # Audit-log row landed with the manual policy tag.
    with store._engine.connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            text("SELECT policy_applied, responder_l2_id, requester_persona FROM cross_l2_audit WHERE audit_id = :id"),
            {"id": body["audit_id"]},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "manual_peer_announce"
    assert rows[0][1] == PEER_L2
    assert rows[0][2] == ADMIN_A


def test_announce_packs_centroid_and_decodes_bloom(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    centroid = [0.5, 0.25, -0.125, 0.0]
    bloom_bytes = b"\x01\x02\x03\x04\x05"
    bloom_b64 = base64.b64encode(bloom_bytes).decode()
    body = _valid_body(
        embedding_centroid=centroid,
        domain_bloom=bloom_b64,
        embedding_model="test-embedder/v1",
        ku_count=12,
        domain_count=4,
    )
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 201, resp.text

    store = _get_store()
    peers = store.sync.list_aigrp_peers(ENT_A)
    row = next(p for p in peers if p["l2_id"] == PEER_L2)
    # Centroid round-trips through little-endian float32.
    assert row["embedding_centroid"] == struct.pack("<4f", *centroid)
    assert row["domain_bloom"] == bloom_bytes
    assert row["embedding_model"] == "test-embedder/v1"
    assert row["ku_count"] == 12
    assert row["domain_count"] == 4


def test_invalid_pubkey_base64_is_400(client: TestClient) -> None:
    """Non-base64url ``pubkey`` lands the canonical 400 invalid_pubkey body (#346 concern 1)."""
    headers = _login(client, ADMIN_A)
    body = _valid_body(pubkey="!!!not-base64!!!@@@")
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "invalid_pubkey",
        "detail": "pubkey must be base64url-encoded Ed25519 public key (32 bytes)",
    }


def test_invalid_pubkey_wrong_length_is_400(client: TestClient) -> None:
    """A b64u-decodable string that isn't 32 bytes is rejected (#346 concern 1)."""
    headers = _login(client, ADMIN_A)
    short_pubkey = base64.urlsafe_b64encode(b"\x01" * 16).rstrip(b"=").decode()
    body = _valid_body(pubkey=short_pubkey)
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_pubkey"


def test_invalid_bloom_base64_is_422(client: TestClient) -> None:
    """Non-alphabet chars in ``domain_bloom`` land a clean 422.

    ``validate=True`` on ``base64.b64decode`` rejects characters outside
    the base64 alphabet — guards against admins pasting raw bytes into
    a field that the receiver will treat as a Bloom filter.
    """
    headers = _login(client, ADMIN_A)
    body = _valid_body(domain_bloom="!!!not-base64!!!@@@")
    resp = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "domain_bloom" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_announce_is_idempotent_keeps_first_seen(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    first = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=_valid_body())
    assert first.status_code == 201, first.text
    first_seen_initial = first.json()["first_seen_at"]

    # Re-announce with the same l2_id; endpoint URL change should land
    # but first_seen_at must remain anchored.
    body2 = _valid_body(endpoint_url="https://sga-v2.acme.example.com")
    second = client.post("/api/v1/admin/aigrp/peers", headers=headers, json=body2)
    assert second.status_code == 201, second.text
    j2 = second.json()
    assert j2["first_seen_at"] == first_seen_initial
    assert j2["endpoint_url"] == "https://sga-v2.acme.example.com"

    # Only one row, not two.
    store = _get_store()
    peers = [p for p in store.sync.list_aigrp_peers(ENT_A) if p["l2_id"] == PEER_L2]
    assert len(peers) == 1
