"""Activity-log read endpoint (#108 Stage 2 — Workstream D).

Stage 1 shipped the schema + write helper. Stage 2 wires every existing
write-path handler to append rows; *this* module is the read side —
``GET /api/v1/activity`` with admin-or-self auth scoping and cursor
pagination.

Auth model:

* Admin callers can read any persona's events within their Enterprise.
  Use ``persona=<name>`` to filter; omit it to see everyone.
* Non-admin callers can only see events tagged with their own persona
  (the username from their auth claims). Any persona query parameter
  they send is forced to their own username before the store query —
  no enumeration oracle, no cross-persona leak.

Tenancy is mandatory: the route layer always pins
``tenant_enterprise`` to the caller's Enterprise. Cross-Enterprise
visibility is impossible by construction; foreign-Enterprise consults
already log on both ends per the consult logging policy.

Cursor:

* Each response includes ``next_cursor`` when a full page returned;
  callers re-send it as ``cursor=<value>`` for the next page.
* Encoded as ``<ts>|<id>`` — opaque to the client, just round-trip it.
* Decoder accepts the historical empty value as "no cursor".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .activity import EVENT_TYPES
from .auth import get_current_user, scope_filter
from .deps import get_store
from .store._sqlite import SqliteStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/activity", tags=["activity"])

_CURSOR_SEP = "|"
# Hard ceiling protects the read endpoint from a caller asking for ten
# million rows in one page; mirrors the ``le=500`` bound on /consents.
_LIMIT_MAX = 500


class ActivityRow(BaseModel):
    """Public wire shape for one ``activity_log`` row."""

    id: str
    ts: str
    tenant_enterprise: str
    tenant_group: str | None
    persona: str | None
    human: str | None
    event_type: str
    payload: dict[str, Any]
    result_summary: dict[str, Any] | None
    thread_or_chain_id: str | None


class ActivityListResponse(BaseModel):
    """Paginated list of activity rows + opaque cursor for the next page."""

    items: list[ActivityRow]
    count: int
    next_cursor: str | None


def _encode_cursor(ts: str, id_: str) -> str:
    return f"{ts}{_CURSOR_SEP}{id_}"


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Parse a ``ts|id`` cursor back into the store-helper tuple shape.

    Returns None when ``cursor`` is None / empty. Raises 400 when the
    cursor is malformed — a malformed cursor is a client bug, not a
    "no more results" signal.
    """
    if not cursor:
        return None
    if _CURSOR_SEP not in cursor:
        raise HTTPException(
            status_code=400,
            detail=f"malformed cursor {cursor!r}; expected '<ts>{_CURSOR_SEP}<id>'",
        )
    ts, id_ = cursor.split(_CURSOR_SEP, 1)
    if not ts or not id_:
        raise HTTPException(status_code=400, detail="cursor parts must be non-empty")
    return ts, id_


def _normalise_iso(value: str | None, *, field_name: str) -> str | None:
    """Validate that ``value`` parses as ISO-8601; return it as-is.

    The store column stores ISO-8601 strings rather than timestamps,
    and SQLite compares them lexicographically — which works because
    ISO-8601 with a fixed timezone offset is monotone in lexicographic
    order. We accept the value verbatim once it parses; do NOT
    canonicalise to UTC because that would shift the comparison
    boundary by a timezone offset and miss rows.
    """
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} is not a valid ISO-8601 timestamp: {value!r}",
        ) from exc
    return value


@router.get("")
async def list_activity(
    persona: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO-8601 lower bound (inclusive)"),
    until: str | None = Query(default=None, description="ISO-8601 upper bound (exclusive)"),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=50, gt=0, le=_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> ActivityListResponse:
    """Read activity-log rows with admin-or-self auth scoping.

    Authorization tiers:

    * **Admin caller** — ``persona`` query param applied as-is; omit it
      to see every persona within the caller's Enterprise.
    * **Non-admin caller** — ``persona`` is forced to the caller's
      own username regardless of what they sent. A non-admin who
      asked for ``persona=alice`` while authenticated as Bob will see
      Bob's events only. Silent override (rather than a 403) — the
      route can't tell whether the caller meant to spoof or just
      misunderstood the contract; either way the right answer is
      "your own activity".

    Cursor pagination on ``(ts DESC, id DESC)``. ``next_cursor`` is
    populated when the page returned exactly ``limit`` rows — the
    last row's ``ts|id`` becomes the next page's anchor. A null
    ``next_cursor`` means "no more rows".

    422 on malformed timestamps; 400 on malformed cursor; 401 on auth
    failure (raised by the chained ``get_current_user`` dep).
    """
    user = await store.get_user(username)
    if user is None:
        # Auth accepted the bearer but the user row vanished. Same shape
        # as the ``propose_unit`` defensive 401.
        raise HTTPException(status_code=401, detail="User not found")
    # Decision 27: under PER_L2_ISOLATION the activity log read tightens
    # to ``(tenant_enterprise, tenant_group)``. The base index covers
    # both columns + ts so the new clause is index-friendly. Admin
    # callers stay scoped per-L2 — directory federation is the
    # Enterprise-level oversight surface, not /activity.
    enterprise_id, group_id = scope_filter(enterprise_id=user["enterprise_id"], group_id=user.get("group_id"))
    role = user.get("role") or "user"

    # Scope the persona filter:
    #   - admin: pass through whatever they sent (including None for
    #     "all personas in this Enterprise")
    #   - non-admin: pin to their own username regardless of input
    if role == "admin":
        effective_persona = persona
    else:
        effective_persona = username
        if persona is not None and persona != username:
            logger.info(
                "activity: non-admin %s asked for persona=%s; forced to self",
                username,
                persona,
            )

    if event_type is not None and event_type not in EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(f"unknown event_type {event_type!r}; expected one of {sorted(EVENT_TYPES)}"),
        )

    since_iso = _normalise_iso(since, field_name="since")
    until_iso = _normalise_iso(until, field_name="until")
    cursor_tuple = _decode_cursor(cursor)

    rows = await store.list_activity(
        tenant_enterprise=enterprise_id,
        tenant_group=group_id,
        persona=effective_persona,
        since_iso=since_iso,
        until_iso=until_iso,
        event_type=event_type,
        limit=limit,
        cursor=cursor_tuple,
    )

    items = [ActivityRow(**row) for row in rows]
    next_cursor = _encode_cursor(items[-1].ts, items[-1].id) if len(items) == limit and items else None
    # Defensive: if `len(items) == limit` but we know there aren't more
    # rows, the next call returns empty and the second-to-last cursor
    # naturally terminates. Cheap one extra round-trip on the boundary
    # vs the cost of a count(*) query on every page.

    return ActivityListResponse(
        items=items,
        count=len(items),
        next_cursor=next_cursor,
    )
