"""Regression tests for #90 — /propose tier resolution.

Pre-fix every KU proposed via the API got ``tier=private`` regardless
of any configuration, so KUs were invisible to anything querying
``public`` until manually retiered. This module exercises the three
allowed sources, in priority order:

1. Explicit ``tier`` field in the request body
2. ``CQ_DEFAULT_KU_TIER`` env var on the server
3. Fallback to ``private`` (preserves pre-fix behaviour)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.auth import hash_password


@pytest.fixture()
def client_factory(tmp_path: Path):
    def _factory(monkeypatch: pytest.MonkeyPatch, *, default_tier: str | None) -> TestClient:
        monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "tier.db"))
        monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
        monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
        if default_tier is None:
            monkeypatch.delenv("CQ_DEFAULT_KU_TIER", raising=False)
        else:
            monkeypatch.setenv("CQ_DEFAULT_KU_TIER", default_tier)
        return TestClient(app)

    return _factory


def _seed_user(client: TestClient) -> str:
    from cq_server.app import _get_store

    store = _get_store()
    store.sync.create_user("alice", hash_password("alice-pw-123"))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            ("team-dw", "engineering", "alice"),
        )
    jwt = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-pw-123"},
    ).json()["token"]
    key_resp = client.post(
        "/api/v1/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": "tier-test", "ttl": "30d"},
    )
    return key_resp.json()["token"]


def _propose(client: TestClient, token: str, *, tier: str | None = None) -> str:
    body = {
        "domains": ["postgres", "test"],
        "insight": {
            "summary": "Tier resolution exercise — explicit tier wins over env, env wins over fallback.",
            "detail": (
                "When a /propose request specifies a tier in the body, the server uses it; "
                "otherwise CQ_DEFAULT_KU_TIER selects the default; otherwise tier=private."
            ),
            "action": (
                "Set CQ_DEFAULT_KU_TIER=public on team L2s to make new KUs queryable by default; "
                "agents can still opt into private per-propose by passing tier=private in the body."
            ),
        },
    }
    if tier is not None:
        body["tier"] = tier
    resp = client.post(
        "/api/v1/propose",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _read_tier(unit_id: str) -> str:
    conn = sqlite3.connect(os.environ["CQ_DB_PATH"])
    try:
        row = conn.execute(
            "SELECT tier FROM knowledge_units WHERE id = ?", (unit_id,)
        ).fetchone()
        assert row is not None
        return row[0]
    finally:
        conn.close()


def test_default_tier_is_private(client_factory, monkeypatch):
    """No env, no body field → 'private' (backwards compatible)."""
    with client_factory(monkeypatch, default_tier=None) as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_tier(unit_id) == "private"


def test_env_override_public(client_factory, monkeypatch):
    """CQ_DEFAULT_KU_TIER=public flips the server-side default."""
    with client_factory(monkeypatch, default_tier="public") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_tier(unit_id) == "public"


def test_request_body_tier_wins_over_env(client_factory, monkeypatch):
    """Body `tier` takes precedence over the env var."""
    with client_factory(monkeypatch, default_tier="public") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token, tier="private")
    assert _read_tier(unit_id) == "private"


def test_unknown_env_falls_back_to_private(client_factory, monkeypatch):
    """Garbled env value falls back to private rather than 500'ing."""
    with client_factory(monkeypatch, default_tier="floof") as client:
        token = _seed_user(client)
        unit_id = _propose(client, token)
    assert _read_tier(unit_id) == "private"
