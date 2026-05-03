"""Reputation reader endpoints (task #108 sub-tasks 6 + admin trigger).

Two read-only endpoints under ``/reputation/...``:

- ``GET /reputation/events`` — paginated event stream, JWT-gated,
  scoped to the caller's Enterprise. The signature columns are
  surfaced so external verifiers can re-check signatures without
  having to query the events table directly.
- ``GET /reputation/roots`` — daily Merkle roots, same scoping.

Plus an admin-only ``POST /reputation/roots/compute`` that triggers
a same-day or yesterday root computation immediately. Useful for
testing + recovery from a missed cron tick.

Authn pattern mirrors review.py: JWT bearer via ``get_current_user``.
The Enterprise scope is resolved from the user's row — same logic
as ``_admin_enterprise`` but available to any authenticated user
(reading your own Enterprise's reputation chain isn't admin-gated).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import get_current_user, require_admin
from .deps import get_store
from .store import RemoteStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reputation", tags=["reputation"])


class EventOut(BaseModel):
    """One row of the reputation event log surfaced to authenticated readers."""

    event_id: str
    event_type: str
    enterprise_id: str
    l2_id: str
    ts: str
    prev_event_hash: str
    payload_canonical: str
    payload_hash: str
    signature_b64u: str | None
    signing_key_id: str | None
    created_at: str


class EventsResponse(BaseModel):
    """Paginated response for ``GET /reputation/events``."""

    events: list[EventOut]
    total: int
    limit: int
    offset: int


class RootOut(BaseModel):
    """One persisted daily Merkle root surfaced to authenticated readers."""

    enterprise_id: str
    root_date: str
    event_count: int
    merkle_root_hash: str
    first_event_id: str | None
    last_event_id: str | None
    signature_b64u: str | None
    signing_key_id: str | None
    computed_at: str
    published_to_directory_at: str | None


class RootsResponse(BaseModel):
    """Response for ``GET /reputation/roots`` — newest-first list of daily roots."""

    roots: list[RootOut]
    total: int


def _user_enterprise(username: str, store: RemoteStore) -> str:
    """Resolve the authenticated user's Enterprise id.

    Raises 403 if the user has no Enterprise (defensive — every user
    row has one in practice, but a stale row would otherwise leak
    cross-tenant data on the GET).
    """
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=403, detail="user not found")
    enterprise_id = user.get("enterprise_id")
    if not enterprise_id:
        raise HTTPException(status_code=403, detail="user has no enterprise scope")
    return enterprise_id


@router.get("/events", response_model=EventsResponse)
def list_reputation_events(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> EventsResponse:
    """Return reputation events for the caller's Enterprise.

    Newest-first by ``ts`` — pagination via offset/limit. Optional
    ``event_type`` filter narrows to one of ``consult.closed``,
    ``ku.event``, ``peer.heartbeat``.

    The full ``payload_canonical`` is returned so verifiers can
    re-derive ``payload_hash`` and re-verify the signature locally
    without trusting the server's stored values.
    """
    enterprise_id = _user_enterprise(username, store)

    base = (
        "SELECT event_id, event_type, enterprise_id, l2_id, ts, "
        "prev_event_hash, payload_canonical, payload_hash, "
        "signature_b64u, signing_key_id, created_at "
        "FROM reputation_events WHERE enterprise_id = ?"
    )
    count_base = "SELECT COUNT(*) FROM reputation_events WHERE enterprise_id = ?"
    params: list[Any] = [enterprise_id]
    count_params: list[Any] = [enterprise_id]
    if event_type is not None:
        base += " AND event_type = ?"
        count_base += " AND event_type = ?"
        params.append(event_type)
        count_params.append(event_type)
    base += " ORDER BY ts DESC, event_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with store._lock:
        rows = store._conn.execute(base, params).fetchall()
        total = store._conn.execute(count_base, count_params).fetchone()[0]

    events = [
        EventOut(
            event_id=r[0],
            event_type=r[1],
            enterprise_id=r[2],
            l2_id=r[3],
            ts=r[4],
            prev_event_hash=r[5],
            payload_canonical=r[6],
            payload_hash=r[7],
            signature_b64u=r[8],
            signing_key_id=r[9],
            created_at=r[10],
        )
        for r in rows
    ]
    return EventsResponse(events=events, total=total, limit=limit, offset=offset)


@router.get("/roots", response_model=RootsResponse)
def list_reputation_roots(
    limit: int = Query(default=90, ge=1, le=365),
    store: RemoteStore = Depends(get_store),
    username: str = Depends(get_current_user),
) -> RootsResponse:
    """Return daily Merkle roots for the caller's Enterprise, newest first."""
    enterprise_id = _user_enterprise(username, store)

    with store._lock:
        rows = store._conn.execute(
            """
            SELECT enterprise_id, root_date, event_count, merkle_root_hash,
                   first_event_id, last_event_id, signature_b64u, signing_key_id,
                   computed_at, published_to_directory_at
            FROM reputation_roots
            WHERE enterprise_id = ?
            ORDER BY root_date DESC
            LIMIT ?
            """,
            (enterprise_id, limit),
        ).fetchall()
        total = store._conn.execute(
            "SELECT COUNT(*) FROM reputation_roots WHERE enterprise_id = ?",
            (enterprise_id,),
        ).fetchone()[0]

    roots = [
        RootOut(
            enterprise_id=r[0],
            root_date=r[1],
            event_count=r[2],
            merkle_root_hash=r[3],
            first_event_id=r[4],
            last_event_id=r[5],
            signature_b64u=r[6],
            signing_key_id=r[7],
            computed_at=r[8],
            published_to_directory_at=r[9],
        )
        for r in rows
    ]
    return RootsResponse(roots=roots, total=total)


class ComputeRootRequest(BaseModel):
    """Request body for the admin-only ``POST /reputation/roots/compute`` trigger."""

    root_date: str  # YYYY-MM-DD


@router.post("/roots/compute", response_model=RootOut)
def compute_root_now(
    body: ComputeRootRequest,
    store: RemoteStore = Depends(get_store),
    username: str = Depends(require_admin),
) -> RootOut:
    """Admin trigger: compute the Merkle root for one specific UTC day.

    Idempotent — returns the existing row if already computed. Useful
    for filling gaps from a missed daily cron tick, and for tests that
    need a deterministic root without waiting for midnight.
    """
    from .daily_root import compute_root_for_day

    enterprise_id = _user_enterprise(username, store)
    with store._lock:
        result = compute_root_for_day(store._conn, enterprise_id, body.root_date)
        store._conn.commit()
    return RootOut(**result)
