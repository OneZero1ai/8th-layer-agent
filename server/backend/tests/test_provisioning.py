"""Tests for the Enterprise Provisioning Service (FO-2-backend, Decision 31).

Covers:

Unit tests:
  * generate_job_id format + uniqueness
  * ULID id generator properties (sortable, prefixed)
  * DB helpers: insert_job, update_job_phase, complete_job, fail_job, get_job
  * Rate limit counter (count_recent_requests)
  * is_job_expired (COMPLETED + 24h window)
  * Pydantic model validation: slug regex, email, aws_account_id, region
  * Partial-unique migration semantics: FAILED slug retryable, active duplicate blocked

Route tests (mocked AWS + Cloudflare):
  * POST /api/v1/enterprises happy path → 200 + job row inserted
  * POST /api/v1/enterprises rate limit → 429 RATE_LIMIT
  * POST /api/v1/enterprises slug taken → 409 SLUG_TAKEN
  * POST /api/v1/enterprises bad region → 422 VALIDATION (pydantic)
  * POST /api/v1/enterprises bad slug format → 422 VALIDATION
  * POST /api/v1/enterprises AssumeRole fails → 403 ROLE_NOT_ASSUMABLE
  * GET /api/v1/enterprises/jobs/{id} happy path → 200
  * GET /api/v1/enterprises/jobs/{id} unknown id → 404
  * GET /api/v1/enterprises/jobs/{id} expired COMPLETED → 404
  * CORS preflight for signup.8th-layer.ai → 200 with correct headers

Integration smoke tests:
  * Full job lifecycle: create → job row exists → phase advance → complete
  * State machine phase transitions update DB correctly
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cq_server.app import app
from cq_server.provisioning.db import (
    complete_job,
    count_recent_requests,
    fail_job,
    get_job,
    insert_job,
    is_job_expired,
    update_job_phase,
)
from cq_server.provisioning.ids import generate_job_id
from cq_server.provisioning.models import CreateEnterpriseRequest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_BODY = {
    "enterprise_name": "Acme Corp",
    "enterprise_slug": "acme",
    "admin_email": "ceo@acme.com",
    "aws_account_id": "123456789012",
    "aws_region": "us-east-1",
    "marketplace_deploy_role_arn": "arn:aws:iam::123456789012:role/8thLayerL2Provisioner",
    # HIGH #1: ExternalId is now required.
    "assume_role_external_id": "acme-external-id-8chars-min",
}


def _make_db_engine(tmp_path: Path):
    """Create a SQLite DB with the provisioning_jobs table for unit tests.

    Schema mirrors the post-0021a migration shape: no inline UNIQUE on
    enterprise_id, partial unique index on (enterprise_id) WHERE status
    NOT IN ('FAILED', 'COMPLETED'). Tests covering retry-after-failure
    depend on this schema, so it MUST track the real migration head.
    """
    db_path = tmp_path / "prov_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with engine.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE provisioning_jobs (
                    job_id TEXT PRIMARY KEY,
                    enterprise_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    result_json TEXT,
                    ip_hash TEXT NOT NULL DEFAULT '',
                    assume_role_external_id TEXT NOT NULL DEFAULT '',
                    job_params_json TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX idx_provisioning_jobs_ip_hash_started_at "
                "ON provisioning_jobs(ip_hash, started_at)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX idx_provisioning_jobs_active_slug "
                "ON provisioning_jobs(enterprise_id) "
                "WHERE status NOT IN ('FAILED', 'COMPLETED')"
            )
        )
        conn.commit()
    return engine


@pytest.fixture
def db_engine(tmp_path: Path):
    return _make_db_engine(tmp_path)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests: ID generator
# ---------------------------------------------------------------------------


class TestGenerateJobId:
    def test_prefix(self) -> None:
        job_id = generate_job_id()
        assert job_id.startswith("prov_")

    def test_length(self) -> None:
        # "prov_" (5) + 26 ULID chars = 31 total
        job_id = generate_job_id()
        assert len(job_id) == 31

    def test_unique(self) -> None:
        ids = {generate_job_id() for _ in range(50)}
        assert len(ids) == 50

    def test_crockford_chars_only(self) -> None:
        crockford = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        job_id = generate_job_id()
        for ch in job_id[5:]:  # skip "prov_"
            assert ch in crockford, f"unexpected char {ch!r} in {job_id}"

    def test_sortable(self) -> None:
        """Later IDs sort after earlier ones (ULID property)."""
        ids = []
        for _ in range(5):
            ids.append(generate_job_id())
            time.sleep(0.001)
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Unit tests: Pydantic model validation
# ---------------------------------------------------------------------------


class TestCreateEnterpriseRequestValidation:
    def _make(self, **overrides) -> dict:
        body = dict(_VALID_BODY)
        body.update(overrides)
        return body

    def test_valid_passes(self) -> None:
        req = CreateEnterpriseRequest(**_VALID_BODY)
        assert req.enterprise_slug == "acme"

    def test_slug_too_short(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(enterprise_slug="ab"))

    def test_slug_starts_with_digit(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(enterprise_slug="1acme"))

    def test_slug_uppercase_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(enterprise_slug="Acme"))

    def test_slug_valid_with_hyphens(self) -> None:
        req = CreateEnterpriseRequest(**self._make(enterprise_slug="acme-corp-ai"))
        assert req.enterprise_slug == "acme-corp-ai"

    def test_bad_email_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(admin_email="notanemail"))

    def test_aws_account_id_11_digits_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(aws_account_id="12345678901"))

    def test_aws_account_id_13_digits_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(aws_account_id="1234567890123"))

    def test_unsupported_region_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(aws_region="eu-west-1"))

    def test_bad_role_arn_rejected(self) -> None:
        with pytest.raises(Exception):
            CreateEnterpriseRequest(**self._make(marketplace_deploy_role_arn="not-an-arn"))


# ---------------------------------------------------------------------------
# Unit tests: DB helpers
# ---------------------------------------------------------------------------


class TestDbHelpers:
    def test_insert_and_get(self, db_engine) -> None:
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id="prov_TESTJOB",
                enterprise_id="acme",
                status="PROVISIONING",
                phase=0,
                ip_hash="abc123",
            )
            row = get_job(conn, "prov_TESTJOB")
        assert row is not None
        assert row["enterprise_id"] == "acme"
        assert row["status"] == "PROVISIONING"

    def test_get_nonexistent_returns_none(self, db_engine) -> None:
        with db_engine.connect() as conn:
            row = get_job(conn, "prov_DOESNOTEXIST")
        assert row is None

    def test_update_phase(self, db_engine) -> None:
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id="prov_UPDATE",
                enterprise_id="beta",
                status="PROVISIONING",
                phase=0,
                ip_hash="x",
            )
            update_job_phase(conn, job_id="prov_UPDATE", status="KEY_MINT_IN_PROGRESS", phase=1)
            row = get_job(conn, "prov_UPDATE")
        assert row["status"] == "KEY_MINT_IN_PROGRESS"
        assert row["phase"] == 1

    def test_complete_job(self, db_engine) -> None:
        result = {"enterprise_id": "gamma", "l2_admin_url": "https://gamma.8th-layer.ai"}
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id="prov_COMPLETE",
                enterprise_id="gamma",
                status="PROVISIONING",
                phase=0,
                ip_hash="x",
            )
            complete_job(conn, job_id="prov_COMPLETE", result_json=result)
            row = get_job(conn, "prov_COMPLETE")
        assert row["status"] == "COMPLETED"
        assert row["phase"] == 6
        assert json.loads(row["result_json"]) == result
        assert row["completed_at"] is not None

    def test_fail_job(self, db_engine) -> None:
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id="prov_FAIL",
                enterprise_id="delta",
                status="PROVISIONING",
                phase=0,
                ip_hash="x",
            )
            fail_job(conn, job_id="prov_FAIL", error="phase 2: timeout")
            row = get_job(conn, "prov_FAIL")
        assert row["status"] == "FAILED"
        assert row["error"] == "phase 2: timeout"

    def test_failed_slug_can_be_retried_post_0021a(self, db_engine) -> None:
        """HIGH #4 (8l-reviewer third pass): after migration 0021a replaced
        the full UNIQUE on enterprise_id with a partial unique index, a
        customer whose first job FAILED must be able to re-POST the same
        slug. Empirical evidence — verified against actual sqlite DDL by
        the 8l-reviewer — that the original batch-mode migration left the
        UNIQUE constraint in place.
        """
        with db_engine.begin() as conn:
            insert_job(
                conn,
                job_id="prov_FAILED",
                enterprise_id="acme",
                status="PROVISIONING",
                phase=0,
                ip_hash="x",
            )
            fail_job(conn, job_id="prov_FAILED", error="phase 2: timeout")

        # Retry the SAME slug with a fresh active job — must succeed.
        with db_engine.begin() as conn:
            insert_job(
                conn,
                job_id="prov_RETRY",
                enterprise_id="acme",
                status="PROVISIONING",
                phase=0,
                ip_hash="y",
            )
            row = get_job(conn, "prov_RETRY")
            assert row is not None
            assert row["enterprise_id"] == "acme"
            assert row["status"] == "PROVISIONING"

    def test_duplicate_active_slug_still_blocked(self, db_engine) -> None:
        """The partial unique index must still block two concurrent in-flight
        jobs for the same slug — only FAILED/COMPLETED are exempt.
        """
        from sqlalchemy.exc import IntegrityError

        with db_engine.begin() as conn:
            insert_job(
                conn,
                job_id="prov_FIRST",
                enterprise_id="beta",
                status="KEY_MINT_IN_PROGRESS",
                phase=1,
                ip_hash="x",
            )

        with pytest.raises(IntegrityError):
            with db_engine.begin() as conn:
                insert_job(
                    conn,
                    job_id="prov_SECOND",
                    enterprise_id="beta",
                    status="PROVISIONING",
                    phase=0,
                    ip_hash="y",
                )

    def test_rate_limit_counter_empty(self, db_engine) -> None:
        with db_engine.connect() as conn:
            count = count_recent_requests(conn, "somehash", window_seconds=3600)
        assert count == 0

    def test_rate_limit_counter_counts_recent(self, db_engine) -> None:
        with db_engine.connect() as conn:
            for i in range(3):
                insert_job(
                    conn,
                    job_id=f"prov_RL{i}",
                    enterprise_id=f"rl-ent-{i}",
                    status="PROVISIONING",
                    phase=0,
                    ip_hash="ratelimitip",
                )
            count = count_recent_requests(conn, "ratelimitip", window_seconds=3600)
        assert count == 3

    def test_rate_limit_counter_ignores_different_ip(self, db_engine) -> None:
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id="prov_RLDIFF",
                enterprise_id="rl-ent-x",
                status="PROVISIONING",
                phase=0,
                ip_hash="otheripx",
            )
            count = count_recent_requests(conn, "ratelimitip", window_seconds=3600)
        assert count == 0


class TestIsJobExpired:
    def _row(self, status: str, completed_at: str | None) -> dict:
        return {
            "status": status,
            "completed_at": completed_at,
        }

    def test_not_completed_never_expired(self) -> None:
        assert is_job_expired(self._row("PROVISIONING", None)) is False

    def test_completed_fresh_not_expired(self) -> None:
        fresh = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        assert is_job_expired(self._row("COMPLETED", fresh)) is False

    def test_completed_25h_ago_expired(self) -> None:
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        assert is_job_expired(self._row("COMPLETED", old)) is True

    def test_completed_23h_ago_not_expired(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=23)).isoformat().replace("+00:00", "Z")
        assert is_job_expired(self._row("COMPLETED", recent)) is False

    def test_failed_never_expired(self) -> None:
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
        assert is_job_expired(self._row("FAILED", old)) is False


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def _mock_assume_role_ok(*args, **kwargs) -> None:
    """Patch _validate_assume_role to succeed (HIGH #1: now takes external_id)."""
    pass


def _mock_assume_role_fail(*args, **kwargs) -> None:
    raise RuntimeError("AccessDenied: not authorized to assume role")


class TestCreateEnterpriseRoute:
    def test_happy_path_returns_job_id(self, client: TestClient) -> None:
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["job_id"].startswith("prov_")
        assert body["enterprise_id"] == "acme"
        assert body["status"] == "PROVISIONING"
        assert "/api/v1/enterprises/jobs/" in body["poll_url"]

    def test_happy_path_job_row_in_db(self, client: TestClient) -> None:
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        job_id = resp.json()["job_id"]
        # Poll endpoint should return the job.
        poll = client.get(f"/api/v1/enterprises/jobs/{job_id}")
        assert poll.status_code == 200, poll.text
        poll_body = poll.json()
        assert poll_body["job_id"] == job_id
        assert poll_body["enterprise_id"] == "acme"

    def test_slug_taken_idempotent_returns_200(self, client: TestClient) -> None:
        """HIGH #7: duplicate POST with same slug returns existing job (200).
        Idempotency is the happy path; 409 only fires on a concurrent race.
        """
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp1 = client.post("/api/v1/enterprises", json=_VALID_BODY)
            resp2 = client.post("/api/v1/enterprises", json=_VALID_BODY)
        assert resp1.status_code == 200, resp1.text
        assert resp2.status_code == 200, resp2.text
        assert resp1.json()["job_id"] == resp2.json()["job_id"], (
            "Duplicate POST must return the same job_id (idempotency)"
        )

    def test_slug_taken_race_condition_returns_409(self, client: TestClient) -> None:
        """HIGH #4: UNIQUE constraint violation (concurrent race) returns 409 SLUG_TAKEN."""
        from sqlalchemy.exc import IntegrityError

        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
            patch(
                "cq_server.provisioning.routes.get_active_job_for_slug",
                return_value=None,  # simulate idempotency check missing (race window)
            ),
            patch(
                "cq_server.provisioning.routes.insert_job",
                side_effect=IntegrityError("UNIQUE constraint failed", None, None),
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "SLUG_TAKEN"
        # HIGH #3: slug must NOT appear in the response body.
        assert _VALID_BODY["enterprise_slug"] not in json.dumps(body)

    def test_role_not_assumable_returns_403(self, client: TestClient) -> None:
        with patch(
            "cq_server.provisioning.routes._validate_assume_role",
            side_effect=_mock_assume_role_fail,
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "ROLE_NOT_ASSUMABLE"

    def test_bad_region_returns_422(self, client: TestClient) -> None:
        body = {**_VALID_BODY, "aws_region": "ap-southeast-1"}
        resp = client.post("/api/v1/enterprises", json=body)
        assert resp.status_code == 422, resp.text

    def test_bad_slug_format_returns_422(self, client: TestClient) -> None:
        body = {**_VALID_BODY, "enterprise_slug": "Bad-Slug!"}
        resp = client.post("/api/v1/enterprises", json=body)
        assert resp.status_code == 422, resp.text

    def test_rate_limit_returns_429(self, client: TestClient) -> None:
        # Patch the module-level constant directly (env var is read at import time).
        with (
            patch("cq_server.provisioning.routes._RATE_LIMIT_MAX", 2),
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            for slug in ["acme1", "acme2", "acme3"]:
                body = {**_VALID_BODY, "enterprise_slug": slug}
                resp = client.post("/api/v1/enterprises", json=body)
                if resp.status_code == 429:
                    assert resp.json()["code"] == "RATE_LIMIT"
                    return
        pytest.fail("Expected 429 RATE_LIMIT was not returned within 3 requests")

    def test_cors_preflight_for_signup_origin(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/enterprises",
            headers={
                "Origin": "https://signup.8th-layer.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        # Starlette TestClient follows CORS middleware; preflight returns 200.
        assert resp.status_code == 200
        assert "signup.8th-layer.ai" in resp.headers.get("access-control-allow-origin", "")

    def test_cors_disallowed_for_unknown_origin(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/enterprises",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Unknown origin: CORS header should not reflect the evil origin.
        acao = resp.headers.get("access-control-allow-origin", "")
        assert "evil.example.com" not in acao


class TestGetProvisioningJobRoute:
    def _create_job(self, client: TestClient) -> str:
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        return resp.json()["job_id"]

    def test_unknown_job_id_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/enterprises/jobs/prov_DOESNOTEXIST000000000001")
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "NOT_FOUND"

    def test_known_job_returns_200(self, client: TestClient) -> None:
        job_id = self._create_job(client)
        resp = client.get(f"/api/v1/enterprises/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["status"] == "PROVISIONING"
        assert body["started_at"] is not None

    def test_expired_completed_job_returns_404(self, client: TestClient) -> None:
        from cq_server.app import _get_store

        job_id = self._create_job(client)
        # Directly complete the job and backdate completed_at.
        store = _get_store()
        engine = store._engine  # noqa: SLF001
        old_ts = (datetime.now(UTC) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE provisioning_jobs SET status='COMPLETED', phase=6, "
                    "completed_at=:ts, result_json='{}' WHERE job_id=:jid"
                ),
                {"ts": old_ts, "jid": job_id},
            )
            conn.commit()
        resp = client.get(f"/api/v1/enterprises/jobs/{job_id}")
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "NOT_FOUND"

    def test_fresh_completed_job_returns_result(self, client: TestClient) -> None:
        from cq_server.app import _get_store

        job_id = self._create_job(client)
        store = _get_store()
        engine = store._engine  # noqa: SLF001
        result = {"enterprise_id": "acme", "l2_admin_url": "https://acme.8th-layer.ai"}
        now_ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE provisioning_jobs SET status='COMPLETED', phase=6, "
                    "completed_at=:ts, result_json=:rj WHERE job_id=:jid"
                ),
                {"ts": now_ts, "rj": json.dumps(result), "jid": job_id},
            )
            conn.commit()
        resp = client.get(f"/api/v1/enterprises/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "COMPLETED"
        assert body["result"]["l2_admin_url"] == "https://acme.8th-layer.ai"
        assert body["progress_pct"] == 100


# ---------------------------------------------------------------------------
# Integration smoke tests
# ---------------------------------------------------------------------------


class TestProvisioningJobLifecycleSmoke:
    """Smoke tests against a real in-memory SQLite DB (no AWS)."""

    def test_full_phase_cycle_via_db_helpers(self, db_engine) -> None:
        """Insert → phase 1 → 2 → 3 → complete; verify final row shape."""
        job_id = generate_job_id()
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id=job_id,
                enterprise_id="smoke-co",
                status="PROVISIONING",
                phase=0,
                ip_hash="testhash",
            )
            for phase, status in [
                (1, "KEY_MINT_IN_PROGRESS"),
                (2, "DIRECTORY_REGISTER_IN_PROGRESS"),
                (3, "DNS_PROVISION_IN_PROGRESS"),
                (4, "L2_STANDUP_IN_PROGRESS"),
                (5, "ADMIN_INVITE_SENT"),
            ]:
                update_job_phase(conn, job_id=job_id, status=status, phase=phase)
                row = get_job(conn, job_id)
                assert row["phase"] == phase
                assert row["status"] == status

            complete_job(
                conn,
                job_id=job_id,
                result_json={"l2_admin_url": "https://smoke-co.8th-layer.ai"},
            )
            row = get_job(conn, job_id)

        assert row["status"] == "COMPLETED"
        assert row["phase"] == 6
        assert row["completed_at"] is not None
        assert json.loads(row["result_json"])["l2_admin_url"] == "https://smoke-co.8th-layer.ai"

    def test_fail_at_any_phase_marks_failed(self, db_engine) -> None:
        job_id = generate_job_id()
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id=job_id,
                enterprise_id="fail-co",
                status="PROVISIONING",
                phase=0,
                ip_hash="failhash",
            )
            update_job_phase(conn, job_id=job_id, status="L2_STANDUP_IN_PROGRESS", phase=4)
            fail_job(conn, job_id=job_id, error="CFN stack timeout")
            row = get_job(conn, job_id)

        assert row["status"] == "FAILED"
        assert "CFN" in row["error"]
        assert row["completed_at"] is not None


# ---------------------------------------------------------------------------
# Tests for HIGH security findings
# ---------------------------------------------------------------------------


class TestHigh1ExternalId:
    """HIGH #1 — AssumeRole must require assume_role_external_id."""

    def test_missing_external_id_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in _VALID_BODY.items() if k != "assume_role_external_id"}
        resp = client.post("/api/v1/enterprises", json=body)
        assert resp.status_code == 422, resp.text

    def test_short_external_id_returns_422(self, client: TestClient) -> None:
        body = {**_VALID_BODY, "assume_role_external_id": "short"}
        resp = client.post("/api/v1/enterprises", json=body)
        assert resp.status_code == 422, resp.text

    def test_external_id_forwarded_to_assume_role(self, client: TestClient) -> None:
        """_validate_assume_role is called with the external_id value."""
        captured: dict = {}

        def _capture(role_arn: str, slug: str, external_id: str) -> None:
            captured["external_id"] = external_id

        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_capture,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)

        assert resp.status_code == 200, resp.text
        assert captured.get("external_id") == _VALID_BODY["assume_role_external_id"]


class TestHigh3SlugTakenResponse:
    """HIGH #3 — SLUG_TAKEN must not echo the slug in the error body."""

    def test_slug_taken_response_body_generic(self, client: TestClient) -> None:
        """409 SLUG_TAKEN body must be generic — no slug echoed back."""
        from sqlalchemy.exc import IntegrityError

        # Simulate a concurrent-race 409 by bypassing idempotency check.
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
            patch(
                "cq_server.provisioning.routes.get_active_job_for_slug",
                return_value=None,
            ),
            patch(
                "cq_server.provisioning.routes.insert_job",
                side_effect=IntegrityError("UNIQUE constraint failed", None, None),
            ),
        ):
            resp = client.post("/api/v1/enterprises", json=_VALID_BODY)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "SLUG_TAKEN"
        # HIGH #3: slug must NOT appear in the response body.
        assert _VALID_BODY["enterprise_slug"] not in json.dumps(body)
        # Generic message only.
        assert "not available" in body.get("error", "") or "not available" in body.get("message", "")


class TestHigh5RateLimitTrustedProxy:
    """HIGH #5 — X-Forwarded-For only trusted from known proxy IPs."""

    def test_spoofed_xff_not_trusted_without_config(self, client: TestClient) -> None:
        """Without PROVISIONING_TRUSTED_PROXY_IPS, XFF is ignored."""
        import cq_server.provisioning.routes as routes_mod

        # Temporarily clear trusted proxies.
        original = routes_mod._TRUSTED_PROXY_IPS
        routes_mod._TRUSTED_PROXY_IPS = frozenset()
        try:
            # Spoof an XFF header as if from a trusted proxy.
            with (
                patch("cq_server.provisioning.routes._RATE_LIMIT_MAX", 1),
                patch(
                    "cq_server.provisioning.routes._validate_assume_role",
                    side_effect=_mock_assume_role_ok,
                ),
                patch(
                    "cq_server.provisioning.routes._run_job_background",
                    return_value=None,
                ),
            ):
                resp1 = client.post(
                    "/api/v1/enterprises",
                    json={**_VALID_BODY, "enterprise_slug": "xff-test1"},
                    headers={"X-Forwarded-For": "1.2.3.4"},
                )
                resp2 = client.post(
                    "/api/v1/enterprises",
                    json={**_VALID_BODY, "enterprise_slug": "xff-test2"},
                    headers={"X-Forwarded-For": "9.9.9.9"},  # different spoofed IP
                )
            # Both requests share the same real transport IP → rate limit fires on 2nd.
            assert resp2.status_code == 429, resp2.text
        finally:
            routes_mod._TRUSTED_PROXY_IPS = original


class TestHigh7Idempotency:
    """HIGH #7 — duplicate POST returns existing job, not a new one."""

    def test_duplicate_post_returns_existing_job_id(self, client: TestClient) -> None:
        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_mock_assume_role_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            resp1 = client.post("/api/v1/enterprises", json=_VALID_BODY)
            resp2 = client.post("/api/v1/enterprises", json=_VALID_BODY)

        assert resp1.status_code == 200, resp1.text
        assert resp2.status_code == 200, resp2.text
        assert resp1.json()["job_id"] == resp2.json()["job_id"], (
            "Duplicate POST must return the same job_id, not a new one"
        )

    def test_duplicate_post_does_not_call_validate_twice(self, client: TestClient) -> None:
        """Second POST returns the existing job without re-running AssumeRole validation."""
        call_count = {"n": 0}

        def _counting_ok(*args, **kwargs) -> None:
            call_count["n"] += 1

        with (
            patch(
                "cq_server.provisioning.routes._validate_assume_role",
                side_effect=_counting_ok,
            ),
            patch(
                "cq_server.provisioning.routes._run_job_background",
                return_value=None,
            ),
        ):
            client.post("/api/v1/enterprises", json=_VALID_BODY)
            client.post("/api/v1/enterprises", json=_VALID_BODY)

        # AssumeRole validation only fires once (on the first POST).
        assert call_count["n"] == 1, "AssumeRole should only be called once for idempotent requests"


class TestHigh4UniqueConstraint:
    """HIGH #4 — UNIQUE constraint on enterprise_id in the DB layer."""

    def test_integrity_error_on_duplicate_insert(self, db_engine) -> None:
        """Direct insert of duplicate enterprise_id raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        job_id_1 = generate_job_id()
        job_id_2 = generate_job_id()
        with db_engine.connect() as conn:
            insert_job(
                conn,
                job_id=job_id_1,
                enterprise_id="dup-slug",
                status="PROVISIONING",
                phase=0,
                ip_hash="x",
            )
        with pytest.raises(IntegrityError):
            with db_engine.connect() as conn:
                insert_job(
                    conn,
                    job_id=job_id_2,
                    enterprise_id="dup-slug",
                    status="PROVISIONING",
                    phase=0,
                    ip_hash="x",
                )


class TestHigh8Phase3ErrorPropagation:
    """HIGH #8 — phase 3 errors must propagate, not be swallowed."""

    def test_missing_cf_token_raises(self) -> None:
        """_phase3_dns_provision raises RuntimeError when CF_API_TOKEN is absent."""
        import os

        from cq_server.provisioning.worker import _phase3_dns_provision

        env_backup = os.environ.pop("CF_API_TOKEN", None)
        try:
            with pytest.raises(RuntimeError, match="CF_API_TOKEN"):
                _phase3_dns_provision("test-slug")
        finally:
            if env_backup is not None:
                os.environ["CF_API_TOKEN"] = env_backup

    def test_cloudflare_http_error_raises(self) -> None:
        """_phase3_dns_provision raises RuntimeError on Cloudflare HTTP error."""
        import os
        import urllib.error

        from cq_server.provisioning.worker import _phase3_dns_provision

        os.environ["CF_API_TOKEN"] = "fake-token"
        try:
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="https://api.cloudflare.com/...",
                    code=400,
                    msg="Bad Request",
                    hdrs=None,  # type: ignore[arg-type]
                    fp=None,  # type: ignore[arg-type]
                ),
            ):
                with pytest.raises(RuntimeError, match="Cloudflare"):
                    _phase3_dns_provision("test-slug")
        finally:
            os.environ.pop("CF_API_TOKEN", None)


class TestCfUpsertCnameBranches:
    """8l-reviewer 3rd-pass follow-up: direct branch coverage of
    `_cf_upsert_cname`. The recovery path re-runs phase 1 from scratch so
    each branch (no-op / PATCH / POST) MUST be exercised.
    """

    def _fake_urlopen_responses(self, responses: list):
        """Return a context manager that returns successive responses on each
        urlopen call. Each response item is either a dict (json body, status
        200) or an Exception to raise.
        """
        from contextlib import contextmanager
        from io import BytesIO
        import json
        from unittest.mock import MagicMock

        responses_iter = iter(responses)

        @contextmanager
        def _open(req, timeout=None):
            r = next(responses_iter)
            if isinstance(r, BaseException):
                raise r
            mock = MagicMock()
            mock.read.return_value = json.dumps(r).encode()
            yield mock

        return _open

    def test_noop_when_existing_cname_matches(self) -> None:
        """GET returns 1 record with matching content → no POST/PATCH."""
        from unittest.mock import patch

        from cq_server.provisioning.worker import _cf_upsert_cname

        fake = self._fake_urlopen_responses(
            [
                {
                    "success": True,
                    "result": [
                        {
                            "id": "rec123",
                            "content": "provision.8th-layer.ai",
                        }
                    ],
                },
            ]
        )
        with patch("urllib.request.urlopen", side_effect=fake):
            _cf_upsert_cname(
                cf_token="t",
                cf_zone_id="z",
                fqdn="acme.8th-layer.ai",
                target="provision.8th-layer.ai",
                enterprise_slug="acme",
            )
        # No exception, no second urlopen call — branch verified by call count.

    def test_patch_when_existing_cname_drifts(self) -> None:
        """GET returns 1 record with different content → PATCH the drift."""
        from unittest.mock import patch

        from cq_server.provisioning.worker import _cf_upsert_cname

        fake = self._fake_urlopen_responses(
            [
                {
                    "success": True,
                    "result": [
                        {
                            "id": "rec123",
                            "content": "old.target.example.com",
                        }
                    ],
                },
                {"success": True, "result": {"id": "rec123"}},  # PATCH ok
            ]
        )
        with patch("urllib.request.urlopen", side_effect=fake):
            _cf_upsert_cname(
                cf_token="t",
                cf_zone_id="z",
                fqdn="acme.8th-layer.ai",
                target="provision.8th-layer.ai",
                enterprise_slug="acme",
            )

    def test_post_when_no_existing_cname(self) -> None:
        """GET returns 0 records → POST a fresh CNAME."""
        from unittest.mock import patch

        from cq_server.provisioning.worker import _cf_upsert_cname

        fake = self._fake_urlopen_responses(
            [
                {"success": True, "result": []},  # GET empty
                {"success": True, "result": {"id": "rec123"}},  # POST ok
            ]
        )
        with patch("urllib.request.urlopen", side_effect=fake):
            _cf_upsert_cname(
                cf_token="t",
                cf_zone_id="z",
                fqdn="acme.8th-layer.ai",
                target="provision.8th-layer.ai",
                enterprise_slug="acme",
            )

    def test_list_error_propagates(self) -> None:
        """Non-success response on the GET → RuntimeError raised."""
        from unittest.mock import patch

        from cq_server.provisioning.worker import _cf_upsert_cname

        fake = self._fake_urlopen_responses(
            [
                {"success": False, "errors": [{"message": "auth failed"}]},
            ]
        )
        with patch("urllib.request.urlopen", side_effect=fake):
            with pytest.raises(RuntimeError, match="list returned errors"):
                _cf_upsert_cname(
                    cf_token="t",
                    cf_zone_id="z",
                    fqdn="acme.8th-layer.ai",
                    target="provision.8th-layer.ai",
                    enterprise_slug="acme",
                )
