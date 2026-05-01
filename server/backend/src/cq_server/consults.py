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

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import aigrp as aigrp_mod
from .auth import get_current_user
from .deps import get_store
from .store import RemoteStore

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
    content: str = Field(min_length=1, description="The opening message of the thread")


class ConsultMessage(BaseModel):
    """Body of POST /consults/{thread_id}/messages — append a reply."""

    content: str = Field(min_length=1)


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


def _self_identity(store: RemoteStore, username: str) -> tuple[str, str]:
    """Map the JWT subject to ``(l2_id, persona)``.

    L2 id is ``{enterprise}/{group}`` (same shape as everywhere else
    in the system). Persona defaults to the username; a future PR can
    add per-user persona aliasing if needed (e.g. multiple personas
    per human operator).
    """
    user = store.get_user(username)
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


def _resolve_peer(store: RemoteStore, l2_id: str) -> dict[str, Any] | None:
    """Find a peer's row in this L2's AIGRP peer table by ``l2_id``."""
    try:
        ent, _grp = l2_id.split("/", 1)
    except ValueError:
        return None
    for row in store.list_aigrp_peers(ent):
        if row["l2_id"] == l2_id:
            return row
    return None


def _self_l2_id() -> str:
    """This process's identity — same shape AIGRP uses on the wire."""
    return f"{aigrp_mod.enterprise()}/{aigrp_mod.group()}"


def _forward_request(target: dict[str, Any], payload: dict[str, Any]) -> None:
    """POST /consults/forward-request to a peer L2 with the peer-key bearer.

    Best-effort: a forward failure does NOT roll back the local mirror
    write. Callers see 502 if the peer is unreachable so they know the
    remote side won't see the message until they reach the recipient L2
    directly. Subsequent replies retry independently.
    """
    base = target["endpoint_url"].rstrip("/")
    peer_key = os.environ.get("CQ_AIGRP_PEER_KEY", "")
    if not peer_key:
        raise HTTPException(503, detail="cross-L2 routing requires CQ_AIGRP_PEER_KEY")
    try:
        with httpx.Client(timeout=L2_FORWARD_TIMEOUT) as client:
            r = client.post(
                f"{base}/api/v1/consults/forward-request",
                headers={"authorization": f"Bearer {peer_key}"},
                json=payload,
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"peer {target['l2_id']} returned {r.status_code} on /consults/forward-request: {r.text[:200]}",
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"peer {target['l2_id']} unreachable: {e}",
        ) from e


def _forward_message(target: dict[str, Any], payload: dict[str, Any]) -> None:
    """POST /consults/forward-message — symmetric to _forward_request."""
    base = target["endpoint_url"].rstrip("/")
    peer_key = os.environ.get("CQ_AIGRP_PEER_KEY", "")
    if not peer_key:
        raise HTTPException(503, detail="cross-L2 routing requires CQ_AIGRP_PEER_KEY")
    try:
        with httpx.Client(timeout=L2_FORWARD_TIMEOUT) as client:
            r = client.post(
                f"{base}/api/v1/consults/forward-message",
                headers={"authorization": f"Bearer {peer_key}"},
                json=payload,
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"peer {target['l2_id']} returned {r.status_code} on /consults/forward-message",
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"peer {target['l2_id']} unreachable: {e}",
        ) from e


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
def request_consult(
    body: ConsultRequest,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultThreadOut:
    """Open a new consult thread. Caller becomes the ``from_*`` side.

    Same-L2 path only (sprint 2). When ``to_l2_id`` doesn't match this
    L2's identity, returns 501 — cross-L2 routing is the next PR.
    Drops the opening message into ``consult_messages`` immediately so
    a single round-trip is sufficient for the asker.
    """
    self_l2_id, self_persona = _self_identity(store, username)

    thread_id = f"th_{uuid4().hex[:16]}"
    msg_id = f"msg_{uuid4().hex[:16]}"
    now = _now_iso()

    # Resolve cross-L2 target if the recipient lives on a different L2.
    is_same_l2 = body.to_l2_id == self_l2_id
    target_peer: dict[str, Any] | None = None

    if not is_same_l2:
        target_peer = _resolve_peer(store, body.to_l2_id)
        if target_peer is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"target L2 {body.to_l2_id!r} is not in this L2's AIGRP peer "
                    "table — either it's not in the same Enterprise or AIGRP "
                    "hasn't converged yet. Cross-Enterprise consults will use "
                    "AI-BGP (issue #19), not yet shipped."
                ),
            )
        # Cross-Enterprise reach is gated on AI-BGP. Today it's just a
        # peer-table lookup — same-Enterprise only by virtue of how the
        # peer table is populated. Belt-and-braces guard:
        if target_peer["enterprise"] != aigrp_mod.enterprise():
            raise HTTPException(
                status_code=501,
                detail=(
                    "cross-Enterprise consult routing is on the roadmap "
                    "(issue #19 AI-BGP); same-Enterprise cross-Group works today."
                ),
            )

    # Mirror-write the thread + opening message LOCALLY first. This is
    # the asker's audit point: even if the forward fails, the asker's
    # L2 has a durable record of "I tried to consult X on Y at T".
    store.create_consult(
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        to_l2_id=body.to_l2_id,
        to_persona=body.to_persona,
        subject=body.subject,
        created_at=now,
    )
    store.append_consult_message(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )

    # Forward to the recipient L2 if cross-L2. Both sides log = the
    # routing-through-L2 corporate-IP audit point per decisions/10.
    if target_peer is not None:
        _forward_request(target_peer, {
            "thread_id": thread_id,
            "message_id": msg_id,
            "from_l2_id": self_l2_id,
            "from_persona": self_persona,
            "to_l2_id": body.to_l2_id,
            "to_persona": body.to_persona,
            "subject": body.subject,
            "content": body.content,
            "created_at": now,
        })

    row = store.get_consult(thread_id)
    assert row is not None  # just inserted
    return _to_thread_out(row)


@router.post("/{thread_id}/messages", response_model=ConsultMessageOut, status_code=201)
def post_consult_message(
    thread_id: str,
    body: ConsultMessage,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultMessageOut:
    """Append a message to an existing thread.

    The caller must be one of the two participants (from_persona or
    to_persona on the matching L2). Closed threads reject with 409.
    """
    self_l2_id, self_persona = _self_identity(store, username)
    thread = store.get_consult(thread_id)
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
        target_peer = _resolve_peer(store, other_l2_id)
        if target_peer is None:
            raise HTTPException(
                status_code=502,
                detail=f"peer L2 {other_l2_id!r} not in AIGRP peer table",
            )

    store.append_consult_message(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )

    if target_peer is not None:
        _forward_message(target_peer, {
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
        })
        # Kept side-note: cross-L2 unused var lint guard
        del other_persona

    return ConsultMessageOut(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )


@router.get("/{thread_id}/messages", response_model=MessagesResponse)
def get_consult_messages(
    thread_id: str,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> MessagesResponse:
    """Read every message on a thread the caller participates in."""
    self_l2_id, self_persona = _self_identity(store, username)
    thread = store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")
    msgs = store.list_consult_messages(thread_id)
    return MessagesResponse(
        thread_id=thread_id,
        messages=[ConsultMessageOut(**m) for m in msgs],
    )


@router.post("/{thread_id}/close", response_model=ConsultThreadOut)
def close_consult(
    thread_id: str,
    body: CloseRequest,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> ConsultThreadOut:
    """Mark the thread closed. Either participant can close."""
    self_l2_id, self_persona = _self_identity(store, username)
    thread = store.get_consult(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    is_from = thread["from_l2_id"] == self_l2_id and thread["from_persona"] == self_persona
    is_to = thread["to_l2_id"] == self_l2_id and thread["to_persona"] == self_persona
    if not (is_from or is_to):
        raise HTTPException(status_code=403, detail="not a participant in this thread")

    closed = store.close_consult(
        thread_id=thread_id,
        closed_at=_now_iso(),
        resolution_summary=body.resolution_summary,
    )
    if not closed:
        # Already closed; just return current state without erroring
        logger.info("close_consult on already-closed thread_id=%s", thread_id)
    row = store.get_consult(thread_id)
    assert row is not None
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
    to_persona: str
    subject: str | None = None
    content: str
    created_at: str


@router.post("/forward-request", status_code=201)
def forward_consult_request(
    body: ForwardRequestBody,
    store: RemoteStore = Depends(get_store),
    _peer: None = Depends(aigrp_mod.require_peer_key),
) -> dict[str, str]:
    """Mirror a remote consult request onto this L2.

    Called by a sibling L2 in the same Enterprise after the originating
    side did its local mirror-write. We re-do the same writes here so
    BOTH L2s have the durable corporate-IP record per decisions/10.

    Idempotent on thread_id collision: if the thread already exists
    (re-delivery, retry), we skip the create and just append the message
    if it's new. Same for message_id.
    """
    if store.get_consult(body.thread_id) is None:
        store.create_consult(
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
        store.append_consult_message(
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
    content: str
    created_at: str
    # Defensive: thread metadata so the receiver can lazily backfill if
    # the original /forward-request was lost.
    thread_subject: str | None = None
    thread_to_l2_id: str | None = None
    thread_to_persona: str | None = None
    thread_from_l2_id: str | None = None
    thread_from_persona: str | None = None
    thread_created_at: str | None = None


@router.post("/forward-message", status_code=201)
def forward_consult_message(
    body: ForwardMessageBody,
    store: RemoteStore = Depends(get_store),
    _peer: None = Depends(aigrp_mod.require_peer_key),
) -> dict[str, str]:
    """Mirror a reply onto this L2.

    If the thread row is missing (e.g. /forward-request was lost), we
    create it from the embedded thread metadata before appending. This
    prevents a single dropped forward from permanently breaking the
    audit trail on this side.
    """
    existing = store.get_consult(body.thread_id)
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
        store.create_consult(
            thread_id=body.thread_id,
            from_l2_id=body.thread_from_l2_id,
            from_persona=body.thread_from_persona,
            to_l2_id=body.thread_to_l2_id,
            to_persona=body.thread_to_persona,
            subject=body.thread_subject,
            created_at=body.thread_created_at,
        )
    try:
        store.append_consult_message(
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


@router.get("/inbox", response_model=InboxResponse)
def get_inbox(
    include_closed: bool = False,
    limit: int = 50,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> InboxResponse:
    """Threads addressed to the caller on this L2.

    Default excludes closed threads. `include_closed=true` returns the
    audit view (all threads, sorted by created_at DESC).
    """
    self_l2_id, self_persona = _self_identity(store, username)
    rows = store.list_inbox(
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
