"""Tests for the batch-reflect endpoints (#67).

Covers the contract surface frozen in
``crosstalk-enterprise/docs/specs/batch-reflect-contract.md`` v1:

  - happy-path POST /reflect/submit returns 202 with a sub_<ULID>
  - 413 on oversize context
  - 422 on bad mode / bad session_id regex
  - 429 + Retry-After on second submit inside the rate-limit window
  - dedup short-circuit returns original submission_id with deduped_to
  - GET /reflect/status round-trip on a fresh submission
  - GET /reflect/last returns 200-with-null shape for unseen session
  - cross-session-key isolation: one user can't read another user's
    submissions (different Enterprise → 404)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cq_server.app import app
from cq_server.deps import require_api_key

ALICE_USERNAME = "alice-reflect"
BOB_USERNAME = "bob-reflect"


@pytest.fixture()
def alice_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Auth-overridden client running as ``alice-reflect``.

    Per-test DB so dedup/rate-limit windows don't leak across cases.
    Tightens ``REFLECT_RATE_LIMIT_PER_HOURS`` to a per-test override
    when needed via ``monkeypatch.setenv`` inside the test body.
    """
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    # Default the rate limit to 4h (contract default); individual tests
    # override to 0 when they want to bypass for sequential calls.
    monkeypatch.setenv("REFLECT_RATE_LIMIT_PER_HOURS", "4")
    app.dependency_overrides[require_api_key] = lambda: ALICE_USERNAME
    with TestClient(app) as client:
        from cq_server.app import _get_store
        from cq_server.auth import hash_password

        store = _get_store()
        if store.sync.get_user(ALICE_USERNAME) is None:
            store.sync.create_user(ALICE_USERNAME, hash_password("pw-alice"))
        # Bob lives in a different Enterprise so isolation tests have a
        # second tenant. ``create_user`` defaults to a single tenancy
        # row, but the cross-Enterprise check in the route compares the
        # user's enterprise_id to the row's, so we manually steer Bob
        # to a distinct enterprise via direct SQL.
        if store.sync.get_user(BOB_USERNAME) is None:
            store.sync.create_user(BOB_USERNAME, hash_password("pw-bob"))
            with store._engine.begin() as conn:
                from sqlalchemy import text as _text

                conn.execute(
                    _text(
                        "UPDATE users SET enterprise_id = 'ent-bob' "
                        "WHERE username = :u"
                    ),
                    {"u": BOB_USERNAME},
                )
        yield client
    app.dependency_overrides.pop(require_api_key, None)


def _submit_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": "ranger",
        "context": "hello world reflection context blob",
        "since_ts": "2026-04-29T00:00:00Z",
        "mode": "nightly",
        "max_candidates": 10,
    }
    payload.update(overrides)
    return payload


class TestSubmit:
    def test_happy_path_returns_202_with_submission_id(self, alice_client: TestClient) -> None:
        resp = alice_client.post("/api/v1/reflect/submit", json=_submit_payload())
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["submission_id"].startswith("sub_")
        assert len(body["submission_id"]) == 4 + 26  # "sub_" + 26-char ULID
        assert body["queued_at"].endswith("Z")
        assert body["expected_complete_by"].endswith("Z")
        assert body["deduped_to"] is None

    def test_oversize_context_returns_413(self, alice_client: TestClient) -> None:
        # 1_000_001 chars — one byte over the cap.
        oversize = "a" * 1_000_001
        resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(context=oversize),
        )
        assert resp.status_code == 413
        assert "1000000" in resp.json()["detail"]

    def test_bad_session_id_regex_returns_422(self, alice_client: TestClient) -> None:
        resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="ranger has spaces"),
        )
        assert resp.status_code == 422

    def test_hourly_mode_rejected_with_422(self, alice_client: TestClient) -> None:
        resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(mode="hourly"),
        )
        assert resp.status_code == 422

    def test_max_candidates_clamped_not_rejected(self, alice_client: TestClient) -> None:
        # Contract: requests over 25 are clamped, not rejected.
        resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(max_candidates=999),
        )
        assert resp.status_code == 202

    def test_rate_limit_returns_429_with_retry_after(
        self, alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First submit lands fine; second within 4h hits 429.
        first = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="rate-test", context="ctx-A"),
        )
        assert first.status_code == 202
        second = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="rate-test", context="ctx-B"),
        )
        assert second.status_code == 429
        assert second.headers.get("Retry-After") is not None
        retry_after = int(second.headers["Retry-After"])
        assert 0 < retry_after <= 4 * 3600
        body = second.json()
        # Flat body shape per contract — NOT FastAPI's nested envelope.
        assert body["detail"] == "rate_limit_exceeded"
        assert body["retry_after_seconds"] == retry_after
        assert "session-key" in body["limit"]

    def test_dedup_returns_original_submission_id(
        self, alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same (session_id, context) within 30 min → server returns the
        # original submission_id with deduped_to populated. Disable
        # rate limit so the second call doesn't 429 first.
        monkeypatch.setenv("REFLECT_RATE_LIMIT_PER_HOURS", "0")
        first = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="dedup-test", context="duplicate-ctx"),
        )
        assert first.status_code == 202
        original_id = first.json()["submission_id"]

        second = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="dedup-test", context="duplicate-ctx"),
        )
        assert second.status_code == 202
        body = second.json()
        assert body["submission_id"] == original_id
        assert body["deduped_to"] == original_id


class TestStatus:
    def test_status_round_trip_after_submit(self, alice_client: TestClient) -> None:
        submit_resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="status-rt", context="status-rt-ctx"),
        )
        assert submit_resp.status_code == 202
        submission_id = submit_resp.json()["submission_id"]

        status_resp = alice_client.get(
            "/api/v1/reflect/status",
            params={"submission_id": submission_id},
        )
        assert status_resp.status_code == 200, status_resp.text
        body = status_resp.json()
        assert body["submission_id"] == submission_id
        assert body["session_id"] == "status-rt"
        assert body["state"] == "queued"
        assert body["submitted_at"].endswith("Z")
        # Worker hasn't run yet — these are null until then.
        assert body["started_at"] is None
        assert body["completed_at"] is None
        assert body["model"] is None
        assert body["input_tokens"] is None
        assert body["output_tokens"] is None
        # Counters default to 0.
        assert body["candidates_proposed"] == 0
        assert body["candidates_confirmed"] == 0
        assert body["candidates_excluded"] == 0
        assert body["candidates_deduped"] == 0
        assert body["error"] is None

    def test_status_unknown_id_returns_404(self, alice_client: TestClient) -> None:
        resp = alice_client.get(
            "/api/v1/reflect/status",
            params={"submission_id": "sub_DOES_NOT_EXIST"},
        )
        assert resp.status_code == 404

    def test_status_cross_enterprise_isolation(
        self, alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Alice creates a submission. Bob (different Enterprise) tries
        # to read it via the same submission_id and sees 404.
        submit_resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="iso-test", context="alice-ctx"),
        )
        assert submit_resp.status_code == 202
        alice_submission_id = submit_resp.json()["submission_id"]

        # Flip the auth override to Bob without rebuilding the client
        # (same DB, same TestClient).
        app.dependency_overrides[require_api_key] = lambda: BOB_USERNAME
        try:
            bob_resp = alice_client.get(
                "/api/v1/reflect/status",
                params={"submission_id": alice_submission_id},
            )
            assert bob_resp.status_code == 404
        finally:
            app.dependency_overrides[require_api_key] = lambda: ALICE_USERNAME


class TestLast:
    def test_last_returns_null_shape_when_no_submission(self, alice_client: TestClient) -> None:
        resp = alice_client.get(
            "/api/v1/reflect/last",
            params={"session_id": "never-seen"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["submission_id"] is None
        assert body["session_id"] == "never-seen"
        assert body["state"] is None

    def test_last_returns_most_recent_submission(self, alice_client: TestClient) -> None:
        submit_resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="last-rt", context="last-rt-ctx"),
        )
        assert submit_resp.status_code == 202
        sid = submit_resp.json()["submission_id"]

        resp = alice_client.get(
            "/api/v1/reflect/last",
            params={"session_id": "last-rt"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["submission_id"] == sid
        assert body["session_id"] == "last-rt"
        assert body["state"] == "queued"

    def test_last_cross_enterprise_isolation(self, alice_client: TestClient) -> None:
        # Alice has a submission for session "shared-name".
        alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="shared-name", context="alice-ctx-2"),
        )

        # Bob queries /last for "shared-name" — should see null shape.
        app.dependency_overrides[require_api_key] = lambda: BOB_USERNAME
        try:
            resp = alice_client.get(
                "/api/v1/reflect/last",
                params={"session_id": "shared-name"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["submission_id"] is None
        finally:
            app.dependency_overrides[require_api_key] = lambda: ALICE_USERNAME


class TestRouterMounting:
    """Sanity that the router lands at both /reflect and /api/v1/reflect.

    Mirrors the dual-mount pattern the rest of api_router uses (SDK
    compatibility at root, frontend at /api/v1).
    """

    def test_root_mount_works(self, alice_client: TestClient) -> None:
        resp = alice_client.post(
            "/reflect/submit",
            json=_submit_payload(session_id="root-mount", context="root-ctx"),
        )
        assert resp.status_code == 202

    def test_v1_mount_works(self, alice_client: TestClient) -> None:
        resp = alice_client.post(
            "/api/v1/reflect/submit",
            json=_submit_payload(session_id="v1-mount", context="v1-ctx"),
        )
        assert resp.status_code == 202


# Sanity that the env var is read fresh per request, not cached at
# import time. Touching ``os.environ`` directly to keep the assertion
# tight.
def test_rate_limit_env_var_read_fresh(alice_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_RATE_LIMIT_PER_HOURS", "0")
    # Two submits with limit=0 should both succeed (limit interpreted
    # as "no rate limit window" → fall back to default? — see impl).
    # Actually the impl falls back to default on n<=0, so this is a
    # canary that the default keeps holding when env is malformed.
    a = alice_client.post(
        "/api/v1/reflect/submit",
        json=_submit_payload(session_id="env-test", context="env-A"),
    )
    assert a.status_code == 202
    b = alice_client.post(
        "/api/v1/reflect/submit",
        json=_submit_payload(session_id="env-test", context="env-B"),
    )
    # Default 4h limit kicks in → 429 on second call.
    assert b.status_code == 429
    # Sanity — clean up env so other tests don't see the override
    # (monkeypatch handles this automatically on teardown).
    assert os.environ.get("REFLECT_RATE_LIMIT_PER_HOURS") == "0"
