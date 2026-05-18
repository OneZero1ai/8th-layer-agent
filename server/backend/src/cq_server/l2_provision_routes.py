"""FO-3 Phase 2 — cq-server L2-provision proxy + SSE passthrough (agent#193).

Decision 32 splits the Create-L2 wizard into a central provisioning service
(``provision.8th-layer.ai``, owns the L2-create job runner — directory PR #22)
and a thin per-L2 admin-shell proxy. This module is that proxy. It holds **no
provisioning state**: every request forwards to the directory provisioning
service, and the SSE endpoint server-side-polls the directory's job row.

Endpoints (mounted under the ``/api/v1`` prefix by ``app.py``):

  POST /api/v1/admin/l2s
      ``require_admin``. Takes the wizard's ``{l2_slug, description,
      aws_region}``. The calling admin's ``enterprise_id`` (resolved from
      their user row) is the Enterprise the L2 lands in — the browser never
      sends ``enterprise_id`` or AWS credentials. Forwards to the directory's
      ``POST /api/v1/enterprises/{enterprise_id}/l2s``. Returns the
      directory's ``{job_id, l2_id, status, poll_url}`` plus a ``stream_url``
      pointing at the SSE endpoint below.

  GET /api/v1/admin/l2s/jobs/{job_id}/stream
      Authenticated user (``get_current_user``). Server-Sent-Events stream.
      Server-side-polls the directory's ``GET .../l2s/jobs/{job_id}`` every
      ~3s and emits one ``data:`` event per phase transition (plus periodic
      heartbeats). Closes the stream on ``COMPLETED`` / ``FAILED``. The final
      ``COMPLETED`` event carries the job ``result`` — including the new L2's
      one-time admin API key for the wizard's reveal panel.

Tenancy: the proxy enforces that the L2 is created in the *caller's* own
Enterprise. The directory's PR #22 contract also derives ``admin_email`` and
the AWS binding from the Enterprise's FO-2 record, so no credential material
crosses this boundary.

Contract dependency: this proxy is built against directory PR #22
(``OneZero1ai/8th-layer-directory``), which is under review at time of
writing. If #22's request/response/job-state shapes shift before merge,
this module adjusts to follow — the forward paths and field names here
mirror #22's diff exactly.

Error envelopes mirror Decision 31 / #22: ``{error, code, detail}``. Upstream
provisioning-service errors are surfaced with their original envelope and
status where possible; transport failures map to ``502 PROVISIONING_UNREACHABLE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import get_current_user, require_admin
from .crypto import b64u, canonicalize, load_private_key, sign_envelope
from .deps import get_provisioning_api_url, get_store
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

# Enterprise root Ed25519 private-key path — the SAME key the cq-server
# already uses to sign directory /announce and heartbeat envelopes
# (``directory_client`` reads the identical env var). The directory's FO-3
# routes derive the caller's Enterprise from the verified signature over
# this key and assert it equals the path enterprise_id — closing the
# cross-tenant hole that directory PR #22 originally had.
CQ_ENTERPRISE_ROOT_PRIVKEY_PATH_ENV = "CQ_ENTERPRISE_ROOT_PRIVKEY_PATH"  # pragma: allowlist secret

router = APIRouter(prefix="/admin/l2s", tags=["admin", "provisioning-l2"])

# Server-side poll cadence for the SSE stream. Decision 32 §Transport: the
# cq-server admin backend polls the directory job row every ~3s and emits
# one event per phase transition — the browser holds one persistent
# connection rather than ~100 long-poll round-trips.
_POLL_INTERVAL_SEC = 3.0

# Heartbeat cadence — emit a comment/heartbeat event at least this often so
# intermediaries (and the browser EventSource) keep the connection warm even
# when no phase transition has occurred.
_HEARTBEAT_EVERY_SEC = 15.0

# Hard ceiling on stream lifetime. The FO-3 standup is ~5 min (Decision 32);
# 30 min is generous headroom. Bounds a stream stuck against a directory job
# that never reaches a terminal state.
_STREAM_MAX_SEC = 30 * 60.0

# Outbound HTTP timeout for the per-request forward calls. The directory's
# create + poll handlers return promptly (the job runs async on their side),
# so a short timeout is correct.
_FORWARD_TIMEOUT_SEC = 10.0

# Terminal directory job states — the stream closes when one is observed.
_TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class CreateL2Request(BaseModel):
    """POST /api/v1/admin/l2s body — the wizard's L2-specific fields.

    The browser sends ONLY these three. ``enterprise_id`` and the AWS
    binding are resolved server-side from the calling admin's tenancy and
    the Enterprise's FO-2 record — never accepted from the client.
    """

    l2_slug: str = Field(
        description="L2 slug — unique within the Enterprise. ^[a-z][a-z0-9-]{2,30}$",
    )
    description: str = Field(
        min_length=5,
        max_length=500,
        description="Free-text purpose for the L2 (becomes the directory description).",
    )
    aws_region: str = Field(
        description="AWS region for the new L2. Validated by the directory allowlist.",
    )


class CreateL2ProxyResponse(BaseModel):
    """202 response from POST /api/v1/admin/l2s.

    The directory's ``{job_id, l2_id, status, poll_url}`` plus a
    ``stream_url`` the wizard opens with a browser ``EventSource``.
    """

    job_id: str
    l2_id: str
    status: str
    poll_url: str
    stream_url: str


# ---------------------------------------------------------------------------
# Error helper — mirrors Decision 31 / #22's {error, code, detail} envelope.
# ---------------------------------------------------------------------------


def _error(code: str, error: str, detail: str = "", *, status: int) -> JSONResponse:
    """Build a Decision-31-shaped error envelope as a JSONResponse."""
    return JSONResponse(
        status_code=status,
        content={"error": error, "code": code, "detail": detail},
    )


def _passthrough_upstream_error(resp: httpx.Response) -> JSONResponse:
    """Surface a non-2xx provisioning-service response to the browser.

    The directory already speaks the ``{error, code, detail}`` envelope, so
    when the body parses as that shape we relay it verbatim with the
    original status. A body that is not JSON (a bare ALB 5xx page, say) is
    wrapped in a generic ``PROVISIONING_ERROR`` envelope.
    """
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if isinstance(body, dict) and "code" in body and "error" in body:
        return JSONResponse(status_code=resp.status_code, content=body)
    return _error(
        "PROVISIONING_ERROR",
        "The provisioning service returned an error.",
        f"upstream status {resp.status_code}",
        status=resp.status_code if resp.status_code >= 400 else 502,
    )


# ---------------------------------------------------------------------------
# Tenancy resolution
# ---------------------------------------------------------------------------


async def _resolve_caller_enterprise(store: SqliteStore, username: str) -> str:
    """Return the calling admin's ``enterprise_id`` from their user row.

    FO-3 tenancy rule (Decision 32): the L2 is created in the *caller's own*
    Enterprise. The browser never sends ``enterprise_id`` — it is derived
    here, server-side, so an admin cannot provision an L2 into another
    Enterprise by tampering with the request body.

    Raises a 403-shaped condition (returned by the caller as ``_error``)
    when the user row carries no ``enterprise_id`` — a pre-tenancy/legacy
    fixture admin cannot drive FO-3.
    """
    user = await store.get_user(username)
    if user is None:
        # Should be impossible after require_admin, but defensive.
        raise _TenancyError("user row missing for authenticated admin")
    enterprise_id = user.get("enterprise_id")
    if not enterprise_id:
        raise _TenancyError("caller user row has no enterprise_id; FO-3 L2-provision requires a tenancy-scoped admin")
    return enterprise_id


class _TenancyError(Exception):
    """Internal — raised by _resolve_caller_enterprise, mapped to a 403 envelope."""


class _SigningError(Exception):
    """Internal — raised when the Enterprise root key cannot be loaded/used.

    Mapped by the route to a 500 ``IDENTITY_KEY_UNAVAILABLE`` envelope: the
    proxy cannot present the directory's required credential, so the request
    cannot proceed. This is a server-misconfiguration condition (the
    ``CQ_ENTERPRISE_ROOT_PRIVKEY_PATH`` mount is missing), not a client error.
    """


# ---------------------------------------------------------------------------
# Enterprise-root-key identity proofs (directory FO-3 auth contract)
# ---------------------------------------------------------------------------
#
# The directory's FO-3 routes (8th-layer-directory #22, contract now locked)
# authenticate the caller by an Enterprise-root Ed25519 signature, NOT a
# bearer token: the directory verifies the signature, derives the caller's
# Enterprise from the signing key, and asserts it equals the path
# enterprise_id. This is the same key + JCS-canonicalize + Ed25519-sign
# primitive the cq-server already uses for directory /announce + heartbeat.
#
#   POST .../l2s        — request BODY is a SignedEnvelope; inner payload is
#                         {l2_slug, description, aws_region, enterprise_id, ts}.
#   GET  .../l2s/jobs/* — GET has no body; an X-8L-Identity-Proof header
#                         carries base64url(JSON SignedEnvelope) whose inner
#                         payload is {enterprise_id, ts}.
#
# ``ts`` is RFC3339, regenerated per request — proofs are single-use, the
# directory's replay-skew window is 300s. A stale ts → 400 VALIDATION, which
# the proxy avoids by always signing with a fresh ts immediately before send.


def _rfc3339_now() -> str:
    """Return an RFC3339 UTC timestamp (``...Z``) for an identity-proof ``ts``."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_enterprise_root_key() -> Ed25519PrivateKey:
    """Load the Enterprise root Ed25519 private key from the configured mount.

    Reads ``CQ_ENTERPRISE_ROOT_PRIVKEY_PATH`` — the identical env var the
    directory client uses for /announce signing. Raises :class:`_SigningError`
    when the var is unset or the key file is missing/unreadable; the route
    maps that to a 500 so a misconfigured L2 fails loudly rather than sending
    an unsigned (and therefore directory-rejected) request.
    """
    path = os.environ.get(CQ_ENTERPRISE_ROOT_PRIVKEY_PATH_ENV, "")
    if not path:
        raise _SigningError(
            f"{CQ_ENTERPRISE_ROOT_PRIVKEY_PATH_ENV} is unset — the proxy cannot sign the directory identity proof"
        )
    try:
        return load_private_key(Path(path))
    except (FileNotFoundError, ValueError) as exc:
        raise _SigningError(f"cannot load Enterprise root key from {path}: {exc}") from exc


def _build_create_envelope(
    privkey: Ed25519PrivateKey,
    *,
    enterprise_id: str,
    l2_slug: str,
    description: str,
    aws_region: str,
) -> dict[str, Any]:
    """Build the SignedEnvelope body for ``POST .../enterprises/{id}/l2s``.

    The inner payload carries the wizard fields plus the resolved
    ``enterprise_id`` (the directory checks it equals the path) and a fresh
    ``ts``. :func:`crypto.sign_envelope` produces the exact wire shape the
    directory expects: ``{payload, payload_canonical, signature, signing_key_id}``.
    """
    payload = {
        "l2_slug": l2_slug,
        "description": description,
        "aws_region": aws_region,
        "enterprise_id": enterprise_id,
        "ts": _rfc3339_now(),
    }
    return sign_envelope(privkey, payload)


def _build_identity_proof_header(privkey: Ed25519PrivateKey, *, enterprise_id: str) -> str:
    """Build the ``X-8L-Identity-Proof`` header value for body-less GETs.

    The directory's job-poll route has no request body, so the SignedEnvelope
    travels in a header: ``base64url(JSON SignedEnvelope)`` whose inner
    payload is ``{enterprise_id, ts}``. A fresh ``ts`` per call keeps every
    proof inside the directory's 300s replay window.
    """
    envelope = sign_envelope(privkey, {"enterprise_id": enterprise_id, "ts": _rfc3339_now()})
    return b64u(canonicalize(envelope))


# ---------------------------------------------------------------------------
# POST /api/v1/admin/l2s — create proxy
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateL2ProxyResponse,
    status_code=202,
    summary="Create an additional L2 in the caller's Enterprise (proxy → provisioning service)",
)
async def create_l2(
    body: CreateL2Request,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> CreateL2ProxyResponse | JSONResponse:
    """FO-3 Phase 2 — proxy the wizard's create call to the provisioning service.

    Auth: ``require_admin``. Tenancy: the L2 is created in the calling
    admin's own Enterprise (resolved from their user row). Forwards to the
    directory's ``POST /api/v1/enterprises/{enterprise_id}/l2s`` — the body
    is an Enterprise-root-signed ``SignedEnvelope`` (directory #22 auth
    contract) — and relays the ``{job_id, l2_id, status, poll_url}``
    response, augmented with a ``stream_url`` for the SSE progress endpoint.
    """
    try:
        enterprise_id = await _resolve_caller_enterprise(store, admin)
    except _TenancyError as exc:
        return _error("TENANCY", "Caller is not scoped to an Enterprise.", str(exc), status=403)

    base = get_provisioning_api_url()
    forward_url = f"{base}/api/v1/enterprises/{enterprise_id}/l2s"

    # Directory #22 auth contract: the request body is a SignedEnvelope, NOT
    # a plain dict. Sign the wizard fields + the resolved enterprise_id with
    # the Enterprise root key (the same key the cq-server signs /announce
    # with). The directory verifies the signature, derives the Enterprise,
    # and asserts it equals the path enterprise_id. AWS binding + admin_email
    # are still derived directory-side from the Enterprise's FO-2 record.
    try:
        privkey = _load_enterprise_root_key()
    except _SigningError as exc:
        log.error("FO-3 create-L2: cannot sign directory identity proof: %s", exc)
        return _error(
            "IDENTITY_KEY_UNAVAILABLE",
            "This L2 cannot authenticate to the provisioning service.",
            str(exc),
            status=500,
        )
    envelope = _build_create_envelope(
        privkey,
        enterprise_id=enterprise_id,
        l2_slug=body.l2_slug,
        description=body.description,
        aws_region=body.aws_region,
    )

    try:
        async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SEC) as http:
            resp = await http.post(forward_url, json=envelope)
    except httpx.HTTPError as exc:
        log.warning("FO-3 create-L2 forward failed: %s (%s)", exc, forward_url)
        return _error(
            "PROVISIONING_UNREACHABLE",
            "The provisioning service is unreachable. Please retry shortly.",
            str(exc),
            status=502,
        )

    if resp.status_code >= 400:
        log.info(
            "FO-3 create-L2 upstream error: status=%s enterprise=%s slug=%s",
            resp.status_code,
            enterprise_id,
            body.l2_slug,
        )
        return _passthrough_upstream_error(resp)

    try:
        upstream = resp.json()
    except (json.JSONDecodeError, ValueError):
        return _error(
            "PROVISIONING_ERROR",
            "The provisioning service returned an unreadable response.",
            f"status {resp.status_code}",
            status=502,
        )

    job_id = upstream.get("job_id")
    if not job_id:
        return _error(
            "PROVISIONING_ERROR",
            "The provisioning service response is missing a job_id.",
            status=502,
        )

    log.info(
        "FO-3 create-L2 accepted: job_id=%s enterprise=%s slug=%s admin=%s",
        job_id,
        enterprise_id,
        body.l2_slug,
        admin,
    )

    return CreateL2ProxyResponse(
        job_id=job_id,
        l2_id=upstream.get("l2_id", f"{enterprise_id}/{body.l2_slug}"),
        status=upstream.get("status", "PROVISIONING"),
        # The directory's poll_url is Enterprise-scoped; relay it as-is for
        # callers that prefer plain polling, but the wizard uses stream_url.
        poll_url=upstream.get("poll_url", f"/api/v1/enterprises/{enterprise_id}/l2s/jobs/{job_id}"),
        stream_url=f"/api/v1/admin/l2s/jobs/{job_id}/stream",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/admin/l2s/jobs/{job_id}/stream — SSE passthrough
# ---------------------------------------------------------------------------
#
# OPS NOTE (Decision 32 #4): this SSE route holds a long-lived HTTP/1.1
# connection — the L2 ALB idle timeout MUST be >=360s (live stacks default
# 60s) or the stream is severed mid-standup. The ALB bump is a separate
# `update-stack` ops task tracked in FO-3 Phase 4; do not ship this route
# to a live L2 whose ALB still has the 60s default.


def _sse_event(payload: dict[str, Any], *, event: str | None = None) -> str:
    """Format one Server-Sent-Event frame.

    A frame is an optional ``event:`` line plus a single ``data:`` line
    carrying the JSON payload, terminated by a blank line.
    """
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def _poll_job_once(
    http: httpx.AsyncClient,
    base: str,
    enterprise_id: str,
    job_id: str,
    privkey: Ed25519PrivateKey,
) -> dict[str, Any]:
    """Fetch the directory's job row once. Never raises — returns a status dict.

    The directory's job-poll route requires an Enterprise-root identity
    proof (directory #22 auth contract). The GET has no body, so the
    SignedEnvelope travels in the ``X-8L-Identity-Proof`` header, freshly
    signed with a new ``ts`` on EVERY poll so the proof never goes stale
    against the directory's 300s replay window.

    A transport error or non-2xx upstream response is converted into a
    payload the stream can emit without crashing: the generator decides
    whether to keep polling. The directory poll route is Enterprise-scoped
    (``GET /api/v1/enterprises/{enterprise_id}/l2s/jobs/{job_id}``).
    """
    poll_url = f"{base}/api/v1/enterprises/{enterprise_id}/l2s/jobs/{job_id}"
    # Fresh proof per poll — a reused ts would 400 VALIDATION mid-stream.
    headers = {"X-8L-Identity-Proof": _build_identity_proof_header(privkey, enterprise_id=enterprise_id)}
    try:
        resp = await http.get(poll_url, headers=headers, timeout=_FORWARD_TIMEOUT_SEC)
    except httpx.HTTPError as exc:
        log.debug("FO-3 SSE poll transport error job=%s: %s", job_id, exc)
        return {"_transient_error": f"poll transport error: {exc}"}
    if resp.status_code == 404:
        # The job genuinely does not exist (or expired) — terminal.
        return {"status": "FAILED", "error": "provisioning job not found", "_not_found": True}
    if resp.status_code in (401, 403):
        # Identity proof rejected — wrong Enterprise or unverifiable key.
        # This is not a flaky upstream; it will not self-heal by retrying,
        # so end the stream rather than spin a doomed poll loop.
        log.warning("FO-3 SSE poll auth rejected (%s) job=%s", resp.status_code, job_id)
        return {"status": "FAILED", "error": f"provisioning service rejected identity proof ({resp.status_code})"}
    if resp.status_code >= 400:
        log.debug("FO-3 SSE poll upstream %s job=%s", resp.status_code, job_id)
        return {"_transient_error": f"poll upstream status {resp.status_code}"}
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return {"_transient_error": "poll response unreadable"}


async def _job_event_stream(
    base: str,
    enterprise_id: str,
    job_id: str,
    privkey: Ed25519PrivateKey,
) -> AsyncIterator[str]:
    """Async generator yielding SSE frames for one L2-create job.

    Server-side-polls the directory every ``_POLL_INTERVAL_SEC``. Each poll
    presents a freshly-signed Enterprise-root identity proof (directory #22
    auth contract — see :func:`_poll_job_once`). Emits a ``phase`` event on
    every phase / status transition, a ``heartbeat`` event at least every
    ``_HEARTBEAT_EVERY_SEC`` so the connection stays warm, and a terminal
    ``completed`` / ``failed`` event before closing.

    A poll or logging failure must NOT crash the stream (task brief): a
    transient upstream error emits a ``heartbeat`` carrying a soft warning
    and the loop continues. Only a genuine terminal job state — or the
    ``_STREAM_MAX_SEC`` ceiling — ends the stream.
    """
    last_signature: tuple[Any, Any] | None = None
    elapsed = 0.0
    since_heartbeat = 0.0

    # Opening frame so the browser EventSource fires `onopen`-adjacent
    # state immediately rather than waiting a full poll interval.
    yield _sse_event({"job_id": job_id, "status": "STREAM_OPEN"}, event="open")

    async with httpx.AsyncClient() as http:
        while elapsed < _STREAM_MAX_SEC:
            row = await _poll_job_once(http, base, enterprise_id, job_id, privkey)

            transient = row.get("_transient_error")
            if transient:
                # Soft failure — keep the stream alive, surface as heartbeat.
                yield _sse_event(
                    {"job_id": job_id, "note": transient},
                    event="heartbeat",
                )
                since_heartbeat = 0.0
            else:
                status = row.get("status", "PROVISIONING")
                phase = row.get("phase")
                signature = (status, phase)

                if signature != last_signature:
                    # Phase / status transition — emit a phase event.
                    last_signature = signature
                    since_heartbeat = 0.0
                    yield _sse_event(
                        {
                            "job_id": job_id,
                            "status": status,
                            "phase": phase,
                            "phase_label": row.get("phase_label"),
                            "progress_pct": row.get("progress_pct"),
                        },
                        event="phase",
                    )

                if status in _TERMINAL_STATES:
                    # Terminal — emit the final frame and close the stream.
                    # COMPLETED carries the job `result` (incl. the new L2's
                    # one-time admin API key for the wizard reveal).
                    if status == "COMPLETED":
                        yield _sse_event(
                            {
                                "job_id": job_id,
                                "status": status,
                                "result": row.get("result"),
                            },
                            event="completed",
                        )
                    else:
                        yield _sse_event(
                            {
                                "job_id": job_id,
                                "status": status,
                                "error": row.get("error"),
                            },
                            event="failed",
                        )
                    log.info("FO-3 SSE stream closing job=%s status=%s", job_id, status)
                    return

            await asyncio.sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC
            since_heartbeat += _POLL_INTERVAL_SEC

            if since_heartbeat >= _HEARTBEAT_EVERY_SEC:
                since_heartbeat = 0.0
                yield _sse_event({"job_id": job_id, "ts": "tick"}, event="heartbeat")

    # Ceiling hit without a terminal state — close with an explicit timeout
    # frame so the wizard can fall back to the email-delivered key.
    log.warning("FO-3 SSE stream hit %.0fs ceiling without terminal state job=%s", _STREAM_MAX_SEC, job_id)
    yield _sse_event(
        {
            "job_id": job_id,
            "status": "STREAM_TIMEOUT",
            "note": "stream closed before the job reached a terminal state; poll the job or check email for the key",
        },
        event="failed",
    )


@router.get(
    "/jobs/{job_id}/stream",
    summary="SSE progress stream for an L2-create job (proxy → provisioning service)",
    # The handler returns either a StreamingResponse (text/event-stream) or a
    # JSONResponse error envelope — neither is a pydantic model, so disable
    # response-model generation from the return annotation.
    response_model=None,
)
async def stream_l2_job(
    job_id: str,
    request: Request,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> StreamingResponse | JSONResponse:
    """FO-3 Phase 2 — SSE progress stream for an L2-create job.

    Auth: any authenticated user (``get_current_user``). The job is polled
    against the caller's own Enterprise, so a user cannot stream a job in
    another Enterprise even with a guessed ``job_id`` — the directory poll
    route is Enterprise-scoped and 404s a cross-Enterprise job.

    Returns ``text/event-stream``. Each phase transition is one ``data:``
    event; the terminal ``completed`` event carries the job ``result``
    including the new L2's one-time admin API key.
    """
    user = await store.get_user(username)
    enterprise_id = user.get("enterprise_id") if user else None
    if not enterprise_id:
        return _error(
            "TENANCY",
            "Caller is not scoped to an Enterprise.",
            "user row has no enterprise_id",
            status=403,
        )

    # The poll loop signs an Enterprise-root identity proof per directory
    # call (directory #22 auth contract). Load the key up-front so a missing
    # mount fails as a clean 500 rather than opening a stream that would
    # then emit nothing but auth-rejected frames.
    try:
        privkey = _load_enterprise_root_key()
    except _SigningError as exc:
        log.error("FO-3 SSE stream: cannot sign directory identity proof: %s", exc)
        return _error(
            "IDENTITY_KEY_UNAVAILABLE",
            "This L2 cannot authenticate to the provisioning service.",
            str(exc),
            status=500,
        )

    base = get_provisioning_api_url()
    log.info("FO-3 SSE stream opening job=%s enterprise=%s user=%s", job_id, enterprise_id, username)

    return StreamingResponse(
        _job_event_stream(base, enterprise_id, job_id, privkey),
        media_type="text/event-stream",
        headers={
            # Defeat proxy buffering so events reach the browser as emitted.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
