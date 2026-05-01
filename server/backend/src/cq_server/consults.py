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
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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

    # Same-L2 guard. Cross-L2 routing comes next.
    if body.to_l2_id != self_l2_id:
        raise HTTPException(
            status_code=501,
            detail=(
                "cross-L2 consult routing is on the roadmap (issue #20 next PR); "
                "this PR ships same-L2 only. "
                f"This L2 is {self_l2_id!r}, request targeted {body.to_l2_id!r}."
            ),
        )

    thread_id = f"th_{uuid4().hex[:16]}"
    now = _now_iso()
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
        message_id=f"msg_{uuid4().hex[:16]}",
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
    )
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
    store.append_consult_message(
        message_id=msg_id,
        thread_id=thread_id,
        from_l2_id=self_l2_id,
        from_persona=self_persona,
        content=body.content,
        created_at=now,
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
