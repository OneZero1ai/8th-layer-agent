"""Read-time TTL enforcement on pending_review queries.

Read-time hazard (#121 review finding 2): the TTL sweeper transitions
``pending_review → dropped`` once ``pending_review_expires_at < now``,
but the sweeper's startup-loop wiring is deferred. Without a read-time
filter, ``GET /review/pending-review`` (and the underlying
``list_pending_review`` / ``count_pending_review`` helpers) could
surface candidates that have already passed their TTL — admins would
see and act on rows the spec says should already be dropped.

Fix shape: the read queries AND
``(pending_review_expires_at IS NULL OR pending_review_expires_at > now)``
into the WHERE clause. The route also schedules a background sweep so
the on-disk transition catches up — but the response is correct even
when that sweep is delayed or dropped.

These tests pin three things:

1. ``list_pending_review`` and ``count_pending_review`` filter expired
   rows out at read time, regardless of sweeper state.
2. ``GET /review/pending-review`` mirrors the store-level filter.
3. NULL TTL (defensive: a row with no expires_at) continues to appear
   — same shape as the sweeper's ``IS NOT NULL`` filter.
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
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "ttl.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db = tmp_path / "ttl.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _make_unit(*, summary: str = "ttl candidate") -> object:
    return create_knowledge_unit(
        domains=["test-fleet"],
        insight=Insight(
            summary=summary,
            detail="Probe read-time TTL enforcement on pending_review.",
            action="Expired rows must not surface even when sweeper is idle.",
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
# Store-level: read filters expired rows.
# ---------------------------------------------------------------------------


class TestListFiltersExpiredRows:
    def test_expired_row_invisible_in_list_even_before_sweep(self, store: SqliteStore) -> None:
        """Submit a row whose TTL has already passed at insert time
        (sweeper has not run). The list query must not include it.
        Pre-fix the row appeared in the result; the read filter closes
        that gap.
        """
        past = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        expired = _make_unit(summary="expired")
        fresh = _make_unit(summary="fresh")
        store.sync.submit_pending_review(
            expired,
            reason="should not surface",
            expires_at=past,
            enterprise_id="acme",
            group_id="engineering",
        )
        store.sync.submit_pending_review(
            fresh,
            reason="should surface",
            expires_at=future,
            enterprise_id="acme",
            group_id="engineering",
        )

        # The sweeper has NOT run yet; the on-disk status of the
        # expired row is still 'pending_review'. The read filter is
        # what keeps it out of the response.
        items = store.sync.list_pending_review(enterprise_id="acme", limit=10, offset=0)
        summaries = [i["knowledge_unit"].insight.summary for i in items]
        assert summaries == ["fresh"], (
            f"#121 finding 2: expired pending_review row leaked into list. Got {summaries!r}; expected only ['fresh']."
        )

    def test_count_matches_list_under_ttl_filter(self, store: SqliteStore) -> None:
        """count_pending_review must agree with list_pending_review on
        which rows are visible — otherwise dashboard pagination breaks
        (total: 5 with only 3 items rendered).
        """
        past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
        for label, expires in (
            ("expired1", past),
            ("expired2", past),
            ("fresh1", future),
        ):
            store.sync.submit_pending_review(
                _make_unit(summary=label),
                reason="x",
                expires_at=expires,
                enterprise_id="acme",
                group_id="engineering",
            )

        listed = store.sync.list_pending_review(enterprise_id="acme", limit=10, offset=0)
        counted = store.sync.count_pending_review(enterprise_id="acme")
        assert len(listed) == counted == 1

    def test_null_expires_at_continues_to_appear(self, store: SqliteStore, tmp_path: Path) -> None:
        """Defensive: a row with NULL ``pending_review_expires_at``
        means "no TTL configured". The read filter uses
        ``IS NULL OR > now`` so those rows still appear — same shape
        as the sweeper's ``IS NOT NULL`` guard.

        We can't construct this through the public API
        (``submit_pending_review`` requires expires_at) so we patch
        the on-disk row directly.
        """
        unit = _make_unit(summary="null-ttl")
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        store.sync.submit_pending_review(
            unit,
            reason="x",
            expires_at=future,
            enterprise_id="acme",
            group_id="engineering",
        )
        # Strip the TTL on disk to simulate the legacy NULL case.
        conn = sqlite3.connect(str(tmp_path / "ttl.db"))
        try:
            conn.execute(
                "UPDATE knowledge_units SET pending_review_expires_at = NULL WHERE id = ?",
                (unit.id,),
            )
            conn.commit()
        finally:
            conn.close()

        items = store.sync.list_pending_review(enterprise_id="acme", limit=10, offset=0)
        assert [i["knowledge_unit"].id for i in items] == [unit.id]
        assert store.sync.count_pending_review(enterprise_id="acme") == 1


# ---------------------------------------------------------------------------
# Route-level: GET /review/pending-review honours the read-time filter.
# ---------------------------------------------------------------------------


class TestEndpointEnforcesTTL:
    def test_endpoint_omits_expired_rows(self, client: TestClient) -> None:
        """The HTTP surface must reflect the same filter — admins must
        not see TTL-expired candidates regardless of sweeper state."""
        _seed_user(username="admin_ttl", password="pw", role="admin")
        s = _get_store()

        past = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        s.sync.submit_pending_review(
            _make_unit(summary="route-expired"),
            reason="should not surface",
            expires_at=past,
            enterprise_id="acme",
            group_id="engineering",
        )
        s.sync.submit_pending_review(
            _make_unit(summary="route-fresh"),
            reason="should surface",
            expires_at=future,
            enterprise_id="acme",
            group_id="engineering",
        )

        token = _login_jwt(client, "admin_ttl", "pw")
        resp = client.get(
            "/review/pending-review",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        summaries = [i["knowledge_unit"]["insight"]["summary"] for i in body["items"]]
        assert summaries == ["route-fresh"]

    def test_endpoint_triggers_background_sweep(self, client: TestClient) -> None:
        """The route schedules a background sweep so on-disk state
        eventually catches up. Verify that after a request the
        previously-expired row's status moved to 'dropped' on disk —
        the sweep ran via the BackgroundTask hook.

        TestClient runs background tasks synchronously, so by the time
        the response returns, the sweep has already executed.
        """
        _seed_user(username="admin_sweep", password="pw", role="admin")
        s = _get_store()
        past = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        unit = _make_unit(summary="will-be-swept")
        s.sync.submit_pending_review(
            unit,
            reason="x",
            expires_at=past,
            enterprise_id="acme",
            group_id="engineering",
        )
        token = _login_jwt(client, "admin_sweep", "pw")
        resp = client.get(
            "/review/pending-review",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # Inspect on-disk: the sweeper transitioned the expired row.
        review = s.sync.get_review_status(unit.id)
        assert review is not None
        assert review["status"] == "dropped"
        assert review["reviewed_by"] == "ttl_expired_sweeper"
