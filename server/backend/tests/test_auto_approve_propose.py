"""Regression tests for #123 — CQ_AUTO_APPROVE_PROPOSE env flag.

Discovered during TeamDW micro-reality test 2026-05-07: the cq plugin's
/cq:reflect proposes KUs that land at status='pending', but /query
filters to status='approved' only. Cross-persona retrieval was broken
until manual admin-approval per KU.

This flag is the L2-side fast-fix: solo-operator deployments
(TeamDW, sole-prop AAISNs) opt into auto-approve via
``CQ_AUTO_APPROVE_PROPOSE=true``. Multi-operator deployments leave the
flag unset; status='pending' is the default and Pass 2 Part 4 Ch 21's
review-queue ritual applies. Hard-finding KUs flow through the
dedicated pending_review tier (PR #121 / #103), unaffected.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.auth import hash_password


@pytest.fixture()
def client_factory(tmp_path: Path):
    """Return a factory that yields TestClient with configurable env."""

    def _factory(monkeypatch: pytest.MonkeyPatch, *, auto_approve: str | None) -> TestClient:
        monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "auto.db"))
        monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
        monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
        if auto_approve is None:
            monkeypatch.delenv("CQ_AUTO_APPROVE_PROPOSE", raising=False)
        else:
            monkeypatch.setenv("CQ_AUTO_APPROVE_PROPOSE", auto_approve)
        return TestClient(app)

    return _factory


def _seed_user(client: TestClient) -> str:
    """Seed a user via the same pattern as test_propose_tenancy_regression."""
    from cq_server.app import _get_store

    store = _get_store()
    store.sync.create_user("alice", hash_password("alice-pw-123"))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            ("team-dw", "engineering", "alice"),
        )

    jwt_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-pw-123"},
    )
    assert jwt_resp.status_code == 200, jwt_resp.text
    jwt = jwt_resp.json()["token"]

    # Propose requires API-key auth; mint one and return that.
    key_resp = client.post(
        "/api/v1/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": "auto-approve-test", "ttl": "30d"},
    )
    assert key_resp.status_code == 201, key_resp.text
    return key_resp.json()["token"]


def _propose(client: TestClient, token: str) -> str:
    """Propose a KU and return the new unit_id."""
    resp = client.post(
        "/api/v1/propose",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "domains": ["postgres", "test"],
            "insight": {
                "summary": "Postgres autovacuum_vacuum_scale_factor is percentage-based and tunes per table.",
                "detail": (
                    "When a table grows large the percentage threshold means autovacuum "
                    "fires far less often unless you tune scale_factor down."
                ),
                "action": (
                    "Set autovacuum_vacuum_scale_factor=0.05 on hot large tables; "
                    "pair with autovacuum_vacuum_threshold tuning."
                ),
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _read_status(unit_id: str) -> str:
    import os

    conn = sqlite3.connect(os.environ["CQ_DB_PATH"])
    try:
        row = conn.execute("SELECT status FROM knowledge_units WHERE id = ?", (unit_id,)).fetchone()
        assert row is not None, f"unit {unit_id} not found"
        return row[0]
    finally:
        conn.close()


def test_default_status_is_pending(client_factory, monkeypatch):
    """Without the env flag set, propose lands at status='pending'."""
    with client_factory(monkeypatch, auto_approve=None) as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_status(unit_id) == "pending"


def test_auto_approve_true_lands_at_approved(client_factory, monkeypatch):
    """CQ_AUTO_APPROVE_PROPOSE=true → propose lands at status='approved'."""
    with client_factory(monkeypatch, auto_approve="true") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_status(unit_id) == "approved"


def test_auto_approve_1_lands_at_approved(client_factory, monkeypatch):
    """CQ_AUTO_APPROVE_PROPOSE=1 (numeric truthy) also enables auto-approve."""
    with client_factory(monkeypatch, auto_approve="1") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_status(unit_id) == "approved"


def test_auto_approve_false_keeps_pending(client_factory, monkeypatch):
    """Explicit false (or any non-truthy value) leaves status='pending'."""
    with client_factory(monkeypatch, auto_approve="false") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_status(unit_id) == "pending"


def test_auto_approved_query_returns_unit(client_factory, monkeypatch):
    """Auto-approved KU is immediately surfaced by /query (the actual user-visible behavior)."""
    with client_factory(monkeypatch, auto_approve="true") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
        resp = client.get(
            "/api/v1/query?domains=postgres&limit=5",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()
        assert any(r["id"] == unit_id for r in results), (
            f"auto-approved unit {unit_id} not visible in query results: {results}"
        )


def test_no_flag_query_misses_pending_unit(client_factory, monkeypatch):
    """Without the flag, the pending KU is invisible to /query (the original bug)."""
    with client_factory(monkeypatch, auto_approve=None) as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
        resp = client.get(
            "/api/v1/query?domains=postgres&limit=5",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()
        assert not any(r["id"] == unit_id for r in results), (
            f"pending unit {unit_id} should NOT be visible to /query; this is the original bug"
        )
