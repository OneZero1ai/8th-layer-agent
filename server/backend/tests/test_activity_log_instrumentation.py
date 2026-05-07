"""Activity-log instrumentation — #108 Stage 2 Workstream A.

Pins that every write-path handler emits exactly the right
``activity_log`` row(s) via FastAPI ``BackgroundTasks``. Failure of
the audit write must never affect the response (covered by a separate
suite below — we monkey-patch ``store.append_activity`` to raise and
assert the response is still 2xx).

Coverage:

* ``GET /query`` → ``query`` event with payload + result_summary
* ``POST /propose`` → ``propose`` event with summary_first_60_chars
* ``POST /confirm/{id}`` → ``confirm`` event with ku_id
* ``POST /flag/{id}`` → ``flag`` event with reason
* ``POST /review/{id}/approve`` → ``review_resolve`` decision=approve
* ``POST /review/{id}/reject`` → ``review_resolve`` decision=reject
* ``POST /consults/request`` → ``consult_open`` with thread_id
* ``POST /consults/{id}/messages`` → ``consult_reply``
* ``POST /consults/{id}/close`` → ``consult_close`` with reason
* All rows carry the caller's ``persona`` and ``tenant_enterprise``
  resolved from the user row (never the schema default).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.auth import hash_password


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "activity.db"))
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
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
    if role != "user":
        store.sync.set_user_role(username, role)
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            (enterprise_id, group_id, username),
        )


def _login_jwt(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _mint_api_key(client: TestClient, jwt_token: str) -> str:
    resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "activity-test", "ttl": "30d"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _activity_rows(db_path: Path) -> list[dict]:
    """Read all activity_log rows directly off-disk, ordered by ts."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, ts, tenant_enterprise, tenant_group, persona, human, "
            "event_type, payload, result_summary, thread_or_chain_id "
            "FROM activity_log ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "tenant_enterprise": r[2],
            "tenant_group": r[3],
            "persona": r[4],
            "human": r[5],
            "event_type": r[6],
            "payload": r[7],
            "result_summary": r[8],
            "thread_or_chain_id": r[9],
        }
        for r in rows
    ]


def _propose_one(client: TestClient, api_key: str) -> str:
    resp = client.post(
        "/propose",
        json={
            "domains": ["test-fleet"],
            "insight": {
                "summary": "Activity instrumentation pin",
                "detail": "Asserts a propose event is logged with the caller's persona.",
                "action": "Read tests/test_activity_log_instrumentation.py for the full coverage map.",
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Per-handler row shape
# ---------------------------------------------------------------------------


class TestProposeLogs:
    def test_propose_writes_activity_row(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="proposer", password="pw")
        api_key = _mint_api_key(client, _login_jwt(client, "proposer", "pw"))
        ku_id = _propose_one(client, api_key)

        rows = _activity_rows(tmp_path / "activity.db")
        propose_rows = [r for r in rows if r["event_type"] == "propose"]
        assert len(propose_rows) == 1
        row = propose_rows[0]
        assert row["persona"] == "proposer"
        assert row["tenant_enterprise"] == "acme"
        assert row["tenant_group"] == "engineering"
        assert ku_id in row["payload"]
        # summary_first_60_chars is the schema sketch shape — assert
        # it's present, not just substring-match the full summary.
        import json as _json

        payload = _json.loads(row["payload"])
        assert payload["ku_id"] == ku_id
        assert "summary_first_60_chars" in payload
        assert payload["summary_first_60_chars"] == "Activity instrumentation pin"
        assert payload["domains"] == ["test-fleet"]


class TestQueryLogs:
    def test_query_writes_activity_row_with_result_summary(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="querier", password="pw")
        api_key = _mint_api_key(client, _login_jwt(client, "querier", "pw"))
        ku_id = _propose_one(client, api_key)
        # Approve so the KU is queryable.
        store = _get_store()
        store.sync.set_review_status(ku_id, "approved", "querier")

        resp = client.get(
            "/query?domains=test-fleet&limit=5",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        query_rows = [r for r in rows if r["event_type"] == "query"]
        assert len(query_rows) == 1
        row = query_rows[0]
        assert row["persona"] == "querier"

        import json as _json

        payload = _json.loads(row["payload"])
        assert payload["domains"] == ["test-fleet"]
        assert payload["limit"] == 5
        result = _json.loads(row["result_summary"])
        # The KU we just approved should be in the result list.
        assert ku_id in result["ku_ids"]
        assert result["cache_hit"] is False


class TestConfirmFlagLogs:
    def test_confirm_writes_activity_row(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="confirmer", password="pw")
        api_key = _mint_api_key(client, _login_jwt(client, "confirmer", "pw"))
        ku_id = _propose_one(client, api_key)
        # confirm/flag both reach into ``store.get`` which filters by
        # status='approved'. Approve out-of-band so the handler 200s.
        _get_store().sync.set_review_status(ku_id, "approved", "confirmer")

        resp = client.post(
            f"/confirm/{ku_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        confirm_rows = [r for r in rows if r["event_type"] == "confirm"]
        assert len(confirm_rows) == 1
        import json as _json

        assert _json.loads(confirm_rows[0]["payload"])["ku_id"] == ku_id

    def test_flag_writes_activity_row_with_reason(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="flagger", password="pw")
        api_key = _mint_api_key(client, _login_jwt(client, "flagger", "pw"))
        ku_id = _propose_one(client, api_key)
        _get_store().sync.set_review_status(ku_id, "approved", "flagger")

        resp = client.post(
            f"/flag/{ku_id}",
            json={"reason": "stale"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        flag_rows = [r for r in rows if r["event_type"] == "flag"]
        assert len(flag_rows) == 1
        import json as _json

        payload = _json.loads(flag_rows[0]["payload"])
        assert payload["ku_id"] == ku_id
        # FlagReason is a StrEnum; the helper stringifies it via str().
        assert "stale" in payload["reason"]


class TestReviewLogs:
    def test_approve_writes_review_resolve_row(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="proposer2", password="pw")
        _seed_user(username="admin1", password="pw", role="admin")

        proposer_key = _mint_api_key(client, _login_jwt(client, "proposer2", "pw"))
        admin_jwt = _login_jwt(client, "admin1", "pw")
        ku_id = _propose_one(client, proposer_key)

        resp = client.post(
            f"/review/{ku_id}/approve",
            headers={"Authorization": f"Bearer {admin_jwt}"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        rr = [r for r in rows if r["event_type"] == "review_resolve"]
        assert len(rr) == 1
        import json as _json

        payload = _json.loads(rr[0]["payload"])
        assert payload["decision"] == "approve"
        assert payload["ku_id"] == ku_id
        assert rr[0]["thread_or_chain_id"] == ku_id  # correlates to the KU
        assert rr[0]["persona"] == "admin1"

    def test_reject_writes_review_resolve_row(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="proposer3", password="pw")
        _seed_user(username="admin2", password="pw", role="admin")

        proposer_key = _mint_api_key(client, _login_jwt(client, "proposer3", "pw"))
        admin_jwt = _login_jwt(client, "admin2", "pw")
        ku_id = _propose_one(client, proposer_key)

        resp = client.post(
            f"/review/{ku_id}/reject",
            headers={"Authorization": f"Bearer {admin_jwt}"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        rr = [r for r in rows if r["event_type"] == "review_resolve"]
        assert len(rr) == 1
        import json as _json

        payload = _json.loads(rr[0]["payload"])
        assert payload["decision"] == "reject"


class TestConsultLogs:
    def test_consult_lifecycle_writes_open_reply_close(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _seed_user(username="alice2", password="pw")
        _seed_user(username="bob2", password="pw")
        alice_jwt = _login_jwt(client, "alice2", "pw")
        bob_jwt = _login_jwt(client, "bob2", "pw")

        # Open
        resp = client.post(
            "/api/v1/consults/request",
            headers={"Authorization": f"Bearer {alice_jwt}"},
            json={
                "to_l2_id": "acme/engineering",
                "to_persona": "bob2",
                "subject": "test",
                "content": "hello",
            },
        )
        assert resp.status_code == 201, resp.text
        thread_id = resp.json()["thread_id"]

        # Reply (Bob to Alice)
        resp = client.post(
            f"/api/v1/consults/{thread_id}/messages",
            headers={"Authorization": f"Bearer {bob_jwt}"},
            json={"content": "hi back"},
        )
        assert resp.status_code == 201, resp.text

        # Close
        resp = client.post(
            f"/api/v1/consults/{thread_id}/close",
            headers={"Authorization": f"Bearer {alice_jwt}"},
            json={"reason": "resolved"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        consult_rows = [r for r in rows if r["event_type"].startswith("consult_")]
        events = sorted(r["event_type"] for r in consult_rows)
        assert events == ["consult_close", "consult_open", "consult_reply"]

        # Every consult row carries thread_id for workflow correlation.
        for r in consult_rows:
            assert r["thread_or_chain_id"] == thread_id


# ---------------------------------------------------------------------------
# Failure-mode pins — handler must succeed even when activity log fails.
# ---------------------------------------------------------------------------


class TestActivityLogFailureNeverBreaksResponse:
    def test_propose_succeeds_when_append_activity_raises(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The audit log is fire-and-forget by design (#108).

        Patch ``store.append_activity`` to raise on every call. The
        propose handler still returns 201 because the audit write runs
        after the response is sealed via BackgroundTasks; the
        ``log_activity`` helper swallows the exception with a logged
        warning.
        """
        _seed_user(username="resilient", password="pw")
        api_key = _mint_api_key(client, _login_jwt(client, "resilient", "pw"))

        store = _get_store()
        original = store.append_activity

        async def _raises(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated DB failure during audit append")

        monkeypatch.setattr(store, "append_activity", _raises)
        try:
            ku_id = _propose_one(client, api_key)
            assert ku_id  # propose response succeeded

            # No activity rows written (the patched method raised
            # instead of inserting).
            rows = _activity_rows(tmp_path / "activity.db")
            assert all(r["event_type"] != "propose" for r in rows)
        finally:
            monkeypatch.setattr(store, "append_activity", original)
