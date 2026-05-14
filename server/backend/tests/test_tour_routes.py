"""Tests for the founder-tour persistence endpoints.

Covers the GET/PUT contract on ``/api/v1/users/me/tour-state``:

* GET on a fresh user → empty defaults (never-touched row reads as
  ``{completed_at: null, dismissed_at: null, current_step: 0}``).
* PUT round-trips an arbitrary state and survives a follow-up GET.
* The "now" sentinel on PUT gets stamped to an ISO-8601 timestamp.
* Anonymous callers 401 (the auth dep is on the router).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.auth import get_current_user, hash_password
from cq_server.deps import require_api_key


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "tour.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    app.dependency_overrides[require_api_key] = lambda: "founder"
    app.dependency_overrides[get_current_user] = lambda: "founder"
    with TestClient(app) as c:
        # Seed the row so the UPDATE path has something to hit.
        from cq_server.app import _get_store

        store = _get_store()
        store.sync.create_user("founder", hash_password("pw-1234567"))
        yield c
    app.dependency_overrides.pop(require_api_key, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_get_fresh_user_returns_defaults(client: TestClient) -> None:
    resp = client.get("/api/v1/users/me/tour-state")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"completed_at": None, "dismissed_at": None, "current_step": 0}


def test_put_then_get_round_trips(client: TestClient) -> None:
    put = client.put(
        "/api/v1/users/me/tour-state",
        json={"completed_at": None, "dismissed_at": None, "current_step": 3},
    )
    assert put.status_code == 200, put.text
    assert put.json()["current_step"] == 3

    get = client.get("/api/v1/users/me/tour-state")
    assert get.status_code == 200
    body = get.json()
    assert body["current_step"] == 3
    assert body["completed_at"] is None


def test_put_now_sentinel_stamps_iso_timestamp(client: TestClient) -> None:
    put = client.put(
        "/api/v1/users/me/tour-state",
        json={"completed_at": "now", "current_step": 8},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["current_step"] == 8
    # ISO-8601 with timezone — starts with year and includes 'T'.
    assert body["completed_at"] is not None
    assert body["completed_at"][:4].isdigit()
    assert "T" in body["completed_at"]


def test_put_404_for_missing_user(client: TestClient) -> None:
    """If the user row vanishes mid-flight, PUT surfaces 404 not 500."""
    from cq_server.app import _get_store
    from sqlalchemy import text

    # Delete the seeded user — simulates the mid-flight race.
    store = _get_store()
    with store._engine.begin() as conn:  # noqa: SLF001
        conn.execute(text("DELETE FROM users WHERE username = :u"), {"u": "founder"})

    resp = client.put(
        "/api/v1/users/me/tour-state",
        json={"completed_at": None, "current_step": 1},
    )
    assert resp.status_code == 404, resp.text
