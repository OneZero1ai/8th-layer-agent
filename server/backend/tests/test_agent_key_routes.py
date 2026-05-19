"""Tests for FO-4 Self-service Add Agent endpoints (agent#194).

Covers:
* auth gating — non-admin gets 403 on mint + list
* POST /admin/agent-keys — happy path: token shape, install paths, persona
* POST — 409 on duplicate agent name, 422 on unusable name / bad TTL
* GET /admin/agent-keys — empty + populated; never leaks plaintext
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app

ADMIN = "admin@agentkey-test"
NON_ADMIN = "user@agentkey-test"
PASSWORD = "password123!"  # pragma: allowlist secret

# Matches the cqa.v1.<32-hex>.<52-char-secret> token format.
_TOKEN_RE = re.compile(r"^cqa\.v1\.[a-f0-9]{32}\.[a-z2-7]{52}$")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "agentkeys.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PUBLIC_HOST", "https://test.8th-layer.ai")

    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN, pw)
        store.sync.create_user(NON_ADMIN, pw)
        store.sync.set_user_role(ADMIN, "admin")
        yield c


def _login(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _mint(client: TestClient, headers: dict[str, str], name: str, **over: object) -> object:
    body = {"agent_name": name, "harness": "claude-code"}
    body.update(over)
    return client.post("/api/v1/admin/agent-keys", headers=headers, json=body)


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_mint_requires_admin(client: TestClient) -> None:
    resp = _mint(client, _login(client, NON_ADMIN), "Build Bot")
    assert resp.status_code == 403


def test_list_requires_admin(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/agent-keys", headers=_login(client, NON_ADMIN))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /admin/agent-keys
# ---------------------------------------------------------------------------


def test_mint_happy_path(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    resp = _mint(client, headers, "Build Bot")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert _TOKEN_RE.match(body["token"]), body["token"]
    assert body["agent_username"] == "agent-build-bot"
    assert body["name"] == "Build Bot"
    assert "harness:claude-code" in body["labels"]
    assert body["is_active"] is True

    install = body["install"]
    assert install["persona"] == "agent"
    assert body["token"] in install["join_command"]
    assert install["join_command"].startswith("8l join --enterprise ")
    assert "--persona agent" in install["join_command"]


def test_mint_duplicate_name_conflicts(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    assert _mint(client, headers, "Dup Agent").status_code == 201
    resp = _mint(client, headers, "Dup Agent")
    assert resp.status_code == 409


def test_mint_unusable_name_rejected(client: TestClient) -> None:
    resp = _mint(client, _login(client, ADMIN), "!!!")
    assert resp.status_code == 422


def test_mint_bad_ttl_rejected(client: TestClient) -> None:
    resp = _mint(client, _login(client, ADMIN), "Ttl Agent", ttl="banana")
    assert resp.status_code == 422


def test_mint_default_ttl_is_60d(client: TestClient) -> None:
    resp = _mint(client, _login(client, ADMIN), "Default Ttl")
    assert resp.status_code == 201
    assert resp.json()["ttl"] == "60d"


# ---------------------------------------------------------------------------
# GET /admin/agent-keys
# ---------------------------------------------------------------------------


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/agent-keys", headers=_login(client, ADMIN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["count"] == 0


def test_list_populated_never_leaks_plaintext(client: TestClient) -> None:
    headers = _login(client, ADMIN)
    _mint(client, headers, "Agent One")
    _mint(client, headers, "Agent Two")

    resp = client.get("/api/v1/admin/agent-keys", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    usernames = {row["agent_username"] for row in body["data"]}
    assert usernames == {"agent-agent-one", "agent-agent-two"}
    # The plaintext token is returned only by the mint response, never here.
    for row in body["data"]:
        assert "token" not in row
        assert row["prefix"]  # 8-char display prefix is present
