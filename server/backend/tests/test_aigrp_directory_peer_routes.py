"""HTTP-level tests for ``POST /api/v1/admin/aigrp/directory-peerings`` (agent#347).

Cross-Enterprise sibling of ``test_aigrp_peer_routes.py``. Covers:

* Auth gating — non-admin caller gets 403; unauthenticated gets 401.
* Tenancy gate — body's ``enterprise`` MUST differ from the caller's
  tenancy (this is the cross-Enterprise endpoint). Same-Enterprise body
  is 422 with a "use /admin/aigrp/peers" pointer. Sentinel
  ``default-enterprise`` is also 422.
* Happy path — 201, row landed in ``aigrp_directory_peerings``,
  response carries the offer_id + expires_at + l2_id roster entry,
  ``cross_l2_audit`` row written with
  ``policy_applied='manual_directory_peer_announce'``.
* Idempotent re-announce — same ``(from, to, l2_id)`` synthesizes the
  same ``offer_id`` so re-paste upserts a single row.
* Roster shape — ``to_l2_endpoints_json`` decodes to a one-entry list
  with the L2's ``l2_id``, ``endpoint_url``, and ``pubkey`` — the
  exact shape ``consults._resolve_x_enterprise_target`` reads.
* Pubkey validation — non-base64url input is rejected with 422.
* L2-id decomposition — body's ``l2_id`` must equal
  ``<enterprise>/<group>``; mismatched form is 422.
* Resolver wire-up — after the announce, ``find_active_directory_peering``
  returns the row.
"""

from __future__ import annotations

import base64
import json
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

# The PEER L2 we're announcing — lives in Enterprise B (globex), so
# admin@acme is announcing a cross-Enterprise peering A -> B.
PEER_L2 = f"{ENT_B}/sga"
PEER_GROUP = "sga"
PEER_ENDPOINT = "https://sga.globex.example.com"
PEER_AAISN = "AS-65500"
# Realistic Ed25519 pubkey — 32 zero bytes, base64url, no padding.
PEER_PUBKEY_B64U = base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()

ENDPOINT_PATH = "/api/v1/admin/aigrp/directory-peerings"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "aigrp_directory_peers.db"))
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
        "enterprise": ENT_B,
        "group": PEER_GROUP,
        "endpoint_url": PEER_ENDPOINT,
        "pubkey": PEER_PUBKEY_B64U,
        "aaisn": PEER_AAISN,
        "ku_count": 1284,
        "domain_count": 42,
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_unauthenticated_gets_401(client: TestClient) -> None:
    resp = client.post(ENDPOINT_PATH, json=_valid_body())
    assert resp.status_code == 401, resp.text


def test_non_admin_gets_403(client: TestClient) -> None:
    headers = _login(client, NON_ADMIN)
    resp = client.post(ENDPOINT_PATH, headers=headers, json=_valid_body())
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Tenancy gate
# ---------------------------------------------------------------------------


def test_same_enterprise_body_is_422(client: TestClient) -> None:
    """admin@acme announcing a peer IN acme is the intra-Enterprise path."""
    headers = _login(client, ADMIN_A)
    body = _valid_body(
        l2_id=f"{ENT_A}/sga",
        enterprise=ENT_A,
        group="sga",
    )
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "use POST /admin/aigrp/peers" in detail


def test_default_enterprise_sentinel_body_is_422(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(
        l2_id="default-enterprise/sga",
        enterprise="default-enterprise",
        group="sga",
    )
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "default-enterprise" in resp.json()["detail"]


def test_l2_id_must_decompose_to_enterprise_group(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(l2_id=f"{ENT_B}/research")  # group mismatch
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "decompose" in resp.json()["detail"]


def test_l2_id_without_slash_is_422(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(l2_id="globex-sga")
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 422


def test_invalid_pubkey_base64_is_422(client: TestClient) -> None:
    """Pubkey field must parse as base64url; junk input lands a clean 422."""
    headers = _login(client, ADMIN_A)
    body = _valid_body(pubkey="!!!not-base64!!!@@@")
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 422, resp.text
    assert "pubkey" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_announce_happy_path_lands_row_and_audit(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    resp = client.post(ENDPOINT_PATH, headers=headers, json=_valid_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Response shape
    assert body["from_enterprise"] == ENT_A
    assert body["to_enterprise"] == ENT_B
    assert body["status"] == "active"
    assert body["l2_id"] == PEER_L2
    assert body["endpoint_url"] == PEER_ENDPOINT
    assert body["pubkey"] == PEER_PUBKEY_B64U
    assert body["aaisn"] == PEER_AAISN
    assert body["offer_id"] == f"manual:{ENT_A}:{ENT_B}:{PEER_L2}"
    assert body["active_from"]
    assert body["expires_at"]
    assert body["expires_at"] > body["active_from"]
    assert body["audit_id"]

    # DB-level confirmation — the peering row exists.
    store = _get_store()
    peerings = store.sync.list_directory_peerings(enterprise_id=ENT_A)
    rows = [p for p in peerings if p["offer_id"] == body["offer_id"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["from_enterprise"] == ENT_A
    assert row["to_enterprise"] == ENT_B
    assert row["status"] == "active"
    assert row["content_policy"] == "manual"
    assert row["consult_logging_policy"] == "manual"

    # The roster carries the announce in the exact shape consults.py reads.
    endpoints = json.loads(row["to_l2_endpoints_json"])
    assert len(endpoints) == 1
    ep = endpoints[0]
    assert ep["l2_id"] == PEER_L2
    assert ep["endpoint_url"] == PEER_ENDPOINT
    assert ep["pubkey"] == PEER_PUBKEY_B64U
    assert ep["aaisn"] == PEER_AAISN

    # Audit-log row landed with the manual policy tag.
    with store._engine.connect() as conn:  # noqa: SLF001
        audit_rows = conn.execute(
            text(
                "SELECT policy_applied, responder_l2_id, requester_persona, "
                "requester_enterprise, responder_enterprise "
                "FROM cross_l2_audit WHERE audit_id = :id"
            ),
            {"id": body["audit_id"]},
        ).fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0][0] == "manual_directory_peer_announce"
    assert audit_rows[0][1] == PEER_L2
    assert audit_rows[0][2] == ADMIN_A
    assert audit_rows[0][3] == ENT_A
    assert audit_rows[0][4] == ENT_B


def test_resolver_finds_active_peering_after_announce(client: TestClient) -> None:
    """The whole point of #347 — ``find_active_directory_peering`` returns the row."""
    headers = _login(client, ADMIN_A)
    resp = client.post(ENDPOINT_PATH, headers=headers, json=_valid_body())
    assert resp.status_code == 201, resp.text

    store = _get_store()
    # Direct sync probe of the resolver method consults.py uses.
    peering = store.sync._find_active_directory_peering_sync(  # noqa: SLF001
        from_enterprise=ENT_A,
        to_enterprise=ENT_B,
    )
    assert peering is not None
    assert peering["status"] == "active"
    endpoints = json.loads(peering["to_l2_endpoints_json"])
    assert any(e["l2_id"] == PEER_L2 for e in endpoints)


def test_optional_aaisn_omitted(client: TestClient) -> None:
    """AAISN is optional — the table has no aaisn column, the roster handles it."""
    headers = _login(client, ADMIN_A)
    body = _valid_body()
    del body["aaisn"]
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    j = resp.json()
    assert j["aaisn"] is None

    store = _get_store()
    peerings = store.sync.list_directory_peerings(enterprise_id=ENT_A)
    row = next(p for p in peerings if p["offer_id"] == j["offer_id"])
    endpoints = json.loads(row["to_l2_endpoints_json"])
    assert "aaisn" not in endpoints[0]


def test_ttl_days_override(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    body = _valid_body(ttl_days=7)
    resp = client.post(ENDPOINT_PATH, headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    j = resp.json()
    # 7 days < 30 days (default) — sanity check that the override stuck.
    # Compare ISO strings directly: 7-day delta will start with a date
    # earlier than the 30-day default.
    from datetime import UTC, datetime, timedelta

    active = datetime.fromisoformat(j["active_from"])
    expires = datetime.fromisoformat(j["expires_at"])
    delta = expires - active
    # Use a tolerance window to absorb any microsecond drift between
    # the two now() calls inside the handler.
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)
    # And it's strictly less than the default 30-day TTL.
    assert delta < timedelta(days=30)
    # And it's in the future of the now-clock the test runs against.
    assert expires > datetime.now(UTC)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_announce_is_idempotent_same_offer_id(client: TestClient) -> None:
    headers = _login(client, ADMIN_A)
    first = client.post(ENDPOINT_PATH, headers=headers, json=_valid_body())
    assert first.status_code == 201, first.text
    j1 = first.json()

    # Re-announce with the same l2_id, different endpoint URL.
    body2 = _valid_body(endpoint_url="https://sga-v2.globex.example.com")
    second = client.post(ENDPOINT_PATH, headers=headers, json=body2)
    assert second.status_code == 201, second.text
    j2 = second.json()

    # Deterministic offer_id means the same row was upserted.
    assert j1["offer_id"] == j2["offer_id"]

    # Only one row exists; endpoint URL was updated.
    store = _get_store()
    peerings = [p for p in store.sync.list_directory_peerings(enterprise_id=ENT_A) if p["offer_id"] == j1["offer_id"]]
    assert len(peerings) == 1
    endpoints = json.loads(peerings[0]["to_l2_endpoints_json"])
    assert endpoints[0]["endpoint_url"] == "https://sga-v2.globex.example.com"


def test_independent_peerings_get_distinct_offer_ids(client: TestClient) -> None:
    """Two different peer L2s in the same Enterprise B land two rows."""
    headers = _login(client, ADMIN_A)
    first = client.post(ENDPOINT_PATH, headers=headers, json=_valid_body())
    assert first.status_code == 201, first.text

    body2 = _valid_body(
        l2_id=f"{ENT_B}/research",
        group="research",
        endpoint_url="https://research.globex.example.com",
    )
    second = client.post(ENDPOINT_PATH, headers=headers, json=body2)
    assert second.status_code == 201, second.text

    j1 = first.json()
    j2 = second.json()
    assert j1["offer_id"] != j2["offer_id"]

    store = _get_store()
    peerings = store.sync.list_directory_peerings(enterprise_id=ENT_A)
    ids = {p["offer_id"] for p in peerings}
    assert j1["offer_id"] in ids
    assert j2["offer_id"] in ids
