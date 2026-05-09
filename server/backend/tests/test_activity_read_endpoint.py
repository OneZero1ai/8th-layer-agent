"""``GET /api/v1/activity`` — #108 Stage 2 Workstream D.

Pins:

* Authentication is mandatory; both JWT and API key are accepted via
  ``get_current_user``.
* Tenancy: the route always pins ``tenant_enterprise`` to the caller's
  Enterprise. No cross-Enterprise leak path.
* Admin scope: admin callers can read any persona within their
  Enterprise; ``persona=`` filter is honoured as-sent.
* Self scope: non-admin callers always see only their own persona's
  events. A non-admin who sends ``persona=alice`` while authenticated
  as Bob sees Bob's events — not 403, silent override.
* Filters compose: ``event_type``, ``since``, ``until``, ``persona``
  AND together. Malformed values 422 (timestamps) or 400 (cursor).
* Cursor pagination yields strict (ts DESC, id DESC) ordering and
  ``next_cursor`` terminates correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.activity import generate_activity_id, now_iso_z
from cq_server.app import _get_store, app
from cq_server.auth import hash_password


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "activity-read.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


def _seed_user(
    *,
    username: str,
    password: str,
    enterprise_id: str = "acme",
    group_id: str = "engineering",
    role: str = "user",
) -> None:
    s = _get_store()
    s.sync.create_user(username, hash_password(password))
    if role != "user":
        s.sync.set_user_role(username, role)
    with s._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            (enterprise_id, group_id, username),
        )


def _login_jwt(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _seed_activity_row(
    *,
    persona: str,
    enterprise: str = "acme",
    group: str = "engineering",
    event_type: str = "query",
    ts: str | None = None,
    payload: dict | None = None,
    thread_or_chain_id: str | None = None,
) -> str:
    """Drive store.append_activity directly. Bypasses the request path."""
    s = _get_store()
    activity_id = generate_activity_id()
    await s.append_activity(
        activity_id=activity_id,
        ts=ts or now_iso_z(),
        tenant_enterprise=enterprise,
        tenant_group=group,
        persona=persona,
        human=None,
        event_type=event_type,
        payload=payload or {},
        result_summary=None,
        thread_or_chain_id=thread_or_chain_id,
    )
    return activity_id


# ---------------------------------------------------------------------------
# Auth + tenancy scoping
# ---------------------------------------------------------------------------


class TestAuthAndScoping:
    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/activity")
        assert resp.status_code == 401

    def test_admin_sees_every_persona_in_their_enterprise(self, client: TestClient) -> None:
        import asyncio

        _seed_user(username="alice3", password="pw")
        _seed_user(username="bob3", password="pw")
        _seed_user(username="admin3", password="pw", role="admin")

        async def _seed() -> None:
            await _seed_activity_row(persona="alice3", event_type="query")
            await _seed_activity_row(persona="bob3", event_type="query")

        asyncio.run(_seed())

        token = _login_jwt(client, "admin3", "pw")
        resp = client.get(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        personas = sorted(item["persona"] for item in body["items"])
        assert personas == ["alice3", "bob3"]

    def test_non_admin_sees_only_their_own_persona(self, client: TestClient) -> None:
        """Critical scoping pin: a non-admin caller asking for
        ``persona=alice4`` while authenticated as bob4 sees bob4's
        events — silent override, no 403, no leak."""
        import asyncio

        _seed_user(username="alice4", password="pw")
        _seed_user(username="bob4", password="pw")

        async def _seed() -> None:
            await _seed_activity_row(persona="alice4", event_type="query")
            await _seed_activity_row(persona="bob4", event_type="query")

        asyncio.run(_seed())

        token = _login_jwt(client, "bob4", "pw")
        # Bob asks for Alice's events; the route forces persona=bob4.
        resp = client.get(
            "/api/v1/activity?persona=alice4",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert all(item["persona"] == "bob4" for item in items), items

    def test_cross_enterprise_rows_not_visible(self, client: TestClient) -> None:
        """An admin in acme cannot see moscowmul3 rows even when they
        ask. Tenancy is mandatory and pinned by the route layer."""
        import asyncio

        _seed_user(username="acme_admin", password="pw", role="admin")

        async def _seed() -> None:
            await _seed_activity_row(persona="alice5", enterprise="acme")
            await _seed_activity_row(persona="rival", enterprise="moscowmul3")

        asyncio.run(_seed())

        token = _login_jwt(client, "acme_admin", "pw")
        resp = client.get(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        personas = [item["persona"] for item in resp.json()["items"]]
        assert "rival" not in personas
        assert "alice5" in personas


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_event_type_filter(self, client: TestClient) -> None:
        import asyncio

        _seed_user(username="filter_admin", password="pw", role="admin")

        async def _seed() -> None:
            await _seed_activity_row(persona="x", event_type="query")
            await _seed_activity_row(persona="x", event_type="propose")
            await _seed_activity_row(persona="x", event_type="confirm")

        asyncio.run(_seed())

        token = _login_jwt(client, "filter_admin", "pw")
        resp = client.get(
            "/api/v1/activity?event_type=propose",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["items"]
        assert all(r["event_type"] == "propose" for r in rows)

    def test_event_type_unknown_value_422s(self, client: TestClient) -> None:
        _seed_user(username="filter_x", password="pw")
        token = _login_jwt(client, "filter_x", "pw")
        resp = client.get(
            "/api/v1/activity?event_type=not_a_real_event",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert "event_type" in resp.json()["detail"].lower() or "expected" in resp.json()["detail"].lower()

    def test_since_until_window(self, client: TestClient) -> None:
        import asyncio

        _seed_user(username="window_admin", password="pw", role="admin")

        # Three rows at three different timestamps.
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        mid = (datetime.now(UTC) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        new = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

        async def _seed() -> None:
            await _seed_activity_row(persona="t", event_type="query", ts=old)
            await _seed_activity_row(persona="t", event_type="query", ts=mid)
            await _seed_activity_row(persona="t", event_type="query", ts=new)

        asyncio.run(_seed())

        token = _login_jwt(client, "window_admin", "pw")
        # Window covers only the mid row (since=before-mid, until=before-new).
        since = (datetime.now(UTC) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        until = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        resp = client.get(
            f"/api/v1/activity?since={since}&until={until}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["items"]
        assert len(rows) == 1
        assert rows[0]["ts"] == mid

    def test_malformed_timestamp_422s(self, client: TestClient) -> None:
        _seed_user(username="ts", password="pw")
        token = _login_jwt(client, "ts", "pw")
        resp = client.get(
            "/api/v1/activity?since=not-a-timestamp",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    def test_cursor_advances_through_rows(self, client: TestClient) -> None:
        import asyncio

        _seed_user(username="cur_admin", password="pw", role="admin")

        # Five rows, monotone-increasing ts.
        async def _seed() -> None:
            base = datetime.now(UTC)
            for i in range(5):
                ts = (base + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
                await _seed_activity_row(persona="curs", event_type="query", ts=ts)

        asyncio.run(_seed())

        token = _login_jwt(client, "cur_admin", "pw")
        resp = client.get(
            "/api/v1/activity?limit=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 2
        first_page_tss = [it["ts"] for it in body["items"]]
        assert body["next_cursor"] is not None

        resp2 = client.get(
            f"/api/v1/activity?limit=2&cursor={body['next_cursor']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        page2 = resp2.json()
        page2_tss = [it["ts"] for it in page2["items"]]
        # No overlap with page 1; descending order.
        assert not set(first_page_tss) & set(page2_tss)
        assert all(p2 < p1 for p1 in first_page_tss for p2 in page2_tss)

    def test_malformed_cursor_400s(self, client: TestClient) -> None:
        _seed_user(username="cm", password="pw")
        token = _login_jwt(client, "cm", "pw")
        resp = client.get(
            "/api/v1/activity?cursor=not-a-cursor",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# API-key auth — the same endpoint accepts cqa.v1.* tokens (issue #99 fix).
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    def test_endpoint_accepts_api_key(self, client: TestClient) -> None:
        import asyncio

        _seed_user(username="ak_user", password="pw")

        async def _seed() -> None:
            await _seed_activity_row(persona="ak_user", event_type="query")

        asyncio.run(_seed())

        jwt = _login_jwt(client, "ak_user", "pw")
        resp = client.post(
            "/auth/api-keys",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": "x", "ttl": "30d"},
        )
        assert resp.status_code == 201, resp.text
        api_key = resp.json()["token"]
        assert api_key.startswith("cqa.v1.")

        resp = client.get(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        # Non-admin scope: only their own persona.
        rows = resp.json()["items"]
        assert all(r["persona"] == "ak_user" for r in rows)
