"""Phase 6 step 3 / Lane C: GET /peers/active scoping + filter tests.

Pins:
  - Cross-Enterprise visibility is silently dropped (presence is
    Enterprise-bounded, even if a consent record is in place).
  - since_minutes filters out stale rows.
  - discoverable=False rows are hidden regardless of last_seen_at.
  - group filter narrows within the same Enterprise.
  - include_self=False + self_persona excludes the caller's own row.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.deps import require_api_key

ALICE = "alice"  # acme/engineering
BOB = "bob"      # acme/solutions
CARLA = "carla"  # initech/r-and-d (cross-Enterprise)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "peers_active.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ALICE, pw)
        store.sync.create_user(BOB, pw)
        store.sync.create_user(CARLA, pw)
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'engineering' WHERE username = ?",
                (ALICE,),
            )
            store._conn.execute(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'solutions' WHERE username = ?",
                (BOB,),
            )
            store._conn.execute(
                "UPDATE users SET enterprise_id = 'initech', group_id = 'r-and-d' WHERE username = ?",
                (CARLA,),
            )
        yield c


def _heartbeat(client: TestClient, *, as_user: str, persona: str, discoverable: bool = True) -> None:
    app.dependency_overrides[require_api_key] = lambda: as_user
    try:
        client.post(
            "/api/v1/peers/heartbeat",
            json={"persona": persona, "discoverable": discoverable},
        )
    finally:
        app.dependency_overrides.pop(require_api_key, None)


def _set_last_seen(persona: str, when: datetime) -> None:
    """Manually rewind a row's ``last_seen_at`` to test the since filter."""
    store = _get_store()
    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE peers SET last_seen_at = ? WHERE persona = ?",
            (when.isoformat(), persona),
        )


def _list_active(client: TestClient, *, as_user: str, **params: object) -> dict:
    app.dependency_overrides[require_api_key] = lambda: as_user
    try:
        resp = client.get("/api/v1/peers/active", params=params)
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestEnterpriseScoping:
    def test_cross_enterprise_returns_nothing(self, client: TestClient) -> None:
        # Alice (acme) registers; Carla (initech) lists. She must not see
        # Alice — even though they're both alive, presence is bounded.
        _heartbeat(client, as_user=ALICE, persona="persona-alice")
        body = _list_active(client, as_user=CARLA)
        assert body["count"] == 0
        assert body["active_peers"] == []

    def test_same_enterprise_different_group_visible(self, client: TestClient) -> None:
        # Alice in acme/engineering; Bob in acme/solutions. Both see each other.
        _heartbeat(client, as_user=ALICE, persona="persona-alice")
        _heartbeat(client, as_user=BOB, persona="persona-bob")
        body = _list_active(client, as_user=ALICE, include_self=True)
        personas = {p["persona"] for p in body["active_peers"]}
        assert {"persona-alice", "persona-bob"} == personas


class TestSinceMinutesFilter:
    def test_stale_rows_dropped(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-fresh")
        _heartbeat(client, as_user=ALICE, persona="persona-stale")
        # Rewind persona-stale 30 min into the past.
        _set_last_seen("persona-stale", datetime.now(UTC) - timedelta(minutes=30))
        # Default since_minutes = 15; persona-stale should drop out.
        body = _list_active(client, as_user=ALICE, include_self=True)
        personas = {p["persona"] for p in body["active_peers"]}
        assert "persona-fresh" in personas
        assert "persona-stale" not in personas

    def test_widened_window_includes_stale(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-stale")
        _set_last_seen("persona-stale", datetime.now(UTC) - timedelta(minutes=30))
        body = _list_active(client, as_user=ALICE, include_self=True, since_minutes=60)
        personas = {p["persona"] for p in body["active_peers"]}
        assert "persona-stale" in personas


class TestDiscoverableFilter:
    def test_undiscoverable_rows_hidden(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-public", discoverable=True)
        _heartbeat(client, as_user=ALICE, persona="persona-private", discoverable=False)
        body = _list_active(client, as_user=ALICE, include_self=True)
        personas = {p["persona"] for p in body["active_peers"]}
        assert "persona-public" in personas
        assert "persona-private" not in personas


class TestGroupFilter:
    def test_group_param_narrows_to_one_group(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-alice")  # acme/engineering
        _heartbeat(client, as_user=BOB, persona="persona-bob")      # acme/solutions
        body = _list_active(client, as_user=ALICE, include_self=True, group="solutions")
        personas = {p["persona"] for p in body["active_peers"]}
        assert personas == {"persona-bob"}


class TestIncludeSelf:
    def test_self_excluded_by_default_when_persona_passed(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-me")
        _heartbeat(client, as_user=ALICE, persona="persona-other")
        body = _list_active(
            client, as_user=ALICE, include_self=False, self_persona="persona-me"
        )
        personas = {p["persona"] for p in body["active_peers"]}
        assert "persona-me" not in personas
        assert "persona-other" in personas

    def test_include_self_true_returns_self(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-me")
        body = _list_active(
            client, as_user=ALICE, include_self=True, self_persona="persona-me"
        )
        personas = {p["persona"] for p in body["active_peers"]}
        assert "persona-me" in personas


class TestResponseShape:
    def test_minutes_since_last_seen_is_non_negative_float(self, client: TestClient) -> None:
        _heartbeat(client, as_user=ALICE, persona="persona-shape")
        body = _list_active(client, as_user=ALICE, include_self=True)
        assert body["count"] == 1
        peer = body["active_peers"][0]
        assert peer["enterprise_id"] == "acme"
        assert peer["group_id"] == "engineering"
        assert peer["discoverable"] is True
        assert isinstance(peer["minutes_since_last_seen"], float)
        assert peer["minutes_since_last_seen"] >= 0.0
