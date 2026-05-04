"""Tests for the review endpoints."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.deps import require_api_key


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides[require_api_key] = lambda: "test-user"
    with TestClient(app) as c:
        from cq_server.app import _get_store
        from cq_server.auth import hash_password

        store = _get_store()
        if store.sync.get_user("test-user") is None:
            store.sync.create_user("test-user", hash_password("test-pw"))
        yield c
    app.dependency_overrides.pop(require_api_key, None)


def _login(
    client: TestClient,
    username: str = "reviewer",
    password: str = "pass123",
    *,
    role: str = "admin",
    enterprise_id: str | None = None,
) -> str:
    """Seed a user (admin by default for /review tests), log in, return JWT.

    /review/* requires admin role (SEC-CRIT #32). Tests that want to
    exercise the 403-on-non-admin path pass role="user".
    """
    import contextlib

    from cq_server.app import _get_store
    from cq_server.auth import hash_password

    store = _get_store()
    with contextlib.suppress(Exception):
        store.sync.create_user(username, hash_password(password))
    if role != "user":
        store.sync.set_user_role(username, role)
    if enterprise_id is not None:
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE users SET enterprise_id = ? WHERE username = ?",
                (enterprise_id, username),
            )
    resp = client.post("/auth/login", json={"username": username, "password": password})
    return resp.json()["token"]


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _propose(client: TestClient, **overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "domains": ["api", "testing"],
        "insight": {
            "summary": "An API testing insight worth sharing",
            "detail": "When testing API endpoints, validate response shape against the schema, not just status codes.",
            "action": "Use schema-aware assertions in API tests.",
        },
    }
    resp = client.post("/propose", json={**defaults, **overrides})
    assert resp.status_code == 201
    return resp.json()


class TestReviewQueue:
    def test_queue_returns_pending(self, client: TestClient) -> None:
        token = _login(client)
        _propose(client)
        resp = client.get("/review/queue", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["status"] == "pending"

    def test_queue_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/review/queue")
        assert resp.status_code == 401

    def test_queue_empty(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.get("/review/queue", headers=_auth_header(token))
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestApprove:
    def test_approve_pending_unit(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["reviewed_by"] == "reviewer"

    def test_approve_already_reviewed_returns_409(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        assert resp.status_code == 409

    def test_approve_nonexistent_returns_404(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.post("/review/ku_nonexistent/approve", headers=_auth_header(token))
        assert resp.status_code == 404

    def test_approved_unit_appears_in_query(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client, domains=["searchable"])
        client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        resp = client.get("/query", params={"domains": ["searchable"]})
        assert len(resp.json()) == 1


class TestReject:
    def test_reject_pending_unit(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        resp = client.post(f"/review/{unit['id']}/reject", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"

    def test_rejected_unit_not_in_query(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client, domains=["hidden"])
        client.post(f"/review/{unit['id']}/reject", headers=_auth_header(token))
        resp = client.get("/query", params={"domains": ["hidden"]})
        assert len(resp.json()) == 0


class TestListUnits:
    def test_filter_by_domain(self, client: TestClient) -> None:
        token = _login(client)
        _propose(client, domains=["python"])
        _propose(client, domains=["rust"])
        resp = client.get("/review/units", params={"domain": "python"}, headers=_auth_header(token))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert "python" in items[0]["knowledge_unit"]["domains"]

    def test_filter_by_confidence_range(self, client: TestClient) -> None:
        """Default confidence from propose is 0.5; filter to include/exclude it."""
        token = _login(client)
        _propose(client)
        _propose(client)
        # Both KUs have default confidence 0.5 — range [0.3, 0.6) includes them.
        resp = client.get(
            "/review/units",
            params={"confidence_min": 0.3, "confidence_max": 0.6},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        # Range [0.8, 1.01) excludes them.
        resp = client.get(
            "/review/units",
            params={"confidence_min": 0.8, "confidence_max": 1.01},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_includes_all_statuses(self, client: TestClient) -> None:
        token = _login(client)
        u1 = _propose(client, domains=["mixed"])
        u2 = _propose(client, domains=["mixed"])
        _propose(client, domains=["mixed"])
        client.post(f"/review/{u1['id']}/approve", headers=_auth_header(token))
        client.post(f"/review/{u2['id']}/reject", headers=_auth_header(token))
        resp = client.get("/review/units", params={"domain": "mixed"}, headers=_auth_header(token))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 3
        statuses = {item["status"] for item in items}
        assert statuses == {"approved", "rejected", "pending"}

    def test_filter_by_status(self, client: TestClient) -> None:
        token = _login(client)
        u1 = _propose(client, domains=["status-test"])
        _propose(client, domains=["status-test"])
        client.post(f"/review/{u1['id']}/approve", headers=_auth_header(token))
        resp = client.get(
            "/review/units",
            params={"domain": "status-test", "status": "approved"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["status"] == "approved"

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/review/units")
        assert resp.status_code == 401

    def test_no_filters_returns_all(self, client: TestClient) -> None:
        token = _login(client)
        _propose(client)
        _propose(client)
        resp = client.get("/review/units", headers=_auth_header(token))
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestGetUnit:
    def test_get_pending_unit(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        resp = client.get(f"/review/{unit['id']}", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["knowledge_unit"]["id"] == unit["id"]
        assert body["knowledge_unit"]["insight"]["summary"] == "An API testing insight worth sharing"
        assert body["status"] == "pending"
        assert body["reviewed_by"] is None

    def test_get_approved_unit(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        resp = client.get(f"/review/{unit['id']}", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["reviewed_by"] == "reviewer"
        assert body["reviewed_at"] is not None

    def test_get_rejected_unit(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        client.post(f"/review/{unit['id']}/reject", headers=_auth_header(token))
        resp = client.get(f"/review/{unit['id']}", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"

    def test_get_nonexistent_returns_404(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.get("/review/ku_nonexistent", headers=_auth_header(token))
        assert resp.status_code == 404

    def test_get_requires_auth(self, client: TestClient) -> None:
        unit = _propose(client)
        resp = client.get(f"/review/{unit['id']}")
        assert resp.status_code == 401


class TestReviewStats:
    def test_stats_counts(self, client: TestClient) -> None:
        token = _login(client)
        u1 = _propose(client)
        u2 = _propose(client)
        _propose(client)
        client.post(f"/review/{u1['id']}/approve", headers=_auth_header(token))
        client.post(f"/review/{u2['id']}/reject", headers=_auth_header(token))
        resp = client.get("/review/stats", headers=_auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["approved"] == 1
        assert body["counts"]["rejected"] == 1
        assert body["counts"]["pending"] == 1

    def test_domains_count_approved_only(self, client: TestClient) -> None:
        token = _login(client)
        u1 = _propose(client, domains=["only-approved"])
        u2 = _propose(client, domains=["only-approved"])
        client.post(f"/review/{u1['id']}/approve", headers=_auth_header(token))
        client.post(f"/review/{u2['id']}/reject", headers=_auth_header(token))
        resp = client.get("/review/stats", headers=_auth_header(token))
        assert resp.status_code == 200
        domains = resp.json()["domains"]
        assert domains.get("only-approved") == 1


class TestReviewStatsDetail:
    def test_stats_includes_confidence_distribution(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        resp = client.get("/review/stats", headers=_auth_header(token))
        body = resp.json()
        assert "confidence_distribution" in body
        total = sum(body["confidence_distribution"].values())
        assert total == 1

    def test_stats_includes_recent_activity(self, client: TestClient) -> None:
        token = _login(client)
        unit = _propose(client)
        client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        resp = client.get("/review/stats", headers=_auth_header(token))
        body = resp.json()
        assert len(body["recent_activity"]) >= 1

    def test_activity_shows_terminal_state_only(self, client: TestClient) -> None:
        """A reviewed KU should appear once (as approved/rejected), not twice."""
        token = _login(client)
        unit = _propose(client)
        approve_resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token))
        assert approve_resp.status_code == 200
        resp = client.get("/review/stats", headers=_auth_header(token))
        assert resp.status_code == 200
        events = resp.json()["recent_activity"]
        unit_events = [e for e in events if e["unit_id"] == unit["id"]]
        assert len(unit_events) == 1
        assert unit_events[0]["type"] == "approved"

    def test_activity_shows_proposed_for_pending(self, client: TestClient) -> None:
        """A pending KU should appear as proposed."""
        token = _login(client)
        unit = _propose(client)
        resp = client.get("/review/stats", headers=_auth_header(token))
        assert resp.status_code == 200
        events = resp.json()["recent_activity"]
        unit_events = [e for e in events if e["unit_id"] == unit["id"]]
        assert len(unit_events) == 1
        assert unit_events[0]["type"] == "proposed"


class TestReviewAdminGate:
    """SEC-CRIT #32 — /review/* requires admin role."""

    def test_non_admin_queue_403(self, client: TestClient) -> None:
        token = _login(client, "regular-user", role="user")
        resp = client.get("/review/queue", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_non_admin_approve_403(self, client: TestClient) -> None:
        admin_token = _login(client)
        unit = _propose(client)
        user_token = _login(client, "regular-user", role="user")
        resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(user_token))
        assert resp.status_code == 403
        # Admin can still approve.
        resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(admin_token))
        assert resp.status_code == 200

    def test_non_admin_delete_403(self, client: TestClient) -> None:
        _login(client)  # seed admin so KU exists in default tenant
        unit = _propose(client)
        user_token = _login(client, "regular-user", role="user")
        resp = client.delete(f"/review/{unit['id']}", headers=_auth_header(user_token))
        assert resp.status_code == 403

    def test_non_admin_stats_403(self, client: TestClient) -> None:
        token = _login(client, "regular-user", role="user")
        resp = client.get("/review/stats", headers=_auth_header(token))
        assert resp.status_code == 403


class TestReviewTenantScope:
    """SEC-CRIT #32 — /review/* is scoped to the admin's Enterprise."""

    def _set_ku_tenancy(self, unit_id: str, *, enterprise_id: str) -> None:
        from cq_server.app import _get_store

        store = _get_store()
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE knowledge_units SET enterprise_id = ? WHERE id = ?",
                (enterprise_id, unit_id),
            )

    def test_admin_a_cannot_see_admin_b_ku(self, client: TestClient) -> None:
        token_a = _login(client, "admin-a", enterprise_id="acme")
        token_b = _login(client, "admin-b", enterprise_id="globex")
        # Both proposes land in default-enterprise; reassign the second one to globex.
        _propose(client, domains=["acme-only"])
        unit_b = _propose(client, domains=["globex-only"])
        self._set_ku_tenancy(unit_b["id"], enterprise_id="globex")

        # admin-a (acme) sees only acme's pending queue.
        a_queue = client.get("/review/queue", headers=_auth_header(token_a)).json()
        a_ids = {item["knowledge_unit"]["id"] for item in a_queue["items"]}
        assert unit_b["id"] not in a_ids

        # admin-b (globex) sees only globex's row.
        b_queue = client.get("/review/queue", headers=_auth_header(token_b)).json()
        b_ids = {item["knowledge_unit"]["id"] for item in b_queue["items"]}
        assert b_ids == {unit_b["id"]}

    def test_cross_tenant_get_returns_404(self, client: TestClient) -> None:
        _login(client)  # seed default-tenant admin for the propose call
        unit = _propose(client)
        self._set_ku_tenancy(unit["id"], enterprise_id="globex")
        token_a = _login(client, "admin-a", enterprise_id="acme")
        resp = client.get(f"/review/{unit['id']}", headers=_auth_header(token_a))
        assert resp.status_code == 404

    def test_cross_tenant_approve_returns_404(self, client: TestClient) -> None:
        _login(client)  # default-tenant admin to allow propose
        unit = _propose(client)
        self._set_ku_tenancy(unit["id"], enterprise_id="globex")
        token_a = _login(client, "admin-a", enterprise_id="acme")
        resp = client.post(f"/review/{unit['id']}/approve", headers=_auth_header(token_a))
        assert resp.status_code == 404

    def test_cross_tenant_delete_returns_404(self, client: TestClient) -> None:
        _login(client)
        unit = _propose(client)
        self._set_ku_tenancy(unit["id"], enterprise_id="globex")
        token_a = _login(client, "admin-a", enterprise_id="acme")
        resp = client.delete(f"/review/{unit['id']}", headers=_auth_header(token_a))
        assert resp.status_code == 404

    def test_stats_scoped_to_admin_enterprise(self, client: TestClient) -> None:
        _login(client)  # default-tenant admin
        _propose(client, domains=["scope-test"])
        unit_globex = _propose(client, domains=["scope-test"])
        self._set_ku_tenancy(unit_globex["id"], enterprise_id="globex")

        token_a = _login(client, "admin-a", enterprise_id="acme")
        # acme has zero KUs.
        resp = client.get("/review/stats", headers=_auth_header(token_a))
        assert resp.status_code == 200
        assert sum(resp.json()["counts"].values()) == 0

        token_b = _login(client, "admin-b", enterprise_id="globex")
        resp = client.get("/review/stats", headers=_auth_header(token_b))
        assert resp.status_code == 200
        assert resp.json()["counts"]["pending"] == 1
