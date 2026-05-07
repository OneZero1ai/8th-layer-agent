"""Optimistic-concurrency control on review-status transitions.

Race surface (#121 review finding 1): two admins resolve the same KU
at the same time — one approves, one rejects. Pre-fix, both UPDATEs
landed and the last writer won silently, leaving an audit-log row
that disagreed with the on-disk ``reviewed_by`` value.

Fix shape: ``UPDATE_REVIEW_STATUS`` carries
``WHERE id = :id AND status NOT IN ('approved','rejected','dropped')``.
The first transition flips the row out of the pending family; the
second SELECT-then-UPDATE finds zero matching rows, ``set_review_status``
returns ``False``, and the FastAPI route returns 409.

These tests pin three things:

1. The store-level ``set_review_status`` returns ``True`` on the
   winning transition and ``False`` on the racing one.
2. The route-level ``approve`` / ``reject`` handlers turn the racing
   ``False`` into a 409 with the winning admin's terminal status.
3. Missing-row (KeyError) is preserved as distinct from race-loss
   (False return). The route 404s on missing, 409s on race-loss.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cq.models import Insight, create_knowledge_unit
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.auth import hash_password
from cq_server.migrations import run_migrations
from cq_server.store import SqliteStore


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "race.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db = tmp_path / "race.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _make_unit(*, summary: str = "race candidate") -> object:
    return create_knowledge_unit(
        domains=["test-fleet"],
        insight=Insight(
            summary=summary,
            detail="Probe the optimistic concurrency guard on review status.",
            action="Two admins act on the same row; only one wins.",
        ),
    )


def _seed_user(*, username: str, password: str, role: str = "user") -> None:
    s = _get_store()
    s.sync.create_user(username, hash_password(password))
    if role != "user":
        s.sync.set_user_role(username, role)
    with s._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            ("acme", "engineering", username),
        )


def _login_jwt(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ---------------------------------------------------------------------------
# Store-level: set_review_status returns True on win, False on race-loss.
# ---------------------------------------------------------------------------


class TestSetReviewStatusReturnValue:
    def test_first_transition_wins_returns_true(self, store: SqliteStore) -> None:
        unit = _make_unit()
        store.sync.insert(unit)
        assert store.sync.set_review_status(unit.id, "approved", "first") is True
        review = store.sync.get_review_status(unit.id)
        assert review is not None
        assert review["status"] == "approved"
        assert review["reviewed_by"] == "first"

    def test_second_transition_loses_returns_false(self, store: SqliteStore) -> None:
        """Approve, then attempt reject. The reject UPDATE finds zero rows
        because the WHERE clause excludes terminal states; the helper
        returns False without overwriting the prior decision.
        """
        unit = _make_unit()
        store.sync.insert(unit)
        assert store.sync.set_review_status(unit.id, "approved", "first") is True
        # Second writer races in — must not silently overwrite.
        assert store.sync.set_review_status(unit.id, "rejected", "second") is False

        review = store.sync.get_review_status(unit.id)
        assert review is not None
        # Still the first writer's decision — race-loss did not corrupt the
        # audit trail. This is the bug the WHERE clause guards against.
        assert review["status"] == "approved"
        assert review["reviewed_by"] == "first"

    def test_dropped_status_also_blocks_subsequent_writes(
        self, store: SqliteStore
    ) -> None:
        """``dropped`` (the pending_review→reject terminal) must also
        block further transitions. Pins that the WHERE clause's
        terminal-state list covers all three values, not just
        approved/rejected.
        """
        unit = _make_unit()
        expires = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        store.sync.submit_pending_review(
            unit,
            reason="x",
            expires_at=expires,
            enterprise_id="acme",
            group_id="engineering",
        )
        assert store.sync.set_review_status(unit.id, "dropped", "first") is True
        assert store.sync.set_review_status(unit.id, "approved", "second") is False
        review = store.sync.get_review_status(unit.id)
        assert review is not None
        assert review["status"] == "dropped"
        assert review["reviewed_by"] == "first"

    def test_missing_unit_still_raises_key_error(self, store: SqliteStore) -> None:
        """Race-loss returns False; missing-row continues to raise
        KeyError. The route layer uses this distinction to 404 on
        missing and 409 on race-loss."""
        with pytest.raises(KeyError, match="Knowledge unit not found"):
            store.sync.set_review_status("does-not-exist", "approved", "x")


# ---------------------------------------------------------------------------
# Route-level: simulated race produces 409, not silent overwrite.
# ---------------------------------------------------------------------------


class TestRouteRaceProduces409:
    def test_concurrent_approve_then_reject_returns_409(
        self, client: TestClient
    ) -> None:
        """Two admins race on the same KU. The first /approve wins; the
        second /reject must 409 with the terminal status, not silently
        overwrite the audit row.
        """
        _seed_user(username="admin_a", password="pw", role="admin")
        _seed_user(username="admin_b", password="pw", role="admin")
        _seed_user(username="proposer", password="pw", role="user")

        # Propose via the API so the row lands at status='pending'.
        prop_jwt = _login_jwt(client, "proposer", "pw")
        prop_key_resp = client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {prop_jwt}"},
            json={"name": "race", "ttl": "30d"},
        )
        prop_key = prop_key_resp.json()["token"]
        propose = client.post(
            "/propose",
            json={
                "domains": ["test-fleet"],
                "insight": {
                    "summary": "Race candidate KU for concurrency probe",
                    "detail": "Two admins act on this row at the same time.",
                    "action": "Verify only the first decision lands.",
                },
            },
            headers={"Authorization": f"Bearer {prop_key}"},
        )
        assert propose.status_code == 201, propose.text
        ku_id = propose.json()["id"]

        a_jwt = _login_jwt(client, "admin_a", "pw")
        b_jwt = _login_jwt(client, "admin_b", "pw")

        # admin_a wins.
        a_resp = client.post(
            f"/review/{ku_id}/approve",
            headers={"Authorization": f"Bearer {a_jwt}"},
        )
        assert a_resp.status_code == 200, a_resp.text
        assert a_resp.json()["status"] == "approved"

        # admin_b loses — 409 with the winning admin's terminal status
        # surfaced in the detail string.
        b_resp = client.post(
            f"/review/{ku_id}/reject",
            headers={"Authorization": f"Bearer {b_jwt}"},
        )
        assert b_resp.status_code == 409
        assert "approved" in b_resp.json()["detail"]

    def test_concurrent_reject_after_approve_does_not_corrupt_audit(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Pin the on-disk shape: after the racing reject 409s, the
        ``reviewed_by`` row in the DB is still the winning admin's
        username — not the loser's. This is the audit-corruption
        regression the WHERE clause closes.
        """
        _seed_user(username="winner", password="pw", role="admin")
        _seed_user(username="loser", password="pw", role="admin")
        _seed_user(username="prop2", password="pw", role="user")
        prop_jwt = _login_jwt(client, "prop2", "pw")
        prop_key_resp = client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {prop_jwt}"},
            json={"name": "race2", "ttl": "30d"},
        )
        propose = client.post(
            "/propose",
            json={
                "domains": ["test-fleet"],
                "insight": {
                    "summary": "Pin the on-disk audit row after race-loss",
                    "detail": "After 409, the reviewed_by must match the winner.",
                    "action": "Inspect the SQLite row directly.",
                },
            },
            headers={
                "Authorization": f"Bearer {prop_key_resp.json()['token']}"
            },
        )
        assert propose.status_code == 201, propose.text
        ku_id = propose.json()["id"]

        winner_jwt = _login_jwt(client, "winner", "pw")
        loser_jwt = _login_jwt(client, "loser", "pw")
        client.post(
            f"/review/{ku_id}/approve",
            headers={"Authorization": f"Bearer {winner_jwt}"},
        )
        client.post(
            f"/review/{ku_id}/reject",
            headers={"Authorization": f"Bearer {loser_jwt}"},
        )

        # Inspect the on-disk row directly — bypass the store.
        conn = sqlite3.connect(str(tmp_path / "race.db"))
        try:
            row = conn.execute(
                "SELECT status, reviewed_by FROM knowledge_units WHERE id = ?",
                (ku_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("approved", "winner")

    def test_404_preserved_when_unit_missing(self, client: TestClient) -> None:
        """Race-loss is 409; missing-id is still 404. The two cases are
        distinct in the store layer (KeyError vs False return) and
        must remain distinct at the route layer.
        """
        _seed_user(username="admin404", password="pw", role="admin")
        token = _login_jwt(client, "admin404", "pw")
        resp = client.post(
            "/review/does-not-exist/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
