"""Activity-log read-path instrumentation — agent#284.

The L2 ``activity_log`` records *write* events (``propose``,
``review_resolve`` …) but historically recorded zero *read* events.
This suite pins the read-path instrumentation added by agent#284:

* ``POST /api/v1/aigrp/lookup`` → an ``aigrp_lookup`` event carrying the
  trigger + request filters in ``payload`` and the matched ``ku_id``s
  with similarity scores in ``result_summary``.
* ``POST /api/v1/aigrp/forward-query`` → a ``query`` event (system row,
  ``persona=None``) recording that this L2 served a cross-L2 query.

Plus the hard requirement: a forced ``append_activity`` failure must
never fail the lookup/query — the audit write is fire-and-forget via
``BackgroundTasks`` and the ``log_activity`` helper swallows the error.

Mirrors the write-path style in ``test_activity_log_instrumentation.py``:
rows are read directly off-disk and asserted by ``event_type``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cq_server.app as app_module
from cq_server.app import _get_store, app
from cq_server.auth import hash_password


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")  # pragma: allowlist secret
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")  # pragma: allowlist secret
    with TestClient(app) as c:
        yield c


def _seed_user(
    *,
    username: str,
    password: str,
    enterprise_id: str = "acme",
    group_id: str = "engineering",
) -> None:
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
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
        json={"name": "read-path-test", "ttl": "30d"},
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


def _propose_one(
    client: TestClient,
    api_key: str,
    summary: str = "Read-path instrumentation coverage pin for agent#284",
) -> str:
    resp = client.post(
        "/propose",
        json={
            "domains": ["test-fleet"],
            "insight": {
                "summary": summary,
                "detail": "A KU used to back the aigrp_lookup instrumentation test.",
                "action": "Read tests/test_activity_log_read_path.py for coverage.",
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _stub_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``embed_text`` deterministic and Bedrock-free.

    The handler only needs ``embed_text`` to return a non-None
    ``(packed_bytes, model_id)`` tuple — the actual vector is irrelevant
    because ``semantic_query`` is stubbed separately in the tests that
    need a hit.
    """
    vec = [1.0] + [0.0] * 7
    packed = struct.pack(f"<{len(vec)}f", *vec)

    def _fake(_text: str):
        return packed, "stub-model"

    monkeypatch.setattr(app_module, "embed_text", _fake)


# ---------------------------------------------------------------------------
# aigrp/lookup → aigrp_lookup event
# ---------------------------------------------------------------------------


class TestAigrpLookupLogs:
    def test_lookup_writes_aigrp_lookup_row_with_matches(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user(username="looker", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "looker", "pw"))  # pragma: allowlist secret
        ku_id = _propose_one(client, api_key)
        store = _get_store()
        store.sync.set_review_status(ku_id, "approved", "looker")

        _stub_embed(monkeypatch)
        # Force one deterministic hit so the row carries a ku_id +
        # similarity score regardless of the embedding backend.
        unit = asyncio.run(store.get(ku_id))
        assert unit is not None

        async def _fake_semantic_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return [(unit, 0.91)]

        monkeypatch.setattr(store, "semantic_query", _fake_semantic_query)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "context": "how do I rotate an IAM key",
                "trigger": "user_prompt",
                "session_id": "sess-abc",
                "persona": "looker",
                "min_confidence": 0.0,
                "min_similarity": 0.0,
                # The seeded KU was proposed by ``looker``; without
                # this the exclude_self filter would drop the only hit.
                "exclude_self": False,
            },
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        lookup_rows = [r for r in rows if r["event_type"] == "aigrp_lookup"]
        assert len(lookup_rows) == 1
        row = lookup_rows[0]
        assert row["persona"] == "looker"
        assert row["tenant_enterprise"] == "acme"
        assert row["tenant_group"] == "engineering"
        assert row["thread_or_chain_id"] == "sess-abc"

        payload = json.loads(row["payload"])
        assert payload["trigger"] == "user_prompt"
        assert payload["session_id"] == "sess-abc"
        assert payload["embed_unavailable"] is False

        summary = json.loads(row["result_summary"])
        assert ku_id in summary["ku_ids"]
        assert summary["hits"] == 1
        assert "elapsed_ms" in summary
        assert summary["matches"][0]["ku_id"] == ku_id
        assert summary["matches"][0]["similarity"] == pytest.approx(0.91)

    def test_lookup_logs_when_embedding_unavailable(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Bedrock-down lookup still logs a zero-result row.

        ``embed_text`` returning None is the degraded path; the handler
        returns empty results but the lookup is still a real
        fleet-analytics event and must be recorded.
        """
        _seed_user(username="degraded", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "degraded", "pw"))

        monkeypatch.setattr(app_module, "embed_text", lambda _t: None)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"context": "anything", "trigger": "session_start"},
        )
        assert resp.status_code == 200, resp.text

        rows = _activity_rows(tmp_path / "activity.db")
        lookup_rows = [r for r in rows if r["event_type"] == "aigrp_lookup"]
        assert len(lookup_rows) == 1
        payload = json.loads(lookup_rows[0]["payload"])
        assert payload["embed_unavailable"] is True
        assert payload["trigger"] == "session_start"
        summary = json.loads(lookup_rows[0]["result_summary"])
        assert summary["hits"] == 0
        assert summary["ku_ids"] == []

    def test_lookup_writes_row_on_zero_result_lookup(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lookup that embeds fine but matches nothing still logs (agent#306).

        The S1 acceptance smoke pinned this explicitly: even a
        no-result lookup must be auditable. This is distinct from the
        embedding-unavailable path — here ``embed_text`` succeeds and
        the handler runs the full filter loop; ``semantic_query`` just
        returns an empty candidate set. The ``aigrp_lookup`` row must
        still be written, with ``embed_unavailable: False`` and an
        empty ``ku_ids``.
        """
        _seed_user(username="empty-look", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "empty-look", "pw"))

        _stub_embed(monkeypatch)
        store = _get_store()

        async def _no_hits(_vec, *, limit: int = 10):  # noqa: ANN001
            return []

        monkeypatch.setattr(store, "semantic_query", _no_hits)

        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"context": "nothing will match this", "trigger": "tool_failure"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"] == []

        rows = _activity_rows(tmp_path / "activity.db")
        lookup_rows = [r for r in rows if r["event_type"] == "aigrp_lookup"]
        assert len(lookup_rows) == 1
        payload = json.loads(lookup_rows[0]["payload"])
        assert payload["embed_unavailable"] is False
        assert payload["trigger"] == "tool_failure"
        summary = json.loads(lookup_rows[0]["result_summary"])
        assert summary["hits"] == 0
        assert summary["ku_ids"] == []
        assert "elapsed_ms" in summary

    def test_lookup_succeeds_when_append_activity_raises(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A logging failure must never fail the lookup (agent#284).

        ``aigrp/lookup`` is the highest-volume read path — a DB hiccup
        in the audit append must be swallowed by ``log_activity`` and
        leave the lookup response intact.
        """
        _seed_user(username="resilient-look", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "resilient-look", "pw"))

        _stub_embed(monkeypatch)
        store = _get_store()

        async def _empty_semantic_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return []

        monkeypatch.setattr(store, "semantic_query", _empty_semantic_query)

        original = store.append_activity

        async def _raises(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated DB failure during audit append")

        monkeypatch.setattr(store, "append_activity", _raises)
        try:
            resp = client.post(
                "/api/v1/aigrp/lookup",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"context": "still works", "trigger": "user_prompt"},
            )
            assert resp.status_code == 200, resp.text

            rows = _activity_rows(tmp_path / "activity.db")
            assert all(r["event_type"] != "aigrp_lookup" for r in rows)
        finally:
            monkeypatch.setattr(store, "append_activity", original)


# ---------------------------------------------------------------------------
# aigrp/forward-query → query event
# ---------------------------------------------------------------------------


class TestForwardQueryLogs:
    def test_forward_query_writes_query_row(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A served same-Enterprise forward-query logs a system query row."""
        from cq_server import aigrp

        # Bypass the peer-key + forwarder-identity gates — those are
        # exercised by test_forward_query.py; here we pin the activity
        # row, not the auth surface.
        app.dependency_overrides[aigrp.require_peer_key] = lambda: None

        async def _noop_identity(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(aigrp, "require_forwarder_identity", _noop_identity)

        store = _get_store()

        async def _empty_scope_query(_vec, *, limit: int = 10):  # noqa: ANN001
            return []

        monkeypatch.setattr(store, "semantic_query_with_scope", _empty_scope_query)

        try:
            resp = client.post(
                "/api/v1/aigrp/forward-query",
                headers={"X-8L-Forwarder-L2-Id": "peer-l2"},
                json={
                    "query_vec": [0.1] * 8,
                    "requester_l2_id": "peer-l2",
                    "requester_enterprise": aigrp.enterprise(),
                    "requester_group": "remote-group",
                    "requester_persona": "remote-persona",
                    "max_results": 5,
                },
            )
            assert resp.status_code == 200, resp.text
        finally:
            app.dependency_overrides.pop(aigrp.require_peer_key, None)

        rows = _activity_rows(tmp_path / "activity.db")
        query_rows = [r for r in rows if r["event_type"] == "query"]
        assert len(query_rows) == 1
        row = query_rows[0]
        # Forward-query is a system event — no local persona.
        assert row["persona"] is None
        # Filed under this L2's own Enterprise/Group.
        assert row["tenant_enterprise"] == aigrp.enterprise()

        payload = json.loads(row["payload"])
        assert payload["source"] == "forward_query"
        assert payload["requester_l2_id"] == "peer-l2"
        assert payload["requester_persona"] == "remote-persona"

        summary = json.loads(row["result_summary"])
        assert summary["ku_ids"] == []
        assert summary["result_count"] == 0


# ---------------------------------------------------------------------------
# CDN-cacheability guard — agent#306 root cause
# ---------------------------------------------------------------------------


class TestApiResponsesAreNotCacheable:
    """API responses must carry ``Cache-Control: no-store`` (agent#306).

    The L2 sits behind a CDN in some deployments. Without an explicit
    no-store header the CDN cached ``GET /api/v1/activity`` and served a
    stale page that never reflected freshly-written ``aigrp_lookup``
    rows — the false "read-path instrumentation not firing" symptom #306
    was filed for. The audit log, and every dynamic API response, must
    always be live.
    """

    def test_activity_read_sends_no_store(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user(username="cache-look", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "cache-look", "pw"))
        resp = client.get(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"

    def test_aigrp_lookup_response_sends_no_store(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user(username="cache-look2", password="pw")  # pragma: allowlist secret
        api_key = _mint_api_key(client, _login_jwt(client, "cache-look2", "pw"))
        monkeypatch.setattr(app_module, "embed_text", lambda _t: None)
        resp = client.post(
            "/api/v1/aigrp/lookup",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"context": "x", "trigger": "user_prompt"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"

    def test_root_mounted_api_route_sends_no_store(
        self,
        client: TestClient,
    ) -> None:
        """The SDK-compat root mount (no ``/api/v1`` prefix) is covered too."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"

    def test_unauthenticated_401_is_not_cacheable(
        self,
        client: TestClient,
    ) -> None:
        """An auth failure must not be cacheable either — a cached 401
        (or a cached 200 in its place) is how the CDN leaked stale auth
        state in agent#306."""
        resp = client.get("/api/v1/activity")
        assert resp.status_code == 401
        assert resp.headers.get("cache-control") == "no-store"
