"""Review queue endpoints for the review API.

SEC-CRIT #32 — every route requires admin role and is scoped to the
caller's Enterprise. Tenant scope is resolved from the user row, never
the request, matching the pattern used by /peers/heartbeat.
"""

from cq.models import KnowledgeUnit
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .deps import get_store
from .store._sqlite import SqliteStore


async def _admin_enterprise(username: str, store: SqliteStore) -> str:
    """Resolve the admin caller's enterprise_id from the user row.

    Raises 401 if the row vanished between auth and request handling
    (revoked admin, race with delete, etc.).
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user["enterprise_id"]


class ReviewItem(BaseModel):
    """A KU with its review metadata."""

    knowledge_unit: KnowledgeUnit
    status: str
    reviewed_by: str | None
    reviewed_at: str | None


class ReviewQueueResponse(BaseModel):
    """Paginated review queue response."""

    items: list[ReviewItem]
    total: int
    offset: int
    limit: int


class ReviewDecisionResponse(BaseModel):
    """Response after approving or rejecting a KU."""

    unit_id: str
    status: str
    reviewed_by: str
    reviewed_at: str


class DailyCount(BaseModel):
    """Daily proposal, approval, and rejection counts."""

    date: str
    proposed: int
    approved: int
    rejected: int


class TrendsResponse(BaseModel):
    """Trend data for the dashboard chart."""

    daily: list[DailyCount]


class ReviewStatsResponse(BaseModel):
    """Dashboard metrics response."""

    counts: dict[str, int]
    domains: dict[str, int]
    confidence_distribution: dict[str, int]
    recent_activity: list[dict]
    trends: TrendsResponse


def _build_decision(unit_id: str, row: dict[str, str | None]) -> ReviewDecisionResponse:
    """Build a ReviewDecisionResponse from a review status row.

    All fields are guaranteed non-None after set_review_status, so we assert
    rather than silently defaulting.
    """
    status = row["status"]
    reviewed_by = row["reviewed_by"]
    reviewed_at = row["reviewed_at"]
    assert status is not None
    assert reviewed_by is not None
    assert reviewed_at is not None
    return ReviewDecisionResponse(
        unit_id=unit_id,
        status=status,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )


router = APIRouter(prefix="/review", tags=["review"])


@router.get("/queue")
async def review_queue(
    limit: int = 20,
    offset: int = 0,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ReviewQueueResponse:
    """Return pending KUs for review, scoped to the caller's Enterprise."""
    enterprise_id = _admin_enterprise(username, store)
    items = await store.pending_queue(limit=limit, offset=offset, enterprise_id=enterprise_id)
    total = await store.pending_count(enterprise_id=enterprise_id)
    return ReviewQueueResponse(
        items=[
            ReviewItem(
                knowledge_unit=item["knowledge_unit"],
                status=item["status"],
                reviewed_by=item["reviewed_by"],
                reviewed_at=item["reviewed_at"],
            )
            for item in items
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


def _hook_ku_event(store: "SqliteStore", unit_id: str, verb: str, enterprise_id: str, by: str) -> None:
    """Reputation hook for KU lifecycle transitions (#108 sub-task 5).

    Best-effort: ``record_event`` swallows on failure so a flaky
    reputation chain never blocks the underlying review action. Body
    shape per ``reputation-v1.md`` §"ku.event".
    """
    from .reputation import record_event as _record_event

    _record_event(
        store._conn,
        event_type="ku.event",
        body={
            "unit_id": unit_id,
            "verb": verb,
            "enterprise_id": enterprise_id,
            "by": by,
        },
        enterprise_id=enterprise_id,
    )


@router.post("/{unit_id}/approve")
async def approve_unit(
    unit_id: str,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ReviewDecisionResponse:
    """Approve a pending KU in the admin's Enterprise."""
    enterprise_id = _admin_enterprise(username, store)
    status = await store.get_review_status(unit_id, enterprise_id=enterprise_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    if status["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Knowledge unit already {status['status']}")
    await store.set_review_status(unit_id, "approved", username, enterprise_id=enterprise_id)
    updated = await store.get_review_status(unit_id, enterprise_id=enterprise_id)
    assert updated is not None  # Unit exists; we just wrote to it.
    _hook_ku_event(store, unit_id, "approve", enterprise_id, username)
    return _build_decision(unit_id, updated)


@router.post("/{unit_id}/reject")
async def reject_unit(
    unit_id: str,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ReviewDecisionResponse:
    """Reject a pending KU in the admin's Enterprise."""
    enterprise_id = _admin_enterprise(username, store)
    status = await store.get_review_status(unit_id, enterprise_id=enterprise_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    if status["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Knowledge unit already {status['status']}")
    await store.set_review_status(unit_id, "rejected", username, enterprise_id=enterprise_id)
    updated = await store.get_review_status(unit_id, enterprise_id=enterprise_id)
    assert updated is not None  # Unit exists; we just wrote to it.
    _hook_ku_event(store, unit_id, "reject", enterprise_id, username)
    return _build_decision(unit_id, updated)


@router.delete("/{unit_id}", status_code=204)
async def delete_unit(
    unit_id: str,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> None:
    """Hard-delete a KU in the admin's Enterprise (irreversible).

    Cross-tenant DELETEs return 404 — same shape as missing-id, so
    enumeration probes can't fingerprint other tenants' KU IDs.
    """
    enterprise_id = _admin_enterprise(username, store)
    deleted = await store.delete(unit_id, enterprise_id=enterprise_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    return None


@router.get("/stats")
async def review_stats(
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ReviewStatsResponse:
    """Return dashboard metrics, scoped to the caller's Enterprise."""
    enterprise_id = _admin_enterprise(username, store)
    counts = await store.counts_by_status(enterprise_id=enterprise_id)
    return ReviewStatsResponse(
        counts={
            "pending": counts.get("pending", 0),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
        },
        domains=await store.domain_counts(enterprise_id=enterprise_id),
        confidence_distribution=await store.confidence_distribution(enterprise_id=enterprise_id),
        recent_activity=await store.recent_activity(enterprise_id=enterprise_id),
        trends=TrendsResponse(
            daily=[DailyCount(**d) for d in await store.daily_counts(enterprise_id=enterprise_id)],
        ),
    )


@router.get("/units")
async def list_units(
    domain: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    status: str | None = None,
    limit: int = 100,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> list[ReviewItem]:
    """List KUs in the admin's Enterprise, filtered by domain/confidence/status."""
    enterprise_id = _admin_enterprise(username, store)
    items = await store.list_units(
        domain=domain,
        confidence_min=confidence_min,
        confidence_max=confidence_max,
        status=status,
        limit=limit,
        enterprise_id=enterprise_id,
    )
    return [
        ReviewItem(
            knowledge_unit=item["knowledge_unit"],
            status=item["status"],
            reviewed_by=item["reviewed_by"],
            reviewed_at=item["reviewed_at"],
        )
        for item in items
    ]


@router.get("/{unit_id}")
async def get_unit(
    unit_id: str,
    username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ReviewItem:
    """Return one KU's review row, scoped to the admin's Enterprise.

    Cross-tenant GETs return 404 — same shape as missing-id.
    """
    enterprise_id = _admin_enterprise(username, store)
    ku = await store.get_any(unit_id, enterprise_id=enterprise_id)
    if ku is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    review = await store.get_review_status(unit_id, enterprise_id=enterprise_id)
    assert review is not None  # Unit exists; get_any just returned it.
    return ReviewItem(
        knowledge_unit=ku,
        status=review["status"] or "pending",
        reviewed_by=review["reviewed_by"],
        reviewed_at=review["reviewed_at"],
    )
