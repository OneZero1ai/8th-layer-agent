"""HTTP-level tests for FO-3 Phase 2 — the cq-server L2-provision proxy.

Coverage (task brief, agent#193 / Decision 32):

1. Proxy auth — POST /admin/l2s requires an admin; a non-admin 403s.
2. Tenancy — the L2 is created in the *caller's* Enterprise; the browser
   never sends enterprise_id, and the forward URL carries the admin's own
   enterprise_id resolved from their user row.
3. Forward call — the create proxy relays the provisioning service's
   {job_id, l2_id, status, poll_url} and augments it with stream_url.
4. Directory #22 auth contract — the POST body is an Enterprise-root-signed
   SignedEnvelope (not a plain dict); the SSE poll loop sends a freshly
   signed X-8L-Identity-Proof header. Both signatures verify.
5. Upstream-error passthrough — a {error, code, detail} envelope from the
   provisioning service is relayed verbatim with its status.
6. Transport failure — an unreachable provisioning service maps to a
   502 PROVISIONING_UNREACHABLE envelope; a missing identity key → 500.
7. SSE generation — the _job_event_stream async generator emits phase
   events on transitions, a terminal completed/failed event carrying the
   job result, and survives transient poll failures without crashing.

The provisioning service is faked with ``httpx.MockTransport`` injected
into ``httpx.AsyncClient`` via monkeypatch — no network, no live service.
The Enterprise root key is a per-test tmp-file (32 raw Ed25519 bytes), the
same on-disk shape the directory client's /announce signing uses.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app
from cq_server.crypto import load_private_key, public_key_b64u, verify_envelope_signature
from cq_server.l2_provision_routes import _job_event_stream

ENT_A = "acme"
ENT_B = "globex"
ADMIN_A = "admin@acme"
USER_A = "user@acme"

PROVISIONING_BASE = "https://provision.test.invalid"


# ---------------------------------------------------------------------------
# SignedEnvelope assertion helpers (directory #22 auth contract)
# ---------------------------------------------------------------------------


def _decode_proof_header(value: str) -> dict:
    """Decode an X-8L-Identity-Proof header back to its SignedEnvelope.

    The proxy sends ``base64url(canonicalize(envelope))`` — unpadded
    base64url of RFC 8785 JCS bytes. Re-pad and parse.
    """
    pad = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + pad))


def _assert_signed_envelope(envelope: dict, *, expected_enterprise_id: str) -> dict:
    """Assert a SignedEnvelope is well-formed + its Ed25519 signature verifies.

    Returns the inner payload for further field assertions.
    """
    for field in ("payload", "payload_canonical", "signature", "signing_key_id"):
        assert field in envelope, f"envelope missing {field!r}"
    assert verify_envelope_signature(
        envelope["signing_key_id"],
        envelope["payload_canonical"],
        envelope["signature"],
    ), "envelope signature did not verify"
    payload = envelope["payload"]
    assert payload["enterprise_id"] == expected_enterprise_id
    assert payload.get("ts"), "envelope payload missing ts"
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def root_key_path(tmp_path: Path) -> Path:
    """Write an Enterprise root Ed25519 key as 32 raw bytes (load_private_key shape)."""
    key = Ed25519PrivateKey.generate()
    p = tmp_path / "enterprise-root.key"
    p.write_bytes(key.private_bytes_raw())
    return p


@pytest.fixture
def client(
    tmp_path: Path, root_key_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A TestClient with one Enterprise-A admin and one Enterprise-A plain user."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "l2prov.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_PROVISIONING_API_URL", PROVISIONING_BASE)
    monkeypatch.setenv("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", str(root_key_path))
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
    on the forward URL / method / body / headers the proxy produced.
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


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Inject a MockTransport into every httpx.AsyncClient (no Request capture)."""
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


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

        # Tenancy: the forward URL carries the caller's OWN enterprise_id.
        assert len(seen) == 1
        fwd = seen[0]
        assert fwd.method == "POST"
        assert str(fwd.url) == f"{PROVISIONING_BASE}/api/v1/enterprises/{ENT_A}/l2s"

    def test_post_body_is_a_signed_envelope(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directory #22 auth contract — the proxy actually signs the body.

        The forward body must be a SignedEnvelope (not the plain wizard
        dict): {payload, payload_canonical, signature, signing_key_id}, the
        Ed25519 signature verifies, the inner payload carries the wizard
        fields + the resolved enterprise_id + a fresh ts.
        """

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "job_id": "job-signed",
                    "l2_id": f"{ENT_A}/sales",
                    "status": "PROVISIONING",
                    "poll_url": "/p",
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

        envelope = json.loads(seen[0].content)
        payload = _assert_signed_envelope(envelope, expected_enterprise_id=ENT_A)
        # The wizard fields are inside the signed payload.
        assert payload["l2_slug"] == "sales"
        assert payload["description"] == "sales team L2"
        assert payload["aws_region"] == "us-east-1"
        # The signing key is the Enterprise root key — same key the directory
        # verifies /announce with.
        root = load_private_key(Path(os.environ["CQ_ENTERPRISE_ROOT_PRIVKEY_PATH"]))
        assert envelope["signing_key_id"] == public_key_b64u(root)

    def test_client_supplied_enterprise_id_does_not_retarget(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser smuggling enterprise_id cannot retarget the L2.

        The proxy resolves enterprise_id from the admin's user row and signs
        THAT into the envelope payload — the directory checks the signed
        payload's enterprise_id equals the path, so a client-side value is
        both ignored and cryptographically irrelevant.
        """

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={"job_id": "job-x", "l2_id": f"{ENT_A}/sales", "status": "PROVISIONING", "poll_url": "/p"},
            )

        seen = _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={
                "l2_slug": "sales",
                "description": "sales team L2",
                "aws_region": "us-east-1",
                "enterprise_id": ENT_B,  # smuggled — pydantic ignores it
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202
        # Forward URL and the signed payload both carry the caller's own
        # Enterprise, not globex.
        assert str(seen[0].url) == f"{PROVISIONING_BASE}/api/v1/enterprises/{ENT_A}/l2s"
        payload = _assert_signed_envelope(json.loads(seen[0].content), expected_enterprise_id=ENT_A)
        assert payload["enterprise_id"] == ENT_A

    def test_missing_identity_key_is_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No Enterprise root key mount → 500 IDENTITY_KEY_UNAVAILABLE, no forward."""
        monkeypatch.delenv("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", raising=False)
        seen = _mock_provisioning(monkeypatch, lambda r: httpx.Response(202, json={}))
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 500
        assert resp.json()["code"] == "IDENTITY_KEY_UNAVAILABLE"
        # The proxy must NOT have forwarded an unsigned request.
        assert seen == []

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

    def test_upstream_403_forbidden_passed_through(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory 403 FORBIDDEN (wrong Enterprise) relays verbatim."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"error": "enterprise mismatch", "code": "FORBIDDEN", "detail": ""},
            )

        _mock_provisioning(monkeypatch, _handler)
        token = _login(client, ADMIN_A)
        resp = client.post(
            "/api/v1/admin/l2s",
            json={"l2_slug": "sales", "description": "sales team L2", "aws_region": "us-east-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"

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

    def test_stream_missing_identity_key_is_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", raising=False)
        token = _login(client, USER_A)
        resp = client.get(
            "/api/v1/admin/l2s/jobs/job-1/stream",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 500
        assert resp.json()["code"] == "IDENTITY_KEY_UNAVAILABLE"

    def test_stream_open_to_authenticated_non_admin_and_sends_identity_proof(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain user may stream; each poll carries a verified identity proof."""
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
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

        _patch_transport(monkeypatch, _handler)
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

        # Each directory poll carried a signed X-8L-Identity-Proof header.
        assert seen, "no poll requests captured"
        for req in seen:
            proof = req.headers.get("X-8L-Identity-Proof")
            assert proof, "poll request missing X-8L-Identity-Proof header"
            envelope = _decode_proof_header(proof)
            _assert_signed_envelope(envelope, expected_enterprise_id=ENT_A)


# ---------------------------------------------------------------------------
# SSE event-stream generator — unit-level
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    """Async-callable that returns a scripted sequence of job-poll responses."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._i = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        row = self._rows[min(self._i, len(self._rows) - 1)]
        self._i += 1
        if "_raise" in row:
            raise httpx.ConnectError("boom", request=request)
        status = row.pop("_http_status", 200)
        return httpx.Response(status, json=row)


async def _collect(gen) -> list[str]:  # type: ignore[no-untyped-def]
    out: list[str] = []
    async for frame in gen:
        out.append(frame)
    return out


@pytest.fixture
def stream_key() -> Ed25519PrivateKey:
    """A standalone Enterprise root key for the unit-level stream tests."""
    return Ed25519PrivateKey.generate()


def _install_scripted(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> _ScriptedTransport:
    scripted = _ScriptedTransport(rows)
    transport = httpx.MockTransport(scripted)
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    monkeypatch.setattr("cq_server.l2_provision_routes._POLL_INTERVAL_SEC", 0.0)
    return scripted


async def test_stream_emits_phase_events_and_terminal_completed(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
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
    scripted = _install_scripted(monkeypatch, rows)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))
    blob = "".join(frames)
    assert "event: open" in blob
    assert blob.count("event: phase") == 3
    assert "event: completed" in blob
    completed = [f for f in frames if "event: completed" in f][0]
    payload = json.loads(completed.split("data: ", 1)[1].strip())
    assert payload["result"]["admin_api_key"] == "cqa.v1.reveal"

    # Every poll sent a verifiable identity proof signed by the root key.
    assert scripted.requests
    for req in scripted.requests:
        proof = req.headers.get("X-8L-Identity-Proof")
        assert proof, "poll missing identity proof header"
        _assert_signed_envelope(_decode_proof_header(proof), expected_enterprise_id=ENT_A)


async def test_stream_identity_proofs_are_freshly_signed_per_poll(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
) -> None:
    """Each poll's proof has a distinct ts — proofs are single-use (replay-safe)."""
    rows = [
        {"job_id": "j", "status": "PROVISIONING", "phase": 1},
        {"job_id": "j", "status": "PROVISIONING", "phase": 2},
        {"job_id": "j", "status": "COMPLETED", "phase": 3, "result": {}},
    ]
    scripted = _install_scripted(monkeypatch, rows)
    await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))

    canonicals = []
    for req in scripted.requests:
        envelope = _decode_proof_header(req.headers["X-8L-Identity-Proof"])
        canonicals.append(envelope["payload_canonical"])
    # Distinct canonical payloads ⇒ distinct ts ⇒ distinct signatures.
    assert len(set(canonicals)) == len(canonicals), "identity proofs were reused across polls"


async def test_stream_terminal_failed_carries_error(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
) -> None:
    rows = [{"job_id": "j", "status": "FAILED", "phase": 2, "error": "phase 2: CFN rollback"}]
    _install_scripted(monkeypatch, rows)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))
    blob = "".join(frames)
    assert "event: failed" in blob
    failed = [f for f in frames if "event: failed" in f][0]
    payload = json.loads(failed.split("data: ", 1)[1].strip())
    assert payload["error"] == "phase 2: CFN rollback"


async def test_stream_survives_transient_poll_failure(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
) -> None:
    """A transient poll error emits a heartbeat and the stream keeps going."""
    rows = [
        {"_raise": True},  # transport error — must NOT crash the stream
        {"job_id": "j", "status": "COMPLETED", "phase": 3, "result": {}},
    ]
    _install_scripted(monkeypatch, rows)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))
    blob = "".join(frames)
    assert "event: heartbeat" in blob
    assert "event: completed" in blob


async def test_stream_404_job_closes_as_failed(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
) -> None:
    """A 404 from the directory poll route is terminal — stream closes failed."""
    rows = [{"_http_status": 404, "error": "not found", "code": "NOT_FOUND", "detail": ""}]
    _install_scripted(monkeypatch, rows)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))
    assert "event: failed" in "".join(frames)


async def test_stream_auth_rejected_closes_as_failed(
    monkeypatch: pytest.MonkeyPatch, stream_key: Ed25519PrivateKey
) -> None:
    """A 401/403 on the poll (rejected identity proof) ends the stream failed.

    An auth rejection will not self-heal by retrying — the stream closes
    rather than spinning a doomed poll loop.
    """
    rows = [{"_http_status": 403, "error": "wrong enterprise", "code": "FORBIDDEN", "detail": ""}]
    _install_scripted(monkeypatch, rows)

    frames = await _collect(_job_event_stream(PROVISIONING_BASE, ENT_A, "j", stream_key))
    blob = "".join(frames)
    assert "event: failed" in blob
    failed = [f for f in frames if "event: failed" in f][0]
    payload = json.loads(failed.split("data: ", 1)[1].strip())
    assert "identity proof" in payload["error"]
