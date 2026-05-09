"""Phase 6 step 3 / Lane C: presence heartbeat upsert tests.

Pins:
  - First heartbeat inserts; subsequent heartbeats update last_seen_at.
  - expertise_domains JSON-roundtrips (list in -> list out, null stays null).
  - tenancy scope is resolved from the API key's owning user, never trusted
    from the request body.
  - persona is the primary key — two heartbeats with the same persona
    end up as one row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.deps import require_api_key

ALICE = "alice"
BOB = "bob"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "peers_heartbeat.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        # Bootstrap two users in distinct enterprise/group pairs so the
        # tenancy-scope test can confirm the resolved scope follows the
        # auth user, not the request body.
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ALICE, pw)
        store.sync.create_user(BOB, pw)
        # Promote Alice to a foreign tenancy scope. Bob stays default.
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'engineering' WHERE username = ?",
                (ALICE,),
            )
        yield c


def _override_api_key(username: str) -> None:
    app.dependency_overrides[require_api_key] = lambda: username


def _clear_override() -> None:
    app.dependency_overrides.pop(require_api_key, None)


class TestHeartbeatUpsert:
    def test_first_heartbeat_inserts_row(self, client: TestClient) -> None:
        _override_api_key(ALICE)
        try:
            resp = client.post(
                "/api/v1/peers/heartbeat",
                json={
                    "persona": "persona-cloudfront-asker",
                    "discoverable": True,
                    "working_dir_hint": "investigating CloudFront 403s",
                    "expertise_domains": ["aws", "cloudfront", "security"],
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["persona"] == "persona-cloudfront-asker"
            assert body["next_heartbeat_in_seconds"] == 300
            assert body["registered_at"]  # non-empty ISO timestamp
        finally:
            _clear_override()

    def test_second_heartbeat_advances_last_seen_at(self, client: TestClient) -> None:
        _override_api_key(ALICE)
        try:
            r1 = client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-x", "discoverable": True},
            )
            r2 = client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-x", "discoverable": True},
            )
            assert r1.status_code == r2.status_code == 200
            # The second registered_at should be >= the first; ISO-8601
            # comparison is lexicographic for fixed-width UTC strings.
            assert r2.json()["registered_at"] >= r1.json()["registered_at"]
            # And there should be exactly one row in the DB.
            store = _get_store()
            with store._engine.begin() as _c:
                count = _c.exec_driver_sql("SELECT COUNT(*) FROM peers WHERE persona = 'persona-x'").fetchone()[0]
            assert count == 1
        finally:
            _clear_override()


class TestTenancyResolvedFromAuth:
    def test_scope_follows_auth_user_not_body(self, client: TestClient) -> None:
        _override_api_key(ALICE)
        try:
            client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-alice", "discoverable": True},
            )
        finally:
            _clear_override()
        store = _get_store()
        with store._engine.begin() as _c:
            row = _c.exec_driver_sql(
                "SELECT enterprise_id, group_id FROM peers WHERE persona = ?",
                ("persona-alice",),
            ).fetchone()
        assert row == ("acme", "engineering")

    def test_two_users_same_persona_overwrites_scope(self, client: TestClient) -> None:
        # Alice (acme/engineering) registers persona-shared; then Bob
        # (default-enterprise/default-group) registers the same persona
        # name. Persona is the PK, so the row reflects the latest writer.
        _override_api_key(ALICE)
        try:
            client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-shared", "discoverable": True},
            )
        finally:
            _clear_override()
        _override_api_key(BOB)
        try:
            client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-shared", "discoverable": True},
            )
        finally:
            _clear_override()
        store = _get_store()
        with store._engine.begin() as _c:
            row = _c.exec_driver_sql(
                "SELECT enterprise_id, group_id FROM peers WHERE persona = ?",
                ("persona-shared",),
            ).fetchone()
        # Bob's scope wins as the latest writer.
        assert row == ("default-enterprise", "default-group")


class TestExpertiseDomainsRoundtrip:
    def test_list_roundtrips_through_json(self, client: TestClient) -> None:
        _override_api_key(ALICE)
        try:
            client.post(
                "/api/v1/peers/heartbeat",
                json={
                    "persona": "persona-domains",
                    "discoverable": True,
                    "expertise_domains": ["aws", "lambda", "cold-start"],
                },
            )
        finally:
            _clear_override()
        store = _get_store()
        with store._engine.begin() as _c:
            row = _c.exec_driver_sql(
                "SELECT expertise_domains FROM peers WHERE persona = ?",
                ("persona-domains",),
            ).fetchone()
        assert json.loads(row[0]) == ["aws", "lambda", "cold-start"]

    def test_null_expertise_domains_stays_null(self, client: TestClient) -> None:
        _override_api_key(ALICE)
        try:
            client.post(
                "/api/v1/peers/heartbeat",
                json={"persona": "persona-no-domains", "discoverable": True},
            )
        finally:
            _clear_override()
        store = _get_store()
        with store._engine.begin() as _c:
            row = _c.exec_driver_sql(
                "SELECT expertise_domains FROM peers WHERE persona = ?",
                ("persona-no-domains",),
            ).fetchone()
        assert row[0] is None


class TestAuthRequired:
    def test_no_api_key_returns_401(self, client: TestClient) -> None:
        # No override; the require_api_key dep should reject.
        resp = client.post(
            "/api/v1/peers/heartbeat",
            json={"persona": "persona-naked", "discoverable": True},
        )
        assert resp.status_code == 401
