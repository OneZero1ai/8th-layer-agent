"""L3 consults — agent-to-agent live consult endpoints.

Sprint 2 (issue #20). Same-L2 path only — both agents are on the same
cq-server instance. Cross-L2 routing (intra-Enterprise via AIGRP +
inter-Enterprise via AI-BGP) lands in a follow-up PR; the wire shape
here is forward-compatible.

L3 IS crosstalk evolved across the substrate (`docs/decisions/10`).
The endpoint vocabulary maps 1:1 with claude-mux's existing crosstalk
MCP primitives, just lifted onto HTTP. Routing through the L2 is the
corporate-IP audit point: every consult lives durably in the
``consults`` + ``consult_messages`` tables.

Auth: bearer JWT issued by /auth/login. The token's username is mapped
to ``{enterprise}/{group}/{persona}`` via the users table at request
time. Cross-L2 calls in the future will use a separate inter-L2
service token; out of scope here.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import aigrp as aigrp_mod
from . import forward_sign
from .activity_logger import log_activity
from .auth import get_current_user
from .deps import get_store
from .store._sqlite import SqliteStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/consults", tags=["consults"])


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ConsultRequest(BaseModel):
    """Body of POST /consults/request — open a new thread."""

    to_l2_id: str = Field(min_length=1, description="Target L2 in {enterprise}/{group} form")
    to_persona: str = Field(min_length=1)
    subject: str | None = Field(default=None, max_length=200)
    # SEC-HIGH #37 — cap matches CloseRequest.resolution_summary (4000) to
    # prevent EFS-fill DoS via repeated multi-MB content payloads.
    content: str = Field(min_length=1, max_length=4096, description="The opening message of the thread")


class ConsultMessage(BaseModel):
    """Body of POST /consults/{thread_id}/messages — append a reply."""

    content: str = Field(min_length=1, max_length=4096)


class CloseRequest(BaseModel):
    """Body of POST /consults/{thread_id}/close."""

    reason: str = Field(min_length=1)
    resolution_summary: str | None = Field(default=None, max_length=4000)


class ConsultThreadOut(BaseModel):
    """One thread's metadata — appears in inbox listings + after open."""

    thread_id: str
    from_l2_id: str
    from_persona: str
    to_l2_id: str
    to_persona: str
    subject: str | None
    status: str
    claimed_by: str | None
    created_at: str
    closed_at: str | None
    resolution_summary: str | None


class ConsultMessageOut(BaseModel):
    message_id: str
    thread_id: str
    from_l2_id: str
    from_persona: str
    content: str
    created_at: str


class InboxResponse(BaseModel):
    self_l2_id: str
    self_persona: str
    threads: list[ConsultThreadOut]


class MessagesResponse(BaseModel):
    thread_id: str
    messages: list[ConsultMessageOut]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _self_identity(store: SqliteStore, username: str) -> tuple[str, str]:
    """Map the JWT subject to ``(l2_id, persona)``.

    L2 id is ``{enterprise}/{group}`` (same shape as everywhere else
    in the system). Persona defaults to the username; a future PR can
    add per-user persona aliasing if needed (e.g. multiple personas
    per human operator).
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    enterprise = str(user.get("enterprise_id") or "default-enterprise")
    group = str(user.get("group_id") or "default-group")
    return f"{enterprise}/{group}", username


# Cross-L2 forwarding helpers. We reuse the AIGRP peer table the
# 5-minute mesh poll already maintains — looking up a target L2's
# endpoint_url + enterprise by l2_id, no separate routing config.
# Cross-Enterprise is gated below with an explicit 501 (issue #19,
# AI-BGP) — same-Enterprise different-Group works today.

L2_FORWARD_TIMEOUT = float(os.environ.get("L3_FORWARD_TIMEOUT_SECS", "8"))


async def _resolve_peer(store: SqliteStore, l2_id: str) -> dict[str, Any] | None:
    """Find a peer's row in this L2's AIGRP peer table by ``l2_id``."""
    try:
        ent, _grp = l2_id.split("/", 1)
    except ValueError:
        return None
    for row in await store.list_aigrp_peers(ent):
        if row["l2_id"] == l2_id:
            return row
    return None


def _self_l2_id() -> str:
    """This process's identity — same shape AIGRP uses on the wire."""
    return f"{aigrp_mod.enterprise()}/{aigrp_mod.group()}"


def _bearer_for_target(target_l2_id: str) -> str:
    """Resolve the Authorization-Bearer value for an outbound cross-L2 call.

    Phase 1.0d preference:

    1. Pair-secret bearer (intra-Enterprise, Decision 28). Tries
       ``aigrp.derive_bearer_token`` first when ``target_l2_id`` is a
       sibling under our Enterprise. Any SSM/KMS read failure falls
       through to the legacy bearer — operator can still recover by
       leaving ``CQ_AIGRP_PEER_KEY`` set during cutover.
    2. Legacy ``CQ_AIGRP_PEER_KEY`` env var (cross-Enterprise + cutover
       fallback).

    Returns an empty string if neither path resolves; caller surfaces 503.
    """
    self_id = _self_l2_id()
    if target_l2_id and target_l2_id != self_id:
        target_enterprise, sep, _ = target_l2_id.partition("/")
        if sep and target_enterprise == aigrp_mod.enterprise():
            try:
                return aigrp_mod.derive_bearer_token(target_l2_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "consults forward: pair-secret derive failed for target=%s; "
                    "falling back to legacy CQ_AIGRP_PEER_KEY",
                    target_l2_id,
                )
    return os.environ.get("CQ_AIGRP_PEER_KEY", "")


def _build_forward_headers(target_l2_id: str, payload: dict[str, Any]) -> dict[str, str]:
    """Compose authorization + forwarder-identity + (sprint 4) signature
    headers for outbound /consults/forward-* calls.

    Phase 1.0d — bearer is per-target (HKDF-derived pair-secret) when the
    target is an intra-Enterprise sibling; legacy shared bearer otherwise.

    The signature header is omitted when no L2 keypair is available on
    disk — receivers fall back to legacy unsigned mode (until they flip
    ``CQ_REQUIRE_SIGNED_FORWARDS=true``).
    """
    from . import forward_sign

    bearer = _bearer_for_target(target_l2_id)
    forwarder_id = _self_l2_id()
    headers = {
        "authorization": f"Bearer {bearer}",
        aigrp_mod.FORWARDER_HEADER: forwarder_id,
    }
    sig = forward_sign.sign_forward_request(payload, forwarder_id)
    if sig:
        headers[forward_sign.SIGNATURE_HEADER] = sig
    return headers


def _forward_request(target: dict[str, Any], payload: dict[str, Any]) -> None:
    """POST /consults/forward-request to a peer L2 with the peer-key bearer.

    Best-effort: a forward failure does NOT roll back the local mirror
    write. Callers see 502 if the peer is unreachable so they know the
    remote side won't see the message until they reach the recipient L2
    directly. Subsequent replies retry independently.

    Sprint 4 (#44) — when this L2 has an Ed25519 keypair on disk, the
    request body is signed and the sig travels in ``X-8L-Forwarder-Sig``.
    """
    base = target["endpoint_url"].rstrip("/")
    headers = _build_forward_headers(target["l2_id"], payload)
    if not headers["authorization"].removeprefix("Bearer ").strip():
        raise HTTPException(
            503, detail="cross-L2 routing requires either Enterprise root SSM access or CQ_AIGRP_PEER_KEY"
        )
    try:
        with httpx.Client(timeout=L2_FORWARD_TIMEOUT) as client:
            r = client.post(
                f"{base}/api/v1/consults/forward-request",
                headers=headers,
                json=payload,
            )
        if r.status_code >= 400:
            # SEC-MED M-1 — log diagnosis-rich detail server-side; return
            # a generic 502 to the client so peer response bodies (which
            # may contain stack traces / config paths / tenant ids) don't
            # leak through to the originating user.
            logger.warning(
                "consults forward-request: peer=%s status=%s body=%r",
                target["l2_id"],
                r.status_code,
                r.text[:200],
            )
            raise HTTPException(status_code=502, detail="peer unreachable")
        # agent#36 — positive proof the cross-Enterprise forward fired.
        # Without this, a missing peer reply is indistinguishable from a
        # forward that never left this L2; the source-side log disambiguates.
        logger.info(
            "consults forward-request: peer=%s thread=%s status=%s — cross-Enterprise forward delivered",
            target["l2_id"],
            payload.get("thread_id", "?"),
            r.status_code,
        )
    except httpx.RequestError as e:
        logger.warning(
            "consults forward-request: peer=%s transport_error=%r",
            target["l2_id"],
            e,
        )
        raise HTTPException(status_code=502, detail="peer unreachable") from e


def _forward_message(target: dict[str, Any], payload: dict[str, Any]) -> None:
    """POST /consults/forward-message — symmetric to _forward_request.

    Sprint 4 (#44) — same Ed25519 signature treatment as forward-request.
    """
    base = target["endpoint_url"].rstrip("/")
    headers = _build_forward_headers(target["l2_id"], payload)
    if not headers["authorization"].removeprefix("Bearer ").strip():
        raise HTTPException(
            503, detail="cross-L2 routing requires either Enterprise root SSM access or CQ_AIGRP_PEER_KEY"
        )
    try:
        with httpx.Client(timeout=L2_FORWARD_TIMEOUT) as client:
            r = client.post(
                f"{base}/api/v1/consults/forward-message",
                headers=headers,
                json=payload,
            )
        if r.status_code >= 400:
            # SEC-MED M-1 — log server-side, return generic to client.
            logger.warning(
                "consults forward-message: peer=%s status=%s body=%r",
                target["l2_id"],
                r.status_code,
                r.text[:200],
            )
            raise HTTPException(status_code=502, detail="peer unreachable")
        # agent#36 — positive proof the cross-Enterprise forward fired.
        logger.info(
            "consults forward-message: peer=%s thread=%s status=%s — cross-Enterprise forward delivered",
            target["l2_id"],
            payload.get("thread_id", "?"),
            r.status_code,
        )
    except httpx.RequestError as e:
        logger.warning(
            "consults forward-message: peer=%s transport_error=%r",
            target["l2_id"],
            e,
        )
        raise HTTPException(status_code=502, detail="peer unreachable") from e


async def _resolve_x_enterprise_target(
    store: SqliteStore, to_l2_id: str, self_enterprise: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve a cross-Enterprise consult target via the directory peering mirror.

    Returns ``(peering_row, target_endpoint_row)`` when an active peering
    exists between this enterprise and the target's enterprise, AND the
    target l2_id appears in the peering's ``to_l2_endpoints_json`` roster
    (the directory's snapshot of the other side's L2s at peering time).
    Returns None when no peering exists or the target L2 isn't in the
    peering's roster.

    The bearer + per-L2 sig auth on the wire is derived from the
    returned peering record (caller composes via ``forward_sign``).
    """
    try:
        target_enterprise, _ = to_l2_id.split("/", 1)
    except ValueError:
        return None
    if target_enterprise == self_enterprise:
        # Caller should have routed this as same-Enterprise — defensive guard.
        return None
    peering = await store.find_active_directory_peering(
        from_enterprise=self_enterprise,
        to_enterprise=target_enterprise,
    )
    if peering is None:
        return None
    try:
        endpoints = json.loads(peering.get("to_l2_endpoints_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        endpoints = []
    for ep in endpoints:
        if isinstance(ep, dict) and ep.get("l2_id") == to_l2_id:
            return peering, ep
    return None


X_ENTERPRISE_FORWARD_PATH = "/api/v1/consults/x-enterprise-forward-request"


def _x_enterprise_forward_request(
    peering: dict[str, Any],
    target_endpoint: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """POST a cross-Enterprise consult forward to the peer L2.

    Auth on the wire is **two-layer**:

    1. ``Authorization: Bearer <peering-bearer>`` — derived locally
       from the peering's offer + accept signatures (HKDF-SHA256). The
       receiver, also a party to this peering, derives the same bearer
       and matches. This proves "the sender knows about an active
       peering between us and them."

    2. ``X-8L-Forwarder-Sig`` (when this L2 has an Ed25519 keypair on
       disk per sprint-4 PR #53) — Ed25519 signature over the canonical
       request body + forwarder l2_id. The receiver looks up the
       forwarder's pubkey from its peering record's roster and verifies.
       This proves "this specific L2 within the sender's enterprise
       sent this forward."

    Plus the ``X-8L-Forwarder-L2-Id`` and ``X-8L-Peering-Offer-Id``
    headers so the receiver can look up the relevant peering + pubkey
    deterministically.

    Best-effort like the same-Enterprise forward path. Failure does NOT
    roll back the local mirror write.
    """
    base = target_endpoint["endpoint_url"].rstrip("/")
    bearer = forward_sign.derive_peering_bearer(
        peering["offer_signature_b64u"],
        peering["accept_signature_b64u"],
    )
    forwarder_id = _self_l2_id()
    headers: dict[str, str] = {
        "authorization": f"Bearer {bearer}",
        aigrp_mod.FORWARDER_HEADER: forwarder_id,
        "x-8l-peering-offer-id": peering["offer_id"],
    }
    sig = forward_sign.sign_forward_request(payload, forwarder_id)
    if sig:
        headers[forward_sign.SIGNATURE_HEADER] = sig
    try:
        with httpx.Client(timeout=L2_FORWARD_TIMEOUT) as client:
            r = client.post(
                f"{base}{X_ENTERPRISE_FORWARD_PATH}",
                headers=headers,
                json=payload,
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"peer {target_endpoint['l2_id']} returned {r.status_code} "
                    f"on x-enterprise-forward-request: {r.text[:200]}"
                ),
            )
        # agent#36 — positive proof the cross-Enterprise forward fired.
        # A missing peer reply downstream is then attributable to the peer
        # side (no auto-reply handler, slow cron), not a forward that never
        # left this L2.
        logger.info(
            "consults x-enterprise-forward-request: peer=%s thread=%s status=%s — cross-Enterprise forward delivered",
            target_endpoint["l2_id"],
            payload.get("thread_id", "?"),
            r.status_code,
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"peer {target_endpoint['l2_id']} unreachable: {e}",
        ) from e


def _redact_for_policy(content: str, policy: str) -> str:
    """Apply ``consult_logging_policy`` to a message body for local storage.

    Per directory-v1 spec + decisions/10:
    - mutual_log_required → store full content
    - summary_only_log    → redact body, keep thread metadata only
    - no_log_consults     → caller skips the message-row write entirely;
                            this helper still returns a redacted string
                            for any path that does insist on writing.
    """
    if policy == "mutual_log_required":
        return content
    return f"<redacted: {policy}>"


def _to_thread_out(row: dict[str, Any]) -> ConsultThreadOut:
    return ConsultThreadOut(
        thread_id=row["thread_id"],
        from_l2_id=row["from_l2_id"],
        from_persona=row["from_persona"],
        to_l2_id=row["to_l2_id"],
        to_persona=row["to_persona"],
        subject=row.get("subject"),
        status=row["status"],
        claimed_by=row.get("claimed_by"),
        created_at=row["created_at"],
        closed_at=row.get("closed_at"),
        resolution_summary=row.get("resolution_summary"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/request", response_model=ConsultThreadOut, status_code=201)
async def request_consult(
    body: ConsultRequest,
    background_tasks: BackgroundTasks,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultThreadOut:
    """Open a new consult thread. Caller becomes the ``from_*`` side.

    Same-L2 path only (sprint 2). When ``to_l2_id`` doesn't match this
    L2's identity, returns 501 — cross-L2 routing is the next PR.
    Drops the opening message into ``consult_messages`` immediately so
    a single round-trip is sufficient for the asker.
    """
    self_l2_id, self_persona = await _self_identity(store, username)
    self_enterprise = aigrp_mod.enterprise()

    thread_id = f"th_{uuid4().hex[:16]}"
    msg_id = f"msg_{uuid4().hex[:16]}"
    now = _now_iso()

    # Three routing modes:
    #   1. same-L2 (this process)               — no forward
    #   2. cross-L2 same-Enterprise              — forward via AIGRP peer table + EnterprisePeerKey
    #   3. cross-Enterprise                      — forward via directory peering mirror + per-pair bearer
    target_peer: dict[str, Any] | None = None
    x_enterprise: tuple[dict[str, Any], dict[str, Any]] | None = None
    target_logging_policy = "mutual_log_required"  # default for same-L2 / same-Enterprise

    is_same_l2 = body.to_l2_id == self_l2_id

    if not is_same_l2:
        # Distinguish same-Enterprise (uses AIGRP peer table) from
        # cross-Enterprise (uses directory peering mirror).
        try:
            target_enterprise, _ = body.to_l2_id.split("/", 1)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"to_l2_id {body.to_l2_id!r} must be enterprise/group form",
            ) from None

        if target_enterprise == self_enterprise:
            # Same-Enterprise — must be in AIGRP peer table.
            target_peer = await _resolve_peer(store, body.to_l2_id)
            if target_peer is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"target L2 {body.to_l2_id!r} is not in this L2's AIGRP "
                        "peer table — AIGRP hasn't converged yet or the L2 is "
                        "not part of this Enterprise."
                    ),
                )
        else:
            # Cross-Enterprise — must have an active directory peering.
            x_enterprise = await _resolve_x_enterprise_target(store, body.to_l2_id, self_enterprise)
            if x_enterprise is None:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"no active peering covers {body.to_l2_id!r}. Either "
                        "no peering exists with enterprise "
                        f"{target_enterprise!r}, or the target L2 is not in the "
                        "peering's roster."
                    ),
                )
            peering_row, _ep = x_enterprise
            target_logging_policy = peering_row["consult_logging_policy"]

    # Mirror-write the thread + opening message LOCALLY first. This is
    # the asker's audit point: even if the forward fails, the asker's
    # L2 has a durable record of "I tried to consult X on Y at T".
    # Logging-policy applies to the asker side too — for symmetry — so
    # both ends honor the same policy on what gets persisted.
    await store.create_consult(
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        to_l2_id=body.to_l2_id,
        to_persona=body.to_persona,
        subject=body.subject,
        created_at=now,
    )
    if target_logging_policy != "no_log_consults":
        await store.append_consult_message(
            message_id=msg_id,
            thread_id=thread_id,
            from_l2_id=self_l2_id,
            from_persona=self_persona,
            content=_redact_for_policy(body.content, target_logging_policy),
            created_at=now,
        )

    forward_payload = {
        "thread_id": thread_id,
        "message_id": msg_id,
        "from_l2_id": self_l2_id,
        "from_persona": self_persona,
        "to_l2_id": body.to_l2_id,
        "to_persona": body.to_persona,
        "subject": body.subject,
        "content": body.content,
        "created_at": now,
    }

    # Forward via the appropriate path. Both sides log per the
    # logging policy = the routing-through-L2 corporate-IP audit point
    # per decisions/10.
    if target_peer is not None:
        _forward_request(target_peer, forward_payload)
    elif x_enterprise is not None:
        peering_row, target_endpoint = x_enterprise
        _x_enterprise_forward_request(peering_row, target_endpoint, forward_payload)

    row = await store.get_consult(thread_id)
    assert row is not None  # just inserted

    # Activity log (#108): consult opened. Both same-L2 and cross-L2
    # paths log on the asker side; the recipient L2 logs separately
    # when its forward-request handler fires (covered there).
    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="consult_open",
        payload={
            "thread_id": thread_id,
            "to_l2_id": body.to_l2_id,
            "to_persona": body.to_persona,
        },
        thread_or_chain_id=thread_id,
    )
    return _to_thread_out(row)


@router.post("/{thread_id}/messages", response_model=ConsultMessageOut, status_code=201)
async def post_consult_message(
    thread_id: str,
    body: ConsultMessage,
    background_tasks: BackgroundTasks,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultMessageOut:
    """Append a message to an existing thread.

    The caller must be one of the two participants (from_persona or
    to_persona on the matching L2). Closed threads reject with 409.
    """
    self_l2_id, self_persona = await _self_identity(store, username)
    thread = await store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")

    # Authz: caller must be one of the two participants on this L2.
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")

    if thread["status"] == "closed":
        raise HTTPException(status_code=409, detail="thread is closed")

    msg_id = f"msg_{uuid4().hex[:16]}"
    now = _now_iso()

    # Resolve the OTHER side's L2 for cross-L2 forward. The thread
    # carries both endpoints; the side that's NOT us is where the
    # message has to be mirrored.
    other_l2_id = thread["to_l2_id"] if is_from else thread["from_l2_id"]
    other_persona = thread["to_persona"] if is_from else thread["from_persona"]
    target_peer = None
    if other_l2_id != self_l2_id:
        target_peer = await _resolve_peer(store, other_l2_id)
        if target_peer is None:
            raise HTTPException(
                status_code=502,
                detail=f"peer L2 {other_l2_id!r} not in AIGRP peer table",
            )

    await store.append_consult_message(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )

    if target_peer is not None:
        _forward_message(
            target_peer,
            {
                "thread_id": thread_id,
                "message_id": msg_id,
                "from_l2_id": self_l2_id,
                "from_persona": self_persona,
                # Hand the thread shape back so the peer can lazily mirror
                # the thread row if it's somehow missing (defensive — should
                # always exist from the original /consults/forward-request).
                "thread_subject": thread.get("subject"),
                "thread_to_l2_id": thread["to_l2_id"],
                "thread_to_persona": thread["to_persona"],
                "thread_from_l2_id": thread["from_l2_id"],
                "thread_from_persona": thread["from_persona"],
                "thread_created_at": thread["created_at"],
                "content": body.content,
                "created_at": now,
            },
        )
        # Kept side-note: cross-L2 unused var lint guard
        del other_persona

    # Activity log (#108): consult reply. ``thread_or_chain_id``
    # carries the consult thread id so dashboards can group
    # send/reply/close events into a single workflow.
    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="consult_reply",
        payload={"thread_id": thread_id, "message_id": msg_id},
        thread_or_chain_id=thread_id,
    )
    return ConsultMessageOut(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )


@router.get("/{thread_id}/messages", response_model=MessagesResponse)
async def get_consult_messages(
    thread_id: str,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> MessagesResponse:
    """Read every message on a thread the caller participates in."""
    self_l2_id, self_persona = await _self_identity(store, username)
    thread = await store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")
    msgs = await store.list_consult_messages(thread_id)
    return MessagesResponse(
        thread_id=thread_id,
        messages=[ConsultMessageOut(**m) for m in msgs],
    )


@router.post("/{thread_id}/close", response_model=ConsultThreadOut)
async def close_consult(
    thread_id: str,
    body: CloseRequest,
    background_tasks: BackgroundTasks,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultThreadOut:
    """Mark the thread closed. Either participant can close.

    Activity log (#108): non-blocking ``consult_close`` row carries
    the close reason so dashboards can break down consult outcomes
    (resolved vs abandoned vs out-of-scope etc.).
    """
    self_l2_id, self_persona = await _self_identity(store, username)
    thread = await store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")

    closed = await store.close_consult(
        thread_id=thread_id,
        closed_at=_now_iso(),
        resolution_summary=body.resolution_summary,
    )
    if not closed:
        # Already closed; just return current state without erroring
        logger.info("close_consult on already-closed thread_id=%s", thread_id)
    row = await store.get_consult(thread_id)
    assert row is not None
    if closed:
        # Reputation hook (#108 sub-task 5). Best-effort — record_event
        # swallows on failure so a flaky reputation chain never blocks
        # consult-close. Body shape per reputation-v1.md §"consult.closed".
        from .reputation import record_event as _record_event

        _record_event(
            store._conn,
            event_type="consult.closed",
            body={
                "thread_id": thread_id,
                "from_l2_id": row["from_l2_id"],
                "to_l2_id": row["to_l2_id"],
                "csat": row.get("csat"),
                "resolution_summary": body.resolution_summary,
            },
        )
    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="consult_close",
        payload={"thread_id": thread_id, "reason": body.reason},
        thread_or_chain_id=thread_id,
    )
    return _to_thread_out(row)


# ---------------------------------------------------------------------------
# Internal forward endpoints (peer-key auth) — receive a mirrored thread
# from a sibling L2 in the same Enterprise. Not called by clients.
# ---------------------------------------------------------------------------


class ForwardRequestBody(BaseModel):
    thread_id: str
    message_id: str
    from_l2_id: str
    from_persona: str
    to_l2_id: str
    to_persona: str = Field(min_length=1)
    subject: str | None = Field(default=None, max_length=512)
    content: str = Field(min_length=1, max_length=4096)
    created_at: str


@router.post("/forward-request", status_code=201)
async def forward_consult_request(
    body: ForwardRequestBody,
    request: Request,
    store: SqliteStore = Depends(get_store),
    _peer: None = Depends(aigrp_mod.require_peer_key),
) -> dict[str, str]:
    """Mirror a remote consult request onto this L2.

    Called by a sibling L2 in the same Enterprise after the originating
    side did its local mirror-write. We re-do the same writes here so
    BOTH L2s have the durable corporate-IP record per decisions/10.

    SEC-CRIT #34 — receiver pins ``X-8L-Forwarder-L2-Id`` to ``body.from_l2_id``
    and to its own Enterprise; closes cross-Enterprise impersonation outright
    and surfaces the residual sibling-L2 gap (sprint 4 / Ed25519).

    Idempotent on thread_id collision: if the thread already exists
    (re-delivery, retry), we skip the create and just append the message
    if it's new. Same for message_id.

    Sprint 4 (#44) — pubkey-on-file peers must also present a valid
    ``X-8L-Forwarder-Sig`` over JCS(body) || from_l2_id.
    """
    await aigrp_mod.require_forwarder_identity(
        request,
        body.from_l2_id,
        body_for_sig=body.model_dump(mode="json"),
        store=store,
    )
    if await store.get_consult(body.thread_id) is None:
        await store.create_consult(
            thread_id=body.thread_id,
            from_l2_id=body.from_l2_id,
            from_persona=body.from_persona,
            to_l2_id=body.to_l2_id,
            to_persona=body.to_persona,
            subject=body.subject,
            created_at=body.created_at,
        )
    # Idempotent on message_id via PRIMARY KEY; sqlite raises IntegrityError.
    # We swallow it because re-delivery should be a no-op, not a 500.
    try:
        await store.append_consult_message(
            message_id=body.message_id,
            thread_id=body.thread_id,
            from_l2_id=body.from_l2_id,
            from_persona=body.from_persona,
            content=body.content,
            created_at=body.created_at,
        )
    except Exception as e:
        # IntegrityError on duplicate message_id is fine; everything else re-raises.
        if "UNIQUE constraint failed" not in str(e):
            raise
    return {"status": "mirrored", "thread_id": body.thread_id}


class ForwardMessageBody(BaseModel):
    thread_id: str
    message_id: str
    from_l2_id: str
    from_persona: str
    content: str = Field(min_length=1, max_length=4096)
    created_at: str
    # Defensive: thread metadata so the receiver can lazily backfill if
    # the original /forward-request was lost.
    thread_subject: str | None = Field(default=None, max_length=512)
    thread_to_l2_id: str | None = None
    thread_to_persona: str | None = None
    thread_from_l2_id: str | None = None
    thread_from_persona: str | None = None
    thread_created_at: str | None = None


@router.post("/forward-message", status_code=201)
async def forward_consult_message(
    body: ForwardMessageBody,
    request: Request,
    store: SqliteStore = Depends(get_store),
    _peer: None = Depends(aigrp_mod.require_peer_key),
) -> dict[str, str]:
    """Mirror a reply onto this L2.

    If the thread row is missing (e.g. /forward-request was lost), we
    create it from the embedded thread metadata before appending. This
    prevents a single dropped forward from permanently breaking the
    audit trail on this side.

    SEC-CRIT #34 — same forwarder-identity binding as /forward-request.
    Sprint 4 (#44) — also requires Ed25519 forward signature for peers
    with a recorded pubkey.
    """
    await aigrp_mod.require_forwarder_identity(
        request,
        body.from_l2_id,
        body_for_sig=body.model_dump(mode="json"),
        store=store,
    )
    existing = await store.get_consult(body.thread_id)
    if existing is None:
        if not (
            body.thread_to_l2_id
            and body.thread_to_persona
            and body.thread_from_l2_id
            and body.thread_from_persona
            and body.thread_created_at
        ):
            raise HTTPException(
                status_code=400,
                detail="thread metadata missing and no existing thread to append to",
            )
        await store.create_consult(
            thread_id=body.thread_id,
            from_l2_id=body.thread_from_l2_id,
            from_persona=body.thread_from_persona,
            to_l2_id=body.thread_to_l2_id,
            to_persona=body.thread_to_persona,
            subject=body.thread_subject,
            created_at=body.thread_created_at,
        )
    try:
        await store.append_consult_message(
            message_id=body.message_id,
            thread_id=body.thread_id,
            from_l2_id=body.from_l2_id,
            from_persona=body.from_persona,
            content=body.content,
            created_at=body.created_at,
        )
    except Exception as e:
        if "UNIQUE constraint failed" not in str(e):
            raise
    return {"status": "mirrored", "thread_id": body.thread_id}


# ---------------------------------------------------------------------------
# Cross-Enterprise forward — receiver side (sprint 4 Track A phase 2)
# ---------------------------------------------------------------------------


def _hmac_eq(a: str, b: str) -> bool:
    """Constant-time equality for two strings of arbitrary length."""
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def _require_x_enterprise_auth(
    request: Request,
    body: ForwardRequestBody,
    store: SqliteStore,
) -> dict[str, Any]:
    """Validate a cross-Enterprise forward.

    Returns the active peering row that authorised this forward — caller
    uses ``consult_logging_policy`` from it.

    Raises:
        HTTPException 400 — missing/malformed required headers.
        HTTPException 401 — bearer doesn't match the peering's derived bearer.
        HTTPException 403 — body identity doesn't match headers, or the peering
                            is unknown / inactive on this L2.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer auth")
    presented_bearer = auth[7:]

    forwarder_l2_id = request.headers.get(aigrp_mod.FORWARDER_HEADER, "").strip()
    if not forwarder_l2_id:
        raise HTTPException(
            status_code=400,
            detail=f"missing {aigrp_mod.FORWARDER_HEADER} header",
        )
    offer_id = request.headers.get("x-8l-peering-offer-id", "").strip()
    if not offer_id:
        raise HTTPException(
            status_code=400,
            detail="missing x-8l-peering-offer-id header",
        )

    # Identity binding — the body's from_l2_id must match the header.
    if body.from_l2_id != forwarder_l2_id:
        raise HTTPException(
            status_code=403,
            detail=(f"forwarder identity mismatch: header={forwarder_l2_id!r} body.from_l2_id={body.from_l2_id!r}"),
        )

    # Peering lookup — by offer_id directly.
    peerings = [p for p in await store.list_directory_peerings(status="active") if p["offer_id"] == offer_id]
    if not peerings:
        raise HTTPException(
            status_code=403,
            detail=f"no active peering with offer_id={offer_id!r} on this L2",
        )
    peering = peerings[0]

    # Per-spec: at expires_at, peering rolls off. Belt + braces (also
    # filtered server-side via find_active_directory_peering, but this
    # path uses list which doesn't expire-filter).
    if peering.get("expires_at") and peering["expires_at"] < datetime.now(UTC).isoformat():
        raise HTTPException(status_code=403, detail="peering expired")

    # Forwarder enterprise must match the OTHER side of the peering.
    forwarder_enterprise, _, _ = forwarder_l2_id.partition("/")
    self_enterprise = aigrp_mod.enterprise()
    other_enterprise = (
        peering["from_enterprise"] if peering["to_enterprise"] == self_enterprise else peering["to_enterprise"]
    )
    if forwarder_enterprise != other_enterprise:
        raise HTTPException(
            status_code=403,
            detail=(
                f"forwarder enterprise {forwarder_enterprise!r} is not the "
                f"other side of peering {offer_id!r} (other_side={other_enterprise!r})"
            ),
        )

    # Bearer derivation — both sides compute the same value from the
    # bilateral signatures. Constant-time compare.
    expected_bearer = forward_sign.derive_peering_bearer(
        peering["offer_signature_b64u"],
        peering["accept_signature_b64u"],
    )
    if not _hmac_eq(presented_bearer, expected_bearer):
        raise HTTPException(status_code=401, detail="invalid peering bearer")

    # Sprint-4 V1: Ed25519 forwarder-sig verification is deferred —
    # roster doesn't yet carry per-L2 pubkeys. The signature, when
    # present, is recorded but not enforced. V2 adds pubkeys to the
    # roster + flips this to enforced.
    return peering


@router.post("/x-enterprise-forward-request", status_code=201)
async def x_enterprise_forward_consult_request(
    body: ForwardRequestBody,
    request: Request,
    store: SqliteStore = Depends(get_store),
) -> dict[str, str]:
    """Mirror a cross-Enterprise consult request onto this L2.

    Auth is two-layer (sprint 4 Track A — see ``_require_x_enterprise_auth``):
    bearer derived from the peering's bilateral signatures + body/header
    identity binding. The shared EnterprisePeerKey is intentionally NOT
    used here — it's intra-Enterprise only.

    Logging is governed by ``consult_logging_policy`` from the peering:

    - ``mutual_log_required`` (default) — full body persisted both sides
    - ``summary_only_log`` — thread row + redacted message body
    - ``no_log_consults`` — thread row only (audit point: who asked whom),
      message body discarded after the receiver agent reads it in real time

    Idempotent on ``(thread_id, message_id)`` like the same-Enterprise
    forward path. Body shape matches ForwardRequestBody for consistency.
    """
    peering = await _require_x_enterprise_auth(request, body, store)
    policy = peering["consult_logging_policy"]

    # Issue #98 — validate that body.to_persona corresponds to a real
    # user on this L2 (matching enterprise + group). Without this guard,
    # a typo'd or stale persona name produces a thread+message pair
    # nobody can ever read (no inbox surface for a non-existent user)
    # and the sender gets a false-positive "delivered" signal.
    #
    # Group-scoped consults (to_persona empty/None) are allowed through
    # without persona validation — current schema requires non-empty
    # to_persona but we keep the conditional for forward compatibility
    # with a future Group-only address shape.
    if body.to_persona:
        user = await store.get_user(body.to_persona)
        self_enterprise = aigrp_mod.enterprise()
        self_group = aigrp_mod.group()
        if (
            user is None
            or str(user.get("enterprise_id") or "default-enterprise") != self_enterprise
            or str(user.get("group_id") or "default-group") != self_group
        ):
            raise HTTPException(status_code=404, detail="to_persona not found")

    # Thread row: always created (audit point: cross-Enterprise consult
    # was attempted, regardless of policy).
    if await store.get_consult(body.thread_id) is None:
        await store.create_consult(
            thread_id=body.thread_id,
            from_l2_id=body.from_l2_id,
            from_persona=body.from_persona,
            to_l2_id=body.to_l2_id,
            to_persona=body.to_persona,
            subject=body.subject,
            created_at=body.created_at,
        )

    # Message body: only persisted under mutual_log_required (full) or
    # summary_only_log (redacted). no_log_consults skips the row.
    if policy != "no_log_consults":
        try:
            await store.append_consult_message(
                message_id=body.message_id,
                thread_id=body.thread_id,
                from_l2_id=body.from_l2_id,
                from_persona=body.from_persona,
                content=_redact_for_policy(body.content, policy),
                created_at=body.created_at,
            )
        except Exception as e:
            if "UNIQUE constraint failed" not in str(e):
                raise

    return {
        "status": "mirrored",
        "thread_id": body.thread_id,
        "logging_policy_applied": policy,
    }


@router.get("/inbox", response_model=InboxResponse)
async def get_inbox(
    include_closed: bool = False,
    limit: int = 50,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> InboxResponse:
    """Threads addressed to the caller on this L2.

    Default excludes closed threads. `include_closed=true` returns the
    audit view (all threads, sorted by created_at DESC).
    """
    self_l2_id, self_persona = await _self_identity(store, username)
    rows = await store.list_inbox(
        to_l2_id=self_l2_id,
        to_persona=self_persona,
        include_closed=include_closed,
        limit=max(1, min(limit, 200)),
    )
    return InboxResponse(
        self_l2_id=self_l2_id,
        self_persona=self_persona,
        threads=[_to_thread_out(r) for r in rows],
    )


# Registered last so the parameterised path ``/{thread_id}`` does not
# shadow static routes like ``/inbox`` and ``/forward-request``.
@router.get("/{thread_id}", response_model=ConsultThreadOut)
async def get_consult_thread(
    thread_id: str,
    store: SqliteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultThreadOut:
    """Fetch thread metadata for a thread the caller participates in."""
    self_l2_id, self_persona = await _self_identity(store, username)
    thread = await store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")
    return _to_thread_out(thread)
