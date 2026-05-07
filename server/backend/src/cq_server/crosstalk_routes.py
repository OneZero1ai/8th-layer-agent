"""Crosstalk endpoints (#124) — L2-mediated inter-session messaging.

Re-scoped 2026-05-07: L2-mediated crosstalk is a TeamDW MVP requirement,
not deferrable to Phase B. Reasons (from Plan 19 v4):

- **Visibility/audit** — legacy claude-mux SQLite crosstalk is invisible
  to the L2; activity log captures KU events but not inter-session
  messaging. Acme-style multi-operator deployments need this.
- **Multi-tenant isolation** — laptop-local SQLite has no enterprise/
  group scoping; messages between sessions can cross persona boundaries
  trivially. L2 routing enforces tenancy.
- **Cross-account routing (eventual)** — cross-Enterprise consults
  already flow through L2 peering envelopes (Phase 0/1). Crosstalk
  needs the same shape eventually; the L2 endpoint is the foundation.

This module mirrors ``activity_routes.py``'s auth pattern (admin-or-
participant scoping). Migration 0014 created the tables; store helpers
on ``SqliteStore`` provide the persistence surface; this module is the
HTTP layer.

# Auth model

Same as activity_routes: ``Depends(get_current_user)`` accepts both JWT
and API-key bearer tokens. The route layer pins tenancy from the
authenticated caller's user row, never from the request body.

# Endpoint surface (per ``docs/plans/22-crosstalk-l2-endpoints-design.md``)

- ``POST /crosstalk/messages`` — send a message; creates new thread if
  ``thread_id`` is absent
- ``GET /crosstalk/threads`` — list threads visible to caller
- ``GET /crosstalk/threads/{id}`` — thread metadata + messages
- ``POST /crosstalk/threads/{id}/messages`` — reply on existing thread
- ``POST /crosstalk/threads/{id}/close`` — mark thread closed
- ``GET /crosstalk/inbox`` — caller's unread messages

Activity log: writes log ``crosstalk_send`` / ``crosstalk_reply`` /
``crosstalk_close`` events with ``thread_or_chain_id=<thread_id>`` for
audit correlation. Reads (list/get/inbox) are not logged.

# Multi-party (deferred)

V1 is two-party. The ``participants`` JSON list on the thread accepts
a list of usernames; v1 always populates it as ``[creator, recipient]``.
Phase 7 will add a ``POST /crosstalk/threads/{id}/participants`` endpoint
that lets thread members add others, plus the corresponding ``group``
shape from the universe (Pass 2 Part 2 Ch 8).
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .activity_logger import log_activity
from .auth import get_current_user
from .deps import get_store
from .store._sqlite import SqliteStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crosstalk", tags=["crosstalk"])

_LIMIT_MAX = 200


# ============================================================================
# Wire shapes
# ============================================================================


class SendMessageRequest(BaseModel):
    """Send a message; creates new thread if ``thread_id`` is absent."""

    to: str = Field(..., description="Recipient username")
    content: str = Field(..., min_length=1)
    persona: str | None = Field(default=None, description="Optional persona attribution for the message")
    subject: str = Field(default="", description="Initial thread subject (used only on new-thread create)")


class ReplyRequest(BaseModel):
    """Reply on an existing thread."""

    content: str = Field(..., min_length=1)
    persona: str | None = Field(default=None)


class CloseRequest(BaseModel):
    """Mark a thread closed."""

    reason: str | None = Field(default=None)


class CrosstalkMessage(BaseModel):
    """Wire shape for one crosstalk_messages row."""

    id: str
    thread_id: str
    from_username: str
    from_persona: str | None
    to_username: str | None
    content: str
    sent_at: str
    read_at: str | None = None


class CrosstalkThread(BaseModel):
    """Wire shape for one crosstalk_threads row."""

    id: str
    subject: str
    status: str
    closed_at: str | None
    closed_by_username: str | None
    closed_reason: str | None
    enterprise_id: str
    group_id: str
    created_at: str
    created_by_username: str
    participants: list[str]


class ThreadSummary(BaseModel):
    """Compact thread shape for list endpoints."""

    id: str
    subject: str
    status: str
    created_at: str
    created_by_username: str
    participants: list[str]


class ThreadListResponse(BaseModel):
    """Response envelope for GET /crosstalk/threads."""

    items: list[ThreadSummary]
    count: int


class ThreadWithMessagesResponse(BaseModel):
    """Response envelope for GET /crosstalk/threads/{id}."""

    thread: CrosstalkThread
    messages: list[CrosstalkMessage]


class InboxResponse(BaseModel):
    """Response envelope for GET /crosstalk/inbox."""

    items: list[CrosstalkMessage]
    count: int


class SendResponse(BaseModel):
    """Response envelope for POST /crosstalk/messages and reply."""

    thread_id: str
    message_id: str
    sent_at: str


# ============================================================================
# Helpers
# ============================================================================


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_thread_id() -> str:
    return f"thread_{secrets.token_hex(16)}"


def _new_message_id() -> str:
    return f"msg_{secrets.token_hex(16)}"


async def _resolve_caller(
    username: str, store: SqliteStore
) -> tuple[str, str, str]:
    """Return (enterprise_id, group_id, role) from caller's user row.

    Raises 401 if the user row is missing tenancy claims (defensive,
    same shape as ``propose_unit``).
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    enterprise_id = user["enterprise_id"]
    group_id = user["group_id"]
    if not enterprise_id or not group_id:
        raise HTTPException(
            status_code=500,
            detail="User row missing tenancy claims; refusing to proceed",
        )
    return enterprise_id, group_id, user.get("role") or "user"


def _summary_first_60(text: str) -> str:
    return text[:60]


# ============================================================================
# Endpoints
# ============================================================================


@router.post("/messages", response_model=SendResponse, status_code=201)
async def send_message(
    request: SendMessageRequest,
    background_tasks: BackgroundTasks,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> SendResponse:
    """Send a message. Creates a new thread between caller + recipient."""
    enterprise_id, group_id, _role = await _resolve_caller(username, store)

    # Verify recipient exists in the same tenant
    recipient = await store.get_user(request.to)
    if recipient is None or recipient.get("enterprise_id") != enterprise_id:
        raise HTTPException(
            status_code=404,
            detail=f"Recipient '{request.to}' not found in this Enterprise",
        )

    now = _now_iso()
    thread_id = _new_thread_id()
    message_id = _new_message_id()

    await store.create_crosstalk_thread(
        thread_id=thread_id,
        subject=request.subject,
        enterprise_id=enterprise_id,
        group_id=group_id,
        created_at=now,
        created_by_username=username,
        participants=[username, request.to],
    )
    await store.append_crosstalk_message(
        message_id=message_id,
        thread_id=thread_id,
        from_username=username,
        from_persona=request.persona,
        to_username=request.to,
        content=request.content,
        sent_at=now,
        enterprise_id=enterprise_id,
        group_id=group_id,
    )

    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="crosstalk_send",
        payload={
            "thread_id": thread_id,
            "message_id": message_id,
            "to_username": request.to,
            "persona": request.persona,
            "content_first_60_chars": _summary_first_60(request.content),
        },
        thread_or_chain_id=thread_id,
    )

    return SendResponse(thread_id=thread_id, message_id=message_id, sent_at=now)


@router.post("/threads/{thread_id}/messages", response_model=SendResponse, status_code=201)
async def reply_on_thread(
    thread_id: str,
    request: ReplyRequest,
    background_tasks: BackgroundTasks,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> SendResponse:
    """Reply on an existing thread. Caller must be a participant."""
    enterprise_id, group_id, role = await _resolve_caller(username, store)

    thread = await store.get_crosstalk_thread(
        thread_id=thread_id, tenant_enterprise=enterprise_id
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["status"] != "open":
        raise HTTPException(status_code=409, detail="Thread is closed")
    if username not in thread["participants"] and role != "admin":
        raise HTTPException(status_code=403, detail="Not a participant")

    # Pick the recipient — for two-party threads, it's the other participant.
    others = [p for p in thread["participants"] if p != username]
    to_username = others[0] if others else None

    now = _now_iso()
    message_id = _new_message_id()

    await store.append_crosstalk_message(
        message_id=message_id,
        thread_id=thread_id,
        from_username=username,
        from_persona=request.persona,
        to_username=to_username,
        content=request.content,
        sent_at=now,
        enterprise_id=enterprise_id,
        group_id=group_id,
    )

    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="crosstalk_reply",
        payload={
            "thread_id": thread_id,
            "message_id": message_id,
            "to_username": to_username,
            "persona": request.persona,
            "content_first_60_chars": _summary_first_60(request.content),
        },
        thread_or_chain_id=thread_id,
    )

    return SendResponse(thread_id=thread_id, message_id=message_id, sent_at=now)


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    limit: int = Query(default=20, gt=0, le=_LIMIT_MAX),
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> ThreadListResponse:
    """List threads visible to caller. Admin sees all in tenant; user sees own."""
    enterprise_id, _group_id, role = await _resolve_caller(username, store)

    rows = await store.list_crosstalk_threads_for_user(
        username=username,
        tenant_enterprise=enterprise_id,
        is_admin=(role == "admin"),
        limit=limit,
    )
    items = [ThreadSummary(**r) for r in rows]
    return ThreadListResponse(items=items, count=len(items))


@router.get("/threads/{thread_id}", response_model=ThreadWithMessagesResponse)
async def get_thread(
    thread_id: str,
    limit: int = Query(default=50, gt=0, le=_LIMIT_MAX),
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> ThreadWithMessagesResponse:
    """Fetch one thread + its messages."""
    enterprise_id, _group_id, role = await _resolve_caller(username, store)

    thread = await store.get_crosstalk_thread(
        thread_id=thread_id, tenant_enterprise=enterprise_id
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if username not in thread["participants"] and role != "admin":
        raise HTTPException(status_code=403, detail="Not a participant")

    msgs = await store.list_crosstalk_messages(
        thread_id=thread_id, tenant_enterprise=enterprise_id, limit=limit
    )
    return ThreadWithMessagesResponse(
        thread=CrosstalkThread(**thread),
        messages=[CrosstalkMessage(**m) for m in msgs],
    )


@router.post("/threads/{thread_id}/close", status_code=200)
async def close_thread(
    thread_id: str,
    request: CloseRequest,
    background_tasks: BackgroundTasks,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    """Mark a thread closed. Caller must be a participant or admin."""
    enterprise_id, _group_id, role = await _resolve_caller(username, store)

    thread = await store.get_crosstalk_thread(
        thread_id=thread_id, tenant_enterprise=enterprise_id
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if username not in thread["participants"] and role != "admin":
        raise HTTPException(status_code=403, detail="Not a participant")

    won = await store.close_crosstalk_thread(
        thread_id=thread_id,
        closed_by_username=username,
        closed_at=_now_iso(),
        reason=request.reason,
        tenant_enterprise=enterprise_id,
    )
    if not won:
        raise HTTPException(status_code=409, detail="Thread already closed")

    background_tasks.add_task(
        log_activity,
        store,
        username=username,
        event_type="crosstalk_close",
        payload={
            "thread_id": thread_id,
            "reason": request.reason,
        },
        thread_or_chain_id=thread_id,
    )

    return {"thread_id": thread_id, "status": "closed"}


@router.get("/inbox", response_model=InboxResponse)
async def inbox(
    limit: int = Query(default=50, gt=0, le=_LIMIT_MAX),
    mark_read: bool = Query(
        default=False,
        description="If true, atomically populate read_at on returned messages",
    ),
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> InboxResponse:
    """Caller's unread messages, oldest first."""
    enterprise_id, _group_id, _role = await _resolve_caller(username, store)

    rows = await store.crosstalk_inbox_for_user(
        username=username,
        tenant_enterprise=enterprise_id,
        limit=limit,
        mark_read=mark_read,
        read_at_iso=_now_iso() if mark_read else None,
    )
    items = [CrosstalkMessage(**r) for r in rows]
    return InboxResponse(items=items, count=len(items))
