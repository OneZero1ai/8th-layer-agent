"""Phase 6 step 2: cross-L2 /aigrp/forward-query endpoint tests.

Covers the policy-evaluation matrix:

  - same-Enterprise + same-Group               -> full_body
  - same-Enterprise + different-Group, no flag -> summary_only
  - same-Enterprise + different-Group, flag    -> full_body
  - different-Enterprise, no consent           -> empty (silent deny)
  - different-Enterprise, with consent         -> summary_only

Plus the audit-log invariant (every call writes one row).
"""

from __future__ import annotations

import sqlite3
import struct
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cq.models import Insight, create_knowledge_unit
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.tables import DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID

PEER_KEY = "test-peer-key-forward-query"


def _pack_vec(vec: list[float]) -> bytes:
    """Pack a float list into the same little-endian float32 bytes the
    Bedrock embedder writes — keeps the test fixture independent of
    Bedrock connectivity."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unit_vec(dim: int = 8, axis: int = 0) -> list[float]:
    """Unit vector with all weight on one axis. Two such vectors with the
    same axis cosine-similar to ~1.0; different axes give ~0.0. Easier
    to reason about than random vectors."""
    v = [0.0] * dim
    v[axis] = 1.0
    return v


@pytest.fixture()
def aigrp_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient configured with the AIGRP peer key + a clean DB.

    The responder L2's identity is set via env: enterprise=acme,
    group=solutions. Tests exercise requester_enterprise/group via the
    request body to drive the policy matrix.
    """
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "fwdq.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_AIGRP_PEER_KEY", PEER_KEY)
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "solutions")
    # Disable Bedrock — tests inject pre-packed embeddings directly.
    monkeypatch.setenv("CQ_EMBED_ENABLED", "false")
    with TestClient(app) as c:
        yield c


def _seed_ku(
    *,
    summary: str = "AWS CloudFront cache key gotcha",
    detail: str = "Cache key omits query strings by default.",
    action: str = "Add forwarded query strings to the cache policy.",
    domains: list[str] | None = None,
    embedding_axis: int = 0,
    enterprise_id: str = "acme",
    group_id: str = "solutions",
    cross_group_allowed: bool = False,
) -> str:
    """Insert an approved KU with an explicit embedding and tenancy scope.

    Returns the new KU id. The runtime ``insert`` path always stamps
    default-enterprise/default-group, so tests then UPDATE the row to
    apply the desired tenancy / xgroup flag — keeps the production code
    path clean.
    """
    store = _get_store()
    domains = domains or ["test-fleet", "aws", "cloudfront"]
    unit = create_knowledge_unit(
        domains=domains,
        insight=Insight(summary=summary, detail=detail, action=action),
    )
    embedding = _pack_vec(_unit_vec(axis=embedding_axis))
    store.sync.insert(unit, embedding=embedding, embedding_model="amazon.titan-embed-text-v2:0")
    # Approve so semantic_query picks it up.
    store.sync.set_review_status(unit.id, "approved", "test-reviewer")
    # Set tenancy scope + xgroup flag explicitly.
    with store._engine.begin() as _c:
        _c.exec_driver_sql(
            "UPDATE knowledge_units SET enterprise_id = ?, group_id = ?, "
            "cross_group_allowed = ? WHERE id = ?",
            (enterprise_id, group_id, 1 if cross_group_allowed else 0, unit.id),
        )
    return unit.id


def _post_forward_query(
    client: TestClient,
    *,
    requester_enterprise: str,
    requester_group: str,
    requester_l2_id: str | None = None,
    requester_persona: str = "persona-cloudfront-asker",
    query_axis: int = 0,
    max_results: int = 5,
    peer_key: str = PEER_KEY,
    forwarder_l2_id: str | None = None,
) -> Any:
    """Issue a forward-query call with a unit-vector embedding.

    SEC-CRIT #34: ``X-8L-Forwarder-L2-Id`` is required and must match
    ``requester_l2_id``; defaults derived from requester scope.
    """
    if requester_l2_id is None:
        requester_l2_id = f"{requester_enterprise}/{requester_group}"
    if forwarder_l2_id is None:
        forwarder_l2_id = requester_l2_id
    return client.post(
        "/api/v1/aigrp/forward-query",
        headers={
            "authorization": f"Bearer {peer_key}",
            "x-8l-forwarder-l2-id": forwarder_l2_id,
        },
        json={
            "query_vec": _unit_vec(axis=query_axis),
            "query_text": "cloudfront cache",
            "requester_l2_id": requester_l2_id,
            "requester_enterprise": requester_enterprise,
            "requester_group": requester_group,
            "requester_persona": requester_persona,
            "max_results": max_results,
        },
    )


# ---------------------------------------------------------------------------
# 1. same-Enterprise, different-Group, cross_group_allowed = false
# ---------------------------------------------------------------------------


class TestSameEnterpriseDiffGroupSummaryOnly:
    def test_returns_summary_only_when_flag_off(self, aigrp_client: TestClient) -> None:
        ku_id = _seed_ku(
            enterprise_id="acme",
            group_id="solutions",  # responder is acme/solutions
            cross_group_allowed=False,
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="acme",
            requester_group="engineering",  # different group
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["responder_enterprise"] == "acme"
        assert body["responder_group"] == "solutions"
        assert body["policy_applied"] == "summary_only"
        assert body["result_count"] == 1
        hit = body["results"][0]
        assert hit["ku_id"] == ku_id
        assert hit["summary"] == "AWS CloudFront cache key gotcha"
        # Detail/action redacted.
        assert hit["detail"] is None
        assert hit["action"] is None
        assert set(hit["redacted_fields"]) == {"detail", "action"}


# ---------------------------------------------------------------------------
# 2. same-Enterprise, different-Group, cross_group_allowed = true
# ---------------------------------------------------------------------------


class TestSameEnterpriseDiffGroupWithFlag:
    def test_returns_full_body_when_flag_on(self, aigrp_client: TestClient) -> None:
        ku_id = _seed_ku(
            enterprise_id="acme",
            group_id="solutions",
            cross_group_allowed=True,
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="acme",
            requester_group="engineering",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["policy_applied"] == "full_body"
        assert body["result_count"] == 1
        hit = body["results"][0]
        assert hit["ku_id"] == ku_id
        assert hit["detail"] == "Cache key omits query strings by default."
        assert hit["action"] == "Add forwarded query strings to the cache policy."
        assert hit["redacted_fields"] == []


# ---------------------------------------------------------------------------
# 3. cross-Enterprise, no consent -> silent zero results
# ---------------------------------------------------------------------------


class TestCrossEnterpriseNoConsent:
    def test_returns_empty_not_401(self, aigrp_client: TestClient) -> None:
        # Seed something findable on the responder side.
        _seed_ku(enterprise_id="acme", group_id="solutions")
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="initech",  # foreign Enterprise
            requester_group="r-and-d",
        )
        # Critically: NOT 401 — consent state must not be probeable.
        assert resp.status_code == 200
        body = resp.json()
        assert body["policy_applied"] == "denied"
        assert body["result_count"] == 0
        assert body["results"] == []


# ---------------------------------------------------------------------------
# 4. cross-Enterprise, with consent -> summary_only
# ---------------------------------------------------------------------------


class TestCrossEnterpriseWithConsent:
    def test_returns_summary_only_when_consent_signed(self, aigrp_client: TestClient) -> None:
        ku_id = _seed_ku(
            enterprise_id="acme",
            group_id="solutions",
            cross_group_allowed=False,  # ignored on cross-Enterprise path
        )
        # Sign a consent: initech -> acme, summary_only.
        store = _get_store()
        store.sync.insert_cross_enterprise_consent(
            consent_id="cons_" + uuid.uuid4().hex[:12],
            requester_enterprise="initech",
            responder_enterprise="acme",
            requester_group=None,  # any group on the requester side
            responder_group=None,
            policy="summary_only",
            signed_by_admin="admin@acme.example",
            signed_at="2026-04-30T00:00:00+00:00",
            expires_at=None,
            audit_log_id="aud_" + uuid.uuid4().hex[:12],
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="initech",
            requester_group="r-and-d",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["policy_applied"] == "summary_only"
        assert body["result_count"] == 1
        hit = body["results"][0]
        assert hit["ku_id"] == ku_id
        assert hit["detail"] is None
        assert hit["action"] is None
        assert set(hit["redacted_fields"]) == {"detail", "action"}


# ---------------------------------------------------------------------------
# 5. audit log written on every call
# ---------------------------------------------------------------------------


class TestAuditLog:
    def _audit_rows(self) -> list[tuple[Any, ...]]:
        store = _get_store()
        with store._lock:
            return store._engine.connect().exec_driver_sql(
                "SELECT requester_enterprise, requester_group, "
                "responder_enterprise, responder_group, policy_applied, "
                "result_count, consent_id "
                "FROM cross_l2_audit ORDER BY ts ASC"
            ).fetchall()

    def test_audit_row_written_on_every_call(self, aigrp_client: TestClient) -> None:
        _seed_ku(enterprise_id="acme", group_id="solutions")
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="acme",
            requester_group="engineering",
        )
        assert resp.status_code == 200
        rows = self._audit_rows()
        assert len(rows) == 1
        (req_ent, req_grp, resp_ent, resp_grp, policy, count, consent_id) = rows[0]
        assert req_ent == "acme"
        assert req_grp == "engineering"
        assert resp_ent == "acme"
        assert resp_grp == "solutions"
        assert policy == "summary_only"
        assert count == 1
        assert consent_id is None  # same-Enterprise → no consent record involved

    def test_denied_call_also_logs_audit_row(self, aigrp_client: TestClient) -> None:
        _seed_ku(enterprise_id="acme", group_id="solutions")
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="initech",
            requester_group="r-and-d",
        )
        assert resp.status_code == 200
        rows = self._audit_rows()
        assert len(rows) == 1
        assert rows[0][4] == "denied"
        assert rows[0][5] == 0

    def test_consent_id_recorded_on_cross_enterprise_hit(self, aigrp_client: TestClient) -> None:
        _seed_ku(enterprise_id="acme", group_id="solutions")
        store = _get_store()
        consent_id = "cons_" + uuid.uuid4().hex[:12]
        store.sync.insert_cross_enterprise_consent(
            consent_id=consent_id,
            requester_enterprise="initech",
            responder_enterprise="acme",
            requester_group=None,
            responder_group=None,
            policy="summary_only",
            signed_by_admin="admin@acme.example",
            signed_at="2026-04-30T00:00:00+00:00",
            expires_at=None,
            audit_log_id="aud_" + uuid.uuid4().hex[:12],
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="initech",
            requester_group="r-and-d",
        )
        assert resp.status_code == 200
        rows = self._audit_rows()
        assert len(rows) == 1
        assert rows[0][6] == consent_id


# ---------------------------------------------------------------------------
# 6. auth — wrong peer key still rejected
# ---------------------------------------------------------------------------


class TestAuth:
    def test_wrong_peer_key_returns_401(self, aigrp_client: TestClient) -> None:
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="acme",
            requester_group="engineering",
            peer_key="wrong-key-of-same-length-as-real",
        )
        assert resp.status_code == 401

    def test_missing_peer_key_returns_401(self, aigrp_client: TestClient) -> None:
        resp = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            json={
                "query_vec": _unit_vec(),
                "requester_l2_id": "acme-engineering-l2",
                "requester_enterprise": "acme",
                "requester_group": "engineering",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 7. defaults — legacy KUs (no explicit scope) still match same-Enterprise
# ---------------------------------------------------------------------------


class TestDefaultsSafety:
    def test_legacy_default_scope_does_not_leak_to_strangers(
        self, aigrp_client: TestClient
    ) -> None:
        # A KU with default-enterprise / default-group is in a different
        # Enterprise from the responder (acme). Must not leak.
        _seed_ku(
            enterprise_id=DEFAULT_ENTERPRISE_ID,
            group_id=DEFAULT_GROUP_ID,
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise=DEFAULT_ENTERPRISE_ID,
            requester_group=DEFAULT_GROUP_ID,
        )
        # Requester claims default-enterprise but responder is "acme" —
        # cross-Enterprise path with no consent → empty.
        assert resp.status_code == 200
        body = resp.json()
        assert body["policy_applied"] == "denied"
        assert body["result_count"] == 0


# ---------------------------------------------------------------------------
# 8. consent — cross_enterprise_consents schema constraints
# ---------------------------------------------------------------------------


class TestConsentLookup:
    def test_expired_consent_is_ignored(self, aigrp_client: TestClient) -> None:
        _seed_ku(enterprise_id="acme", group_id="solutions")
        store = _get_store()
        store.sync.insert_cross_enterprise_consent(
            consent_id="cons_expired",
            requester_enterprise="initech",
            responder_enterprise="acme",
            requester_group=None,
            responder_group=None,
            policy="summary_only",
            signed_by_admin="admin@acme.example",
            signed_at="2024-01-01T00:00:00+00:00",
            expires_at="2024-12-31T23:59:59+00:00",  # in the past
            audit_log_id="aud_expired",
        )
        resp = _post_forward_query(
            aigrp_client,
            requester_enterprise="initech",
            requester_group="r-and-d",
        )
        body = resp.json()
        assert body["policy_applied"] == "denied"
        assert body["result_count"] == 0


# ---------------------------------------------------------------------------
# 9. /aigrp/* prefix mount — endpoint is reachable on both / and /api/v1
# ---------------------------------------------------------------------------


class TestRouteMounts:
    def test_endpoint_reachable_on_root_path(self, aigrp_client: TestClient) -> None:
        # SDK callers hit / directly (legacy compat); frontend hits /api/v1.
        _seed_ku(enterprise_id="acme", group_id="solutions")
        resp = aigrp_client.post(
            "/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                "x-8l-forwarder-l2-id": "acme/engineering",
            },
            json={
                "query_vec": _unit_vec(),
                "requester_l2_id": "acme/engineering",
                "requester_enterprise": "acme",
                "requester_group": "engineering",
                "max_results": 5,
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 10. SQLite NOT NULL on cross_group_allowed
# ---------------------------------------------------------------------------


class TestSchemaConstraints:
    def test_cross_group_allowed_column_is_not_null(self, aigrp_client: TestClient) -> None:
        import sqlalchemy.exc

        store = _get_store()
        with pytest.raises((sqlite3.IntegrityError, sqlalchemy.exc.IntegrityError)):
            with store._engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO knowledge_units (id, data, cross_group_allowed) "
                    "VALUES (?, ?, ?)",
                    ("ku_null_xgroup", "{}", None),
                )
