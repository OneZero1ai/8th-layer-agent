"""Tests for Decision 27 — per-L2 isolation read-path migration.

Each migrated route gets a pair of tests:

* ``*_isolation_disabled`` — flag off (default), two L2s under the same
  Enterprise still see each other's data via enterprise-only filtering.
  Covers the legacy/V1 customer-l2 deployment shape.
* ``*_isolation_enabled`` — flag on (``PER_L2_ISOLATION=true``), two L2s
  see only their own data; cross-L2 reads return 404 / empty / disjoint
  results. Covers the multi-L2 customer (engineering + sga) shape.

The 18 read-path call sites land in three route modules:

* ``review.py`` — admin queue (``/review/queue``, ``/review/{id}``,
  ``/review/{id}/approve|reject``, ``/review/units``, ``/review/stats``,
  ``/review/pending-review``, ``DELETE /review/{id}``)
* ``crosstalk_routes.py`` — inbox + thread reads/closes (``GET
  /crosstalk/threads``, ``GET /crosstalk/threads/{id}``, ``GET
  /crosstalk/inbox``, ``POST /crosstalk/threads/{id}/close``, plus the
  send/reply idempotency lookups)
* ``activity_routes.py`` — ``/activity`` log read

All tests reuse the existing ``client`` fixture pattern + the seeded-
user helper from ``test_crosstalk_routes`` / ``test_review`` so the
fixture wiring stays consistent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.auth import hash_password

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def client_no_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Flag-off (default) — enterprise-only filtering on read paths."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "isolation_off.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.delenv("PER_L2_ISOLATION", raising=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_with_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Flag-on — composite ``(enterprise_id, group_id)`` filtering."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "isolation_on.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("PER_L2_ISOLATION", "true")
    with TestClient(app) as c:
        yield c


# ============================================================================
# Helpers (shared shape with test_crosstalk_routes / test_review)
# ============================================================================


def _seed_user(*, username: str, password: str, enterprise_id: str, group_id: str, role: str = "user") -> None:
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ?, role = ? WHERE username = ?",
            (enterprise_id, group_id, role, username),
        )


def _login_and_mint(client: TestClient, username: str, password: str) -> str:
    jwt_resp = client.post("/auth/login", json={"username": username, "password": password})
    assert jwt_resp.status_code == 200, jwt_resp.text
    jwt = jwt_resp.json()["token"]
    key_resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": f"{username}-test-key", "ttl": "30d"},
    )
    assert key_resp.status_code == 201, key_resp.text
    return key_resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _propose_ku(client: TestClient, key: str, *, summary: str, domain: str) -> str:
    """Helper: propose a KU and return its id.

    The propose-quality guard requires summary >=20 chars, detail
    >=20 chars, and action >=10 chars. Tests pad short markers up to
    those minima while keeping the marker substring distinguishable
    in assertions.
    """
    resp = client.post(
        "/api/v1/propose",
        headers=_bearer(key),
        json={
            "domains": [domain],
            "insight": {
                "summary": f"{summary} — verbose isolation test summary",
                "detail": ("detail body for the isolation regression test covering Decision 27 read-path migration"),
                "action": "do the configured thing carefully",
            },
            "context": {
                "what_i_was_doing": "testing per-L2 isolation read-path",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_two_l2s_with_admins(client: TestClient) -> tuple[str, str]:
    """Seed two L2s under one Enterprise + a per-L2 admin in each.

    Returns ``(eng_admin_key, sga_admin_key)``.
    """
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )
    _seed_user(
        username="sga-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="sga",
        role="admin",
    )
    eng_key = _login_and_mint(client, "eng-admin", "pw")
    sga_key = _login_and_mint(client, "sga-admin", "pw")
    return eng_key, sga_key


# ============================================================================
# /review/queue + /review/stats — KU dashboard (5 dashboard sites)
# ============================================================================


def test_review_queue_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: each admin's queue includes the *other* L2's pending KU."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    eng_id = _propose_ku(client, eng_key, summary="eng pending", domain="engineering")
    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")

    eng_resp = client.get("/review/queue", headers=_bearer(eng_key)).json()
    eng_ids = {item["knowledge_unit"]["id"] for item in eng_resp["items"]}
    assert eng_id in eng_ids
    # Flag off: enterprise-only filtering, so sga's KU shows up too.
    assert sga_id in eng_ids


def test_review_queue_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: each admin's queue is disjoint."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    eng_id = _propose_ku(client, eng_key, summary="eng pending", domain="engineering")
    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")

    eng_resp = client.get("/review/queue", headers=_bearer(eng_key)).json()
    eng_ids = {item["knowledge_unit"]["id"] for item in eng_resp["items"]}
    assert eng_id in eng_ids
    assert sga_id not in eng_ids

    sga_resp = client.get("/review/queue", headers=_bearer(sga_key)).json()
    sga_ids = {item["knowledge_unit"]["id"] for item in sga_resp["items"]}
    assert sga_id in sga_ids
    assert eng_id not in sga_ids


def test_review_stats_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: dashboard counts include the cross-L2 row."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    _propose_ku(client, eng_key, summary="eng pending", domain="engineering")
    _propose_ku(client, sga_key, summary="sga pending", domain="legal")

    stats = client.get("/review/stats", headers=_bearer(eng_key)).json()
    assert stats["counts"]["pending"] >= 2  # Both KUs counted under enterprise-only.


def test_review_stats_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: each admin sees only their own L2's counts and domains."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    _propose_ku(client, eng_key, summary="eng pending", domain="engineering")
    _propose_ku(client, sga_key, summary="sga pending", domain="legal")

    eng_stats = client.get("/review/stats", headers=_bearer(eng_key)).json()
    assert eng_stats["counts"]["pending"] == 1  # Only the eng KU.

    sga_stats = client.get("/review/stats", headers=_bearer(sga_key)).json()
    assert sga_stats["counts"]["pending"] == 1  # Only the sga KU.


def test_review_units_list_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: ``/review/units`` returns cross-L2 KUs (legacy bug)."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    eng_id = _propose_ku(client, eng_key, summary="eng pending", domain="shared-domain")
    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="shared-domain")

    items = client.get("/review/units", headers=_bearer(eng_key)).json()
    ids = {it["knowledge_unit"]["id"] for it in items}
    assert eng_id in ids
    assert sga_id in ids


def test_review_units_list_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: ``/review/units`` is disjoint per L2."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    eng_id = _propose_ku(client, eng_key, summary="eng pending", domain="shared-domain")
    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="shared-domain")

    eng_items = client.get("/review/units", headers=_bearer(eng_key)).json()
    eng_ids = {it["knowledge_unit"]["id"] for it in eng_items}
    assert eng_id in eng_ids
    assert sga_id not in eng_ids


# ============================================================================
# /review/{id} + approve/reject + delete
# ============================================================================


def test_review_get_unit_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: cross-L2 ``GET /review/{id}`` returns 200."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.get(f"/review/{sga_id}", headers=_bearer(eng_key))
    assert resp.status_code == 200


def test_review_get_unit_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: cross-L2 ``GET /review/{id}`` returns 404 (no enumeration oracle)."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.get(f"/review/{sga_id}", headers=_bearer(eng_key))
    assert resp.status_code == 404


def test_review_approve_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: cross-L2 admin can approve another L2's KU (legacy bug)."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.post(f"/review/{sga_id}/approve", headers=_bearer(eng_key))
    assert resp.status_code == 200


def test_review_approve_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: cross-L2 admin gets 404 on approve (consistent with get)."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.post(f"/review/{sga_id}/approve", headers=_bearer(eng_key))
    assert resp.status_code == 404
    # The sga admin can still approve their own KU.
    sga_resp = client.post(f"/review/{sga_id}/approve", headers=_bearer(sga_key))
    assert sga_resp.status_code == 200


def test_review_delete_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: cross-L2 delete succeeds (enterprise-only)."""
    client = client_no_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.delete(f"/review/{sga_id}", headers=_bearer(eng_key))
    assert resp.status_code == 204


def test_review_delete_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: cross-L2 delete returns 404."""
    client = client_with_isolation
    eng_key, sga_key = _seed_two_l2s_with_admins(client)

    sga_id = _propose_ku(client, sga_key, summary="sga pending", domain="legal")
    resp = client.delete(f"/review/{sga_id}", headers=_bearer(eng_key))
    assert resp.status_code == 404
    # sga admin can still delete their own KU.
    sga_resp = client.delete(f"/review/{sga_id}", headers=_bearer(sga_key))
    assert sga_resp.status_code == 204


# ============================================================================
# /crosstalk — thread / inbox / close
# ============================================================================


def _seed_two_l2_user_pairs() -> None:
    """Seed two pairs of users — one in each L2 — for crosstalk tests."""
    # pragma: allowlist secret
    _seed_user(username="eng-alice", password="pw", enterprise_id="acme", group_id="engineering")  # noqa: E501  # pragma: allowlist secret
    _seed_user(username="eng-bob", password="pw", enterprise_id="acme", group_id="engineering")  # noqa: E501  # pragma: allowlist secret
    _seed_user(username="sga-alice", password="pw", enterprise_id="acme", group_id="sga")  # pragma: allowlist secret
    _seed_user(username="sga-bob", password="pw", enterprise_id="acme", group_id="sga")  # pragma: allowlist secret


def test_crosstalk_threads_list_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: admin in L2 A sees threads from L2 B (enterprise-only)."""
    client = client_no_isolation
    _seed_two_l2_user_pairs()
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )

    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")
    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")

    client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "intra-sga", "subject": "sga thread"},
    )

    threads = client.get("/crosstalk/threads", headers=_bearer(eng_admin_key)).json()
    subjects = {t["subject"] for t in threads["items"]}
    assert "sga thread" in subjects  # leaks across L2 under flag-off


def test_crosstalk_threads_list_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: admin's thread list scoped to their own L2 only."""
    client = client_with_isolation
    _seed_two_l2_user_pairs()
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )

    eng_alice_key = _login_and_mint(client, "eng-alice", "pw")
    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")
    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")

    # eng thread
    client.post(
        "/crosstalk/messages",
        headers=_bearer(eng_alice_key),
        json={"to": "eng-bob", "content": "intra-eng", "subject": "eng thread"},
    )
    # sga thread
    client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "intra-sga", "subject": "sga thread"},
    )

    threads = client.get("/crosstalk/threads", headers=_bearer(eng_admin_key)).json()
    subjects = {t["subject"] for t in threads["items"]}
    assert "eng thread" in subjects
    assert "sga thread" not in subjects  # Per-L2 admin oversight only.


def test_crosstalk_get_thread_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: admin in L2 A can fetch L2 B's thread by id."""
    client = client_no_isolation
    _seed_two_l2_user_pairs()
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )

    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")
    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "intra-sga"},
    )
    thread_id = send.json()["thread_id"]

    resp = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(eng_admin_key))
    assert resp.status_code == 200  # Admin sees across-L2 under enterprise-only.


def test_crosstalk_get_thread_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: cross-L2 thread fetch returns 404 even for admin."""
    client = client_with_isolation
    _seed_two_l2_user_pairs()
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )

    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")
    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "intra-sga"},
    )
    thread_id = send.json()["thread_id"]

    resp = client.get(f"/crosstalk/threads/{thread_id}", headers=_bearer(eng_admin_key))
    assert resp.status_code == 404


def test_crosstalk_inbox_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: inbox cannot surface cross-L2 messages.

    Even if a malformed write somehow tagged a message ``to=eng-alice`` in
    the sga group, the inbox query refuses to return it because the
    composite filter pins both columns. This tightens the isolation
    invariant rather than relying on the write path's same-tenant check.
    """
    client = client_with_isolation
    _seed_two_l2_user_pairs()

    eng_alice_key = _login_and_mint(client, "eng-alice", "pw")
    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")

    # Each L2 sends a message to its own peer.
    client.post(
        "/crosstalk/messages",
        headers=_bearer(eng_alice_key),
        json={"to": "eng-bob", "content": "from eng"},
    )
    client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "from sga"},
    )

    # Each user's inbox sees only their own L2's messages.
    eng_bob_key = _login_and_mint(client, "eng-bob", "pw")
    sga_bob_key = _login_and_mint(client, "sga-bob", "pw")

    eng_inbox = client.get("/crosstalk/inbox", headers=_bearer(eng_bob_key)).json()
    assert eng_inbox["count"] == 1
    assert eng_inbox["items"][0]["content"] == "from eng"

    sga_inbox = client.get("/crosstalk/inbox", headers=_bearer(sga_bob_key)).json()
    assert sga_inbox["count"] == 1
    assert sga_inbox["items"][0]["content"] == "from sga"


def test_crosstalk_send_cross_l2_recipient_isolation_enabled(
    client_with_isolation: TestClient,
) -> None:
    """Flag on: ``POST /crosstalk/messages`` to a different-L2 user 404s.

    Direct send across L2 boundaries is not allowed under per-L2
    isolation; cross-L2 messaging requires the shared-domain primitive
    (separate PR). The 404 shape matches the
    "recipient not found in this Enterprise" branch so it leaks no
    enumeration oracle for cross-L2 user existence.
    """
    client = client_with_isolation
    _seed_two_l2_user_pairs()

    eng_alice_key = _login_and_mint(client, "eng-alice", "pw")
    resp = client.post(
        "/crosstalk/messages",
        headers=_bearer(eng_alice_key),
        json={"to": "sga-alice", "content": "cross-l2 attempt"},
    )
    assert resp.status_code == 404


def test_crosstalk_close_thread_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: cross-L2 thread close 404s."""
    client = client_with_isolation
    _seed_two_l2_user_pairs()
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )

    sga_alice_key = _login_and_mint(client, "sga-alice", "pw")
    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")

    send = client.post(
        "/crosstalk/messages",
        headers=_bearer(sga_alice_key),
        json={"to": "sga-bob", "content": "intra-sga"},
    )
    thread_id = send.json()["thread_id"]

    resp = client.post(
        f"/crosstalk/threads/{thread_id}/close",
        headers=_bearer(eng_admin_key),
        json={"reason": "interfering"},
    )
    assert resp.status_code == 404


# ============================================================================
# /activity — read endpoint
# ============================================================================


def test_activity_read_isolation_disabled(client_no_isolation: TestClient) -> None:
    """Flag off: admin in L2 A sees L2 B's activity rows (enterprise-only)."""
    client = client_no_isolation
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )
    _seed_user(username="sga-actor", password="pw", enterprise_id="acme", group_id="sga")  # pragma: allowlist secret

    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")
    sga_actor_key = _login_and_mint(client, "sga-actor", "pw")

    # sga-actor proposes (writes a propose event tagged tenant_group=sga).
    _propose_ku(client, sga_actor_key, summary="sga propose", domain="legal")

    rows = client.get("/activity", headers=_bearer(eng_admin_key)).json()
    personas = {r["persona"] for r in rows["items"]}
    # Flag off: enterprise-only filter so eng admin sees sga-actor's row.
    assert "sga-actor" in personas


def test_activity_read_isolation_enabled(client_with_isolation: TestClient) -> None:
    """Flag on: admin's ``/activity`` view scoped to their own L2."""
    client = client_with_isolation
    _seed_user(
        username="eng-admin",
        password="pw",  # pragma: allowlist secret
        enterprise_id="acme",
        group_id="engineering",
        role="admin",
    )
    _seed_user(username="eng-actor", password="pw", enterprise_id="acme", group_id="engineering")  # noqa: E501  # pragma: allowlist secret
    _seed_user(username="sga-actor", password="pw", enterprise_id="acme", group_id="sga")  # pragma: allowlist secret

    eng_admin_key = _login_and_mint(client, "eng-admin", "pw")
    eng_actor_key = _login_and_mint(client, "eng-actor", "pw")
    sga_actor_key = _login_and_mint(client, "sga-actor", "pw")

    _propose_ku(client, eng_actor_key, summary="eng propose", domain="engineering")
    _propose_ku(client, sga_actor_key, summary="sga propose", domain="legal")

    rows = client.get("/activity", headers=_bearer(eng_admin_key)).json()
    personas = {r["persona"] for r in rows["items"]}
    assert "eng-actor" in personas
    assert "sga-actor" not in personas
