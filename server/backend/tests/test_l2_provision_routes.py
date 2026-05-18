"""HTTP-level tests for FO-3 Phase 2 — the cq-server L2-provision proxy.

Coverage (task brief, agent#193 / Decision 32):

1. Proxy auth — POST /admin/l2s requires an admin; a non-admin 403s.
2. Tenancy — the L2 is created in the *caller's* Enterprise; the browser
   never sends enterprise_id, and the forward URL carries the admin's own
   enterprise_id resolved from their user row.
3. Forward call — the create proxy relays the provisioning service's
   {job_id, l2_id, status, poll_url} and augments it with stream_url.
4. Upstream-error passthrough — a {error, code, detail} envelope from the
   provisioning service is relayed verbatim with its status.
5. Transport failure — an unreachable provisioning service maps to a
   502 PROVISIONING_UNREACHABLE envelope.
6. SSE generation — the _job_event_stream async generator emits phase
   events on transitions, a terminal completed/failed event carrying the
   job result, and survives transient poll failures without crashing.

The provisioning service is faked with ``httpx.MockTransport`` injected
into ``httpx.AsyncClient`` via monkeypatch — no network, no live service.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app
from cq_server.l2_provision_routes import _job_event_stream

ENT_A = "acme"
ENT_B = "globex"
ADMIN_A = "admin@acme"
USER_A = "user@acme"

PROVISIONING_BASE = "https://provision.test.invalid"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient with one Enterprise-A admin and one Enterprise-A plain user."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "l2prov.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PROVISIONING_API_URL", PROVISIONING_BASE)
    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ADMIN_A, pw)
        store.sync.create_user(USER_A, pw)
        store.sync.set_user_role(ADMIN_A, "enterprise_admin")
        store.sync.set_user_role(USER_A, "user")
        # Tenancy is not exposed via the public store API — set it directly.
        with store._engine.begin() as conn:  # noqa: SLF001
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": "engineering", "u": ADMIN_A},
            )
            conn.execute(
                text("UPDATE users SET enterprise_id = :e, group_id = :g WHERE username = :u"),
                {"e": ENT_A, "g": "engineering", "u": USER_A},
            )
        yield c


def _login(client: TestClient, username: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "pw"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _mock_provisioning(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Patch httpx.AsyncClient so every outbound call hits ``handler``.

    Returns a list that accumulates every Request seen, so tests can assert
    on the forward URL / method / body the proxy produced.
    """
    seen: list[httpx.Request] = []

    def _dispatch(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_dispatch)
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


# ---------------------------------------------------------------------------
# POST /api/v1/admin/l2s — auth + tenancy + forward
# ---------------------------------------------------------------------------


class TestCreateL2Auth:
    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
        )
        assert resp.status_code == 401

    def test_non_admin_forbidden(self, client: TestClient) -> None:
        token = _login(client, USER_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestCreateL2Forward:
    def test_forwards_to_callers_enterprise_and_augments_stream_url(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "job_id": "job-123",
                    "l2_id": f"{ENT_A}/sales",
                    "status": "PROVISIONING",
                    "poll_url": f"/api/v1/enterprises/{ENT_A}/l2s/jobs/job-123",
                },
            )

        seen = _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["job_id"] == "job-123"
        assert body["l2_id"] == f"{ENT_A}/sales"
        assert body["stream_url"] == "/api/v1/admin/l2s/jobs/job-123/stream"

        # Tenancy: the forward URL carries the caller's OWN enterprise_id,
        # and the browser-supplied body had no enterprise_id at all.
        assert len(seen) == 1
        fwd = seen[0]
        assert fwd.method == "POST"
        assert str(fwd.url) == f"{PROVISIONING_BASE}/api/v1/enterprises/{ENT_A}/l2s"
        sent = json.loads(fwd.content)
        assert sent == {
            "l2_slug": "sales",
            "description": "sales team L2",
            "aws_region": "us-east-1",
        }
        assert "enterprise_id" not in sent

    def test_client_supplied_enterprise_id_is_ignored(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser that smuggles enterprise_id cannot retarget the L2."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "job_id": "job-x",
                    "l2_id": f"{ENT_A}/sales",
                    "status": "PROVISIONING",
                    "poll_url": "/p",
                },
            )

        seen = _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            # Extra field — pydantic ignores it; the proxy never reads it.
            json={
                "l2_slug": "sales",
                "description": "sales team L2",
                "aws_region": "us-east-1",
                "enterprise_id": ENT_B,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202
        # The forward still targets the caller's Enterprise, not globex.
        assert str(seen[0].url) == f"{PROVISIONING_BASE}/api/v1/enterprises/{ENT_A}/l2s"

    def test_upstream_error_envelope_passed_through(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={
                    "error": "That L2 slug is already in use within this Enterprise.",
                    "code": "L2_SLUG_TAKEN",
                    "detail": "",
                },
            )

        _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "taken", "description": "dup L2 slug", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "L2_SLUG_TAKEN"

    def test_transport_failure_maps_to_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "PROVISIONING_UNREACHABLE"

    def test_missing_job_id_in_upstream_response_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={"status": "PROVISIONING"})

        _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "PROVISIONING_ERROR"


# ---------------------------------------------------------------------------
# GET /api/v1/admin/l2s/jobs/{job_id}/stream — auth
# ---------------------------------------------------------------------------


class TestStreamAuth:
    def test_stream_requires_authentication(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/l2s/jobs/job-1/stream")
        assert resp.status_code == 401

    def test_stream_open_to_authenticated_non_admin(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain (non-admin) user may stream — the SSE route is not admin-gated."""
        # Job poll returns COMPLETED immediately so the stream closes fast.
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "enterprise_id": ENT_A,
                    "status": "COMPLETED",
                    "phase": 3,
                    "result": {"admin_api_key": "cqa.v1.secret"},
                },
            )

        _mock_provisioning(monkeypatch, _handler)
        token = _login(client, USER_A)
        with client.stream(
            "GET",
            "/api/v1/admin/l2s/jobs/job-1/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())
        assert "event: open" in body
        assert "event: completed" in body
        assert "cqa.v1.secret" in body


# ---------------------------------------------------------------------------
# SSE event-stream generator — unit-level, no HTTP
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    """Async-callable that returns a scripted sequence of job-poll responses."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._i = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        row = self._rows[min(self._i, len(self._rows) - 1)]
        self._i += 1
        if "_raise" in row:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=row)


async def _collect(gen) -> list[str]:  # type: ignore[no-untyped-def]
    out: list[str] = []
    async for frame in gen:
        out.append(frame)
    return out


async def test_stream_emits_phase_events_and_terminal_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase transitions each yield one event; COMPLETED carries the result."""
    rows = [
        {"job_id": "j", "status": "PROVISIONING", "phase": 1, "phase_label": "DNS", "progress_pct": 25},
        {"job_id": "j", "status": "PROVISIONING", "phase": 2, "phase_label": "L2", "progress_pct": 75},
        {
            "job_id": "j",
            "status": "COMPLETED",
            "phase": 3,
            "result": {"l2_id": "acme/sales", "admin_api_key": "cqa.v1.reveal"},
        },
    ]
    transport = httpx.MockTransport(_ScriptedTransport(rows))
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    monkeypatch.setattr("cq_server.l2_provision_routes._POLL_INTERVAL_SEC", 0.0)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j"))
    blob = "".join(frames)
    assert "event: open" in blob
    # One phase event per distinct (status, phase) transition.
    assert blob.count("event: phase") == 3
    assert "event: completed" in blob
    # The terminal frame carries the job result incl. the admin key.
    completed = [f for f in frames if "event: completed" in f][0]
    payload = json.loads(completed.split("data: ", 1)[1].strip())
    assert payload["result"]["admin_api_key"] == "cqa.v1.reveal"


async def test_stream_terminal_failed_carries_error(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"job_id": "j", "status": "FAILED", "phase": 2, "error": "phase 2: CFN rollback"}]
    transport = httpx.MockTransport(_ScriptedTransport(rows))
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    monkeypatch.setattr("cq_server.l2_provision_routes._POLL_INTERVAL_SEC", 0.0)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j"))
    blob = "".join(frames)
    assert "event: failed" in blob
    failed = [f for f in frames if "event: failed" in f][0]
    payload = json.loads(failed.split("data: ", 1)[1].strip())
    assert payload["error"] == "phase 2: CFN rollback"


async def test_stream_survives_transient_poll_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient poll error emits a heartbeat and the stream keeps going."""
    rows = [
        {"_raise": True},  # transport error — must NOT crash the stream
        {"job_id": "j", "status": "COMPLETED", "phase": 3, "result": {}},
    ]
    transport = httpx.MockTransport(_ScriptedTransport(rows))
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    monkeypatch.setattr("cq_server.l2_provision_routes._POLL_INTERVAL_SEC", 0.0)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j"))
    blob = "".join(frames)
    # The transient failure surfaced as a heartbeat, then the job completed.
    assert "event: heartbeat" in blob
    assert "event: completed" in blob


async def test_stream_404_job_closes_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from the directory poll route is terminal — stream closes failed."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found", "code": "NOT_FOUND", "detail": ""})

    transport = httpx.MockTransport(_handler)
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    monkeypatch.setattr("cq_server.l2_provision_routes._POLL_INTERVAL_SEC", 0.0)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j"))
    assert "event: failed" in "".join(frames)
