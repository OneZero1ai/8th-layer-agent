"""Pending-review tier (#103) — store + route tests.

State machine pinned by these tests:

* ``submit_pending_review(unit, reason, expires_at, ...)`` lands the KU
  with ``status='pending_review'`` plus the reason and TTL columns.
* ``GET /review/pending-review`` lists rows in that state, sorted by
  expiry ascending (closest-to-expiring first).
* ``POST /review/{id}/approve`` transitions ``pending_review →
  approved`` (same shape as the standard pending → approved flow).
* ``POST /review/{id}/reject`` transitions ``pending_review →
  dropped`` — distinct from the regular ``rejected`` state. The
  distinct value lets dashboards render the two cohorts separately
  and lets future tooling sweep dropped rows on a stricter retention.
* ``expire_pending_reviews`` sweeps rows whose
  ``pending_review_expires_at < now``, transitioning them to
  ``status='dropped'`` and stamping
  ``reviewed_by='ttl_expired_sweeper'``.

Out of scope here: the ``/reflect/submit?queue_hard_findings=true``
contract that produces these rows from the reflect classifier — that
ships in the harness-agnostic-reflect work (#102 / #67). This file
exercises the L2-side substrate that the reflect work will plug into.
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
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "pending.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    """Direct store fixture — no FastAPI lifespan, no API surface."""
    db = tmp_path / "pending.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _make_unit(*, summary: str = "Hard finding") -> object:
    return create_knowledge_unit(
        domains=["test-fleet"],
        insight=Insight(
            summary=summary,
            detail="VIBE√ classifier flagged this candidate for sanitization review.",
            action="Operator approves to promote, rejects to drop.",
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
# Store-level state machine
# ---------------------------------------------------------------------------


class TestSubmitPendingReview:
    def test_submission_lands_with_status_and_reason_columns(
        self, store: SqliteStore, tmp_path: Path
    ) -> None:
        unit = _make_unit()
        expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        store.sync.submit_pending_review(
            unit,
            reason="credential-shaped substring in summary",
            expires_at=expires,
            enterprise_id="acme",
            group_id="engineering",
        )

        # On-disk inspection — the migration column-set must include
        # both pending_review_* columns and the row's status must be
        # 'pending_review' (not the default 'pending').
        conn = sqlite3.connect(str(tmp_path / "pending.db"))
        try:
            row = conn.execute(
                "SELECT status, pending_review_reason, "
                "pending_review_expires_at, enterprise_id, group_id "
                "FROM knowledge_units WHERE id = ?",
                (unit.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "pending_review"
        assert row[1] == "credential-shaped substring in summary"
        assert row[2] == expires
        # #89 fix carries through — submission honours auth claims.
        assert row[3] == "acme"
        assert row[4] == "engineering"


class TestPendingReviewListAndCount:
    def test_listing_orders_by_expires_ascending(
        self, store: SqliteStore
    ) -> None:
        # Three pending-review rows with different expiries.
        soon = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        mid = (datetime.now(UTC) + timedelta(days=15)).isoformat()
        far = (datetime.now(UTC) + timedelta(days=29)).isoformat()
        for expires, label in ((mid, "mid"), (far, "far"), (soon, "soon")):
            unit = _make_unit(summary=label)
            store.sync.submit_pending_review(
                unit,
                reason=f"{label}-test",
                expires_at=expires,
                enterprise_id="acme",
                group_id="engineering",
            )

        items = store.sync.list_pending_review(enterprise_id="acme", limit=10, offset=0)
        # Ordered by pending_review_expires_at ASC — soon, mid, far.
        labels = [i["knowledge_unit"].insight.summary for i in items]
        assert labels == ["soon", "mid", "far"]

        assert store.sync.count_pending_review(enterprise_id="acme") == 3

    def test_listing_scopes_by_enterprise(
        self, store: SqliteStore
    ) -> None:
        """Cross-tenant: an acme pending-review row must NOT appear in
        moscowmul3's queue. Pin against accidental cross-tenant leak."""
        store.sync.submit_pending_review(
            _make_unit(summary="acme-only"),
            reason="x",
            expires_at=(datetime.now(UTC) + timedelta(days=10)).isoformat(),
            enterprise_id="acme",
            group_id="engineering",
        )
        store.sync.submit_pending_review(
            _make_unit(summary="moscow-only"),
            reason="y",
            expires_at=(datetime.now(UTC) + timedelta(days=10)).isoformat(),
            enterprise_id="moscowmul3",
            group_id="engineering",
        )
        acme_items = store.sync.list_pending_review(
            enterprise_id="acme", limit=10, offset=0
        )
        moscow_items = store.sync.list_pending_review(
            enterprise_id="moscowmul3", limit=10, offset=0
        )
        assert [i["knowledge_unit"].insight.summary for i in acme_items] == ["acme-only"]
        assert [i["knowledge_unit"].insight.summary for i in moscow_items] == ["moscow-only"]


class TestExpirePendingReviews:
    def test_sweep_drops_only_expired_rows(self, store: SqliteStore) -> None:
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

        keep = _make_unit(summary="not yet expired")
        drop = _make_unit(summary="should be dropped")
        store.sync.submit_pending_review(
            keep,
            reason="x",
            expires_at=future,
            enterprise_id="acme",
            group_id="engineering",
        )
        store.sync.submit_pending_review(
            drop,
            reason="y",
            expires_at=past,
            enterprise_id="acme",
            group_id="engineering",
        )

        now_iso = datetime.now(UTC).isoformat()
        dropped = store.sync.expire_pending_reviews(
            enterprise_id="acme", now_iso=now_iso
        )
        assert dropped == [drop.id]

        # The non-expired row stays put; the expired row is now status=dropped.
        items = store.sync.list_pending_review(enterprise_id="acme", limit=10, offset=0)
        assert [i["knowledge_unit"].id for i in items] == [keep.id]

        # And the dropped row's status moved on, with the sweeper sentinel
        # in reviewed_by so the audit trail is honest.
        review = store.sync.get_review_status(drop.id)
        assert review is not None
        assert review["status"] == "dropped"
        assert review["reviewed_by"] == "ttl_expired_sweeper"


# ---------------------------------------------------------------------------
# Route layer
# ---------------------------------------------------------------------------


class TestPendingReviewRoute:
    def test_pending_review_endpoint_lists_rows(self, client: TestClient) -> None:
        _seed_user(username="reviewer", password="pw", role="admin")
        # Submit one pending-review KU through the store (no /reflect
        # contract surface yet; the route layer is what we're testing).
        s = _get_store()
        unit = _make_unit(summary="route-test")
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        s.sync.submit_pending_review(
            unit,
            reason="credential-shaped substring",
            expires_at=future,
            enterprise_id="acme",
            group_id="engineering",
        )
        token = _login_jwt(client, "reviewer", "pw")

        resp = client.get(
            "/review/pending-review",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["status"] == "pending_review"
        assert item["pending_review_reason"] == "credential-shaped substring"
        assert item["pending_review_expires_at"] == future

    def test_pending_review_requires_admin(self, client: TestClient) -> None:
        _seed_user(username="grunt", password="pw", role="user")
        token = _login_jwt(client, "grunt", "pw")
        resp = client.get(
            "/review/pending-review",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403  # require_admin gate

    def test_approve_from_pending_review_transitions_to_approved(
        self, client: TestClient
    ) -> None:
        _seed_user(username="admin_approve", password="pw", role="admin")
        s = _get_store()
        unit = _make_unit()
        s.sync.submit_pending_review(
            unit,
            reason="x",
            expires_at=(datetime.now(UTC) + timedelta(days=10)).isoformat(),
            enterprise_id="acme",
            group_id="engineering",
        )
        token = _login_jwt(client, "admin_approve", "pw")

        resp = client.post(
            f"/review/{unit.id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"

    def test_reject_from_pending_review_transitions_to_dropped_not_rejected(
        self, client: TestClient
    ) -> None:
        """The lifecycle distinction (#103): operator rejection of a
        pending_review row sets status='dropped', not 'rejected'.
        Pins the spec line "pending_review → dropped (operator rejects)".
        """
        _seed_user(username="admin_reject", password="pw", role="admin")
        s = _get_store()
        unit = _make_unit()
        s.sync.submit_pending_review(
            unit,
            reason="x",
            expires_at=(datetime.now(UTC) + timedelta(days=10)).isoformat(),
            enterprise_id="acme",
            group_id="engineering",
        )
        token = _login_jwt(client, "admin_reject", "pw")

        resp = client.post(
            f"/review/{unit.id}/reject",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        # Critical assertion: dropped, not rejected.
        assert resp.json()["status"] == "dropped"

    def test_reject_from_regular_pending_still_uses_rejected(
        self, client: TestClient
    ) -> None:
        """The dropped distinction is *only* for pending_review. A
        regular ``status='pending'`` rejection still goes to
        ``status='rejected'`` — unchanged from pre-#103 behaviour."""
        _seed_user(username="admin_reg", password="pw", role="admin")
        # Propose via the API path so the row lands at status='pending'
        # (default), not pending_review.
        _seed_user(username="proposer_reg", password="pw", role="user")
        prop_jwt = _login_jwt(client, "proposer_reg", "pw")
        prop_key_resp = client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {prop_jwt}"},
            json={"name": "x", "ttl": "30d"},
        )
        prop_key = prop_key_resp.json()["token"]
        propose = client.post(
            "/propose",
            json={
                "domains": ["test-fleet"],
                "insight": {
                    "summary": "Regular pending KU for the regression boundary",
                    "detail": "Goes to status='rejected', not dropped, when an admin rejects.",
                    "action": "Pins the boundary between #103 dropped and pre-existing rejected.",
                },
            },
            headers={"Authorization": f"Bearer {prop_key}"},
        )
        assert propose.status_code == 201, propose.text
        ku_id = propose.json()["id"]

        admin_jwt = _login_jwt(client, "admin_reg", "pw")
        resp = client.post(
            f"/review/{ku_id}/reject",
            headers={"Authorization": f"Bearer {admin_jwt}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"  # NOT dropped


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


class TestPendingReviewMigration:
    def test_columns_present_after_upgrade(self, tmp_path: Path) -> None:
        db = tmp_path / "migration.db"
        run_migrations(f"sqlite:///{db}")

        conn = sqlite3.connect(str(db))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_units)")}
        finally:
            conn.close()
        assert "pending_review_reason" in cols
        assert "pending_review_expires_at" in cols

    def test_downgrade_drops_columns(self, tmp_path: Path) -> None:
        """Round-trip: upgrade brings the columns; downgrade removes
        them. Validates the migration is symmetric (used by tests, not
        prod) — same shape as ``test_migration_0011_activity_log``'s
        downgrade pin."""
        import os
        import subprocess

        db = tmp_path / "downgrade.db"
        sqlite3.connect(str(db)).close()  # touch
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path.home()),
            "CQ_DB_PATH": str(db),
        }

        up = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "0012_pending_review_tier"],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert up.returncode == 0, f"upgrade failed: {up.stderr}\n{up.stdout}"

        # Both columns now present.
        conn = sqlite3.connect(str(db))
        try:
            cols_after_up = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_units)")}
        finally:
            conn.close()
        assert {"pending_review_reason", "pending_review_expires_at"} <= cols_after_up

        # Downgrade ONE step (not all the way to base — that would
        # blow away every previous migration too).
        down = subprocess.run(
            ["uv", "run", "alembic", "downgrade", "0011_activity_log"],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert down.returncode == 0, f"downgrade failed: {down.stderr}\n{down.stdout}"

        conn = sqlite3.connect(str(db))
        try:
            cols_after_down = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_units)")}
        finally:
            conn.close()
        assert "pending_review_reason" not in cols_after_down
        assert "pending_review_expires_at" not in cols_after_down
