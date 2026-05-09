"""Admin routes — xgroup_consent propose/cosign/ratify/revoke (Phase 1.0b).

Decision 28 §2.2 maps onto these endpoints:

  POST /api/v1/admin/xgroup_consent/propose
  GET  /api/v1/admin/xgroup_consent/pending
  POST /api/v1/admin/xgroup_consent/cosign/{pending_id}
  POST /api/v1/admin/xgroup_consent/ratify/{pending_id}
  POST /api/v1/admin/xgroup_consent/revoke/{grant_id}
  POST /api/v1/admin/xgroup_consent/recovery-revoke/{grant_id}

A note on auth shape: each step requires an admin's Ed25519 signature
over the canonical grant body (or revoke envelope). The bearer token
authenticates the *caller* via the standard ``require_admin`` ladder;
the bearer is necessary but NOT sufficient — the body-level signature
is the binding cryptographic act per the 2-of-2 design. ``require_admin``
gates who can hit the endpoint; the signature gates whose key actually
signed the grant.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import xgroup_consent as xgc
from .auth import require_admin
from .deps import get_store
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/xgroup_consent", tags=["admin", "xgroup-consent"])


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class GrantScope(BaseModel):
    """Scope envelope inside a grant body — see Decision 28 §2.1."""

    kind: str = Field(..., description="domains|topics|all")
    values: list[str] = Field(default_factory=list)


class GrantBody(BaseModel):
    """Mirrors ``xgroup_consent.build_grant_body`` output verbatim.

    Clients build the body locally with the helper, sign the canonical
    bytes, and POST both. The server canonicalises before verifying so
    a client that builds an unequal dict (extra whitespace, key ordering)
    still verifies — the canonical form is identical.
    """

    grant_id: str
    enterprise_id: str
    source_l2: str
    target_l2: str
    scope: GrantScope
    issued_at: str
    expires_at: str
    nonce: str
    version: str
    recovery_operator_pubkey_b64u: str


class ProposeRequest(BaseModel):
    """POST /propose body — first signer's grant proposal + signature."""

    body: GrantBody
    proposer_l2: str
    proposer_pubkey_b64u: str
    proposer_signature_b64u: str


class ProposeResponse(BaseModel):
    """Result of /propose — pending row id + cosign-window deadline."""

    pending_id: str
    grant_id: str
    status: str
    expires_for_cosign_at: str


class CosignRequest(BaseModel):
    """POST /cosign body — second signer attaches their signature."""

    cosigner_l2: str
    cosigner_pubkey_b64u: str
    cosigner_signature_b64u: str


class CosignResponse(BaseModel):
    """Result of /cosign — pending row state advanced to cosigned."""

    pending_id: str
    status: str
    cosigned_at: str


class RatifyRequest(BaseModel):
    """Optional inline-cosign convenience body for /ratify.

    All fields optional. When all four are present and the pending row
    is still in 'proposed' state, /ratify will cosign-then-promote in
    one transaction. When the row is already 'cosigned', /ratify
    ignores these fields and just promotes.
    """

    cosigner_l2: str | None = None
    cosigner_pubkey_b64u: str | None = None
    cosigner_signature_b64u: str | None = None


class RatifyResponse(BaseModel):
    """Result of /ratify — grant promoted to active xgroup_consent row."""

    grant_id: str
    status: str
    ratified_at: str


class RevokeRequest(BaseModel):
    """POST /revoke or /recovery-revoke body — signed revocation envelope."""

    revoker_l2: str | None = None
    revoker_pubkey_b64u: str
    revoker_signature_b64u: str
    revoke_ts: str
    reason: str | None = None


class RevokeResponse(BaseModel):
    """Result of /revoke — grant flipped to revoked + audit flag echoed."""

    grant_id: str
    status: str
    revoked_by_recovery: bool
    revoked_at: str


class PendingItem(BaseModel):
    """One row of /pending — proposal awaiting cosign or ratify."""

    pending_id: str
    grant_id: str
    enterprise_id: str
    source_l2: str
    target_l2: str
    body: dict[str, Any]
    body_canonical_sha256_hex: str
    proposer_l2: str
    proposer_pubkey_b64u: str
    proposer_signature_b64u: str
    status: str
    proposed_at: str
    expires_at: str


class PendingListResponse(BaseModel):
    """Paginated list of pending proposals targeted at this L2."""

    items: list[PendingItem]
    count: int


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for(exc: xgc.XGroupConsentError) -> HTTPException:
    code_map = {
        "not_found": 404,
        "conflict": 409,
        "invalid_signature": 403,
        "expired": 410,
        "bad_request": 400,
    }
    return HTTPException(status_code=code_map.get(exc.code, 400), detail=exc.detail)


async def _enforce_caller_tenancy(
    *,
    username: str,
    store: SqliteStore,
    expected_enterprise_id: str,
    expected_l2: str | None,
) -> None:
    """Verify the authenticated admin's user-row tenancy matches the request.

    Defence-in-depth on top of the body-level signature checks. ``require_admin``
    confirms role; this confirms the admin who hit the endpoint actually
    belongs to the Enterprise + Group identified in the request body or
    query params. Without this gate an admin in Enterprise A could submit
    a grant spec for Enterprise B (cryptography would still need a valid
    signature, but defence-in-depth shouldn't rely on a single layer).

    ``expected_l2`` is in Enterprise/Group form ("acme/sga"); we split
    against the user-row's ``group_id`` only when supplied. None means
    Enterprise-only check (used by /pending which spans the entire
    Enterprise-admin's view).
    """
    user = await store.get_user(username)
    if user is None:
        # Should be impossible after require_admin, but defensive.
        raise HTTPException(status_code=401, detail="user row missing")
    user_ent = user.get("enterprise_id")
    user_grp = user.get("group_id")
    if user_ent is None:
        # Pre-tenancy users (legacy fixtures) — refuse rather than silently
        # admit "anyone with admin role" to xgroup_consent ops.
        raise HTTPException(
            status_code=403,
            detail="caller user row has no enterprise_id; xgroup_consent ops require tenancy-scoped admins",
        )
    if user_ent != expected_enterprise_id:
        raise HTTPException(
            status_code=403,
            detail=f"caller enterprise={user_ent!r} does not match request enterprise={expected_enterprise_id!r}",
        )
    if expected_l2 is None:
        return
    # L2 ids are "<enterprise>/<group>" — bind the group component too.
    expected_grp = expected_l2.split("/", 1)[1] if "/" in expected_l2 else expected_l2
    if user_grp is not None and user_grp != expected_grp:
        raise HTTPException(
            status_code=403,
            detail=f"caller group={user_grp!r} does not match request L2 group={expected_grp!r}",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/propose", response_model=ProposeResponse, status_code=201)
async def propose(
    req: ProposeRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> ProposeResponse:
    """First signer proposes a grant; pending row written on source L2."""
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=req.body.enterprise_id,
        expected_l2=req.proposer_l2,
    )
    body_dict = req.body.model_dump()
    try:
        result = await xgc.propose_grant(
            store,
            body=body_dict,
            proposer_l2=req.proposer_l2,
            proposer_pubkey_b64u=req.proposer_pubkey_b64u,
            proposer_signature_b64u=req.proposer_signature_b64u,
        )
    except xgc.XGroupConsentError as exc:
        raise _http_for(exc) from exc
    return ProposeResponse(**result)


@router.get("/pending", response_model=PendingListResponse)
async def list_pending(
    enterprise_id: str,
    target_l2: str,
    limit: int = 100,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> PendingListResponse:
    """Target L2 admin retrieves proposals awaiting their cosign / ratify."""
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=enterprise_id,
        expected_l2=target_l2,
    )
    rows = await xgc.list_pending_for_target(
        store,
        enterprise_id=enterprise_id,
        target_l2=target_l2,
        limit=min(limit, 500),
    )
    items = []
    for r in rows:
        body = _safe_json(r["body_canonical"])
        items.append(
            PendingItem(
                pending_id=r["pending_id"],
                grant_id=body.get("grant_id", "") if isinstance(body, dict) else "",
                enterprise_id=r["enterprise_id"],
                source_l2=r["source_l2"],
                target_l2=r["target_l2"],
                body=body if isinstance(body, dict) else {},
                body_canonical_sha256_hex=r["body_canonical_sha256_hex"],
                proposer_l2=r["proposer_l2"],
                proposer_pubkey_b64u=r["proposer_pubkey_b64u"],
                proposer_signature_b64u=r["proposer_signature_b64u"],
                status=r["status"],
                proposed_at=r["proposed_at"],
                expires_at=r["expires_at"],
            )
        )
    return PendingListResponse(items=items, count=len(items))


@router.post("/cosign/{pending_id}", response_model=CosignResponse)
async def cosign(
    pending_id: str,
    req: CosignRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> CosignResponse:
    """Target L2 admin cosigns a pending proposal."""
    # Bind caller to the cosigner's L2 — Enterprise inferred via lookup
    # (we don't have it from the pending row at this point without a
    # second SELECT; the L2 component carries both halves).
    user = await store.get_user(admin)
    if user is None or user.get("enterprise_id") is None:
        raise HTTPException(status_code=403, detail="caller has no Enterprise scope")
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=user["enterprise_id"],
        expected_l2=req.cosigner_l2,
    )
    try:
        result = await xgc.cosign_grant(
            store,
            pending_id=pending_id,
            cosigner_l2=req.cosigner_l2,
            cosigner_pubkey_b64u=req.cosigner_pubkey_b64u,
            cosigner_signature_b64u=req.cosigner_signature_b64u,
        )
    except xgc.XGroupConsentError as exc:
        raise _http_for(exc) from exc
    return CosignResponse(**result)


@router.post("/ratify/{pending_id}", response_model=RatifyResponse)
async def ratify(
    pending_id: str,
    req: RatifyRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> RatifyResponse:
    """Promote cosigned pending → active xgroup_consent.

    Convenience: if all three cosigner fields are present and the row is
    still 'proposed', cosign first then ratify. If the row is already
    'cosigned' those fields are ignored.
    """
    # Tenancy gate — load the pending row's Enterprise and confirm
    # the caller's Enterprise matches before mutating.
    pending = await xgc._load_pending(store, pending_id)  # noqa: SLF001
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=pending["enterprise_id"],
        expected_l2=None,  # ratify can be initiated by either side per Decision 28 §2.2
    )
    if req.cosigner_l2 and req.cosigner_pubkey_b64u and req.cosigner_signature_b64u:
        # Best-effort cosign; if already cosigned, ratify path catches it.
        try:
            await xgc.cosign_grant(
                store,
                pending_id=pending_id,
                cosigner_l2=req.cosigner_l2,
                cosigner_pubkey_b64u=req.cosigner_pubkey_b64u,
                cosigner_signature_b64u=req.cosigner_signature_b64u,
            )
        except xgc.XGroupConsentError as exc:
            # 'conflict' here means already-cosigned — that's fine, fall
            # through to the ratify call. Anything else is a real error.
            if exc.code != "conflict":
                raise _http_for(exc) from exc
    try:
        result = await xgc.ratify_grant(store, pending_id=pending_id)
    except xgc.XGroupConsentError as exc:
        raise _http_for(exc) from exc
    return RatifyResponse(**result)


@router.post("/revoke/{grant_id}", response_model=RevokeResponse)
async def revoke(
    grant_id: str,
    req: RevokeRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> RevokeResponse:
    """Normal revoke — either pinned admin (or current admin via lineage)."""
    active = await xgc._load_active(store, grant_id)  # noqa: SLF001
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=active["enterprise_id"],
        expected_l2=None,
    )
    try:
        result = await xgc.revoke_grant(
            store,
            grant_id=grant_id,
            revoker_pubkey_b64u=req.revoker_pubkey_b64u,
            revoker_signature_b64u=req.revoker_signature_b64u,
            revoker_l2=req.revoker_l2,
            reason=req.reason,
            revoke_ts=req.revoke_ts,
            is_recovery=False,
            key_lineage_walker=None,
        )
    except xgc.XGroupConsentError as exc:
        raise _http_for(exc) from exc
    return RevokeResponse(**result)


@router.post("/recovery-revoke/{grant_id}", response_model=RevokeResponse)
async def recovery_revoke(
    grant_id: str,
    req: RevokeRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> RevokeResponse:
    """Recovery revoke — directory root signing key flow (Decision 28 §2.5)."""
    active = await xgc._load_active(store, grant_id)  # noqa: SLF001
    await _enforce_caller_tenancy(
        username=admin,
        store=store,
        expected_enterprise_id=active["enterprise_id"],
        expected_l2=None,
    )
    try:
        result = await xgc.revoke_grant(
            store,
            grant_id=grant_id,
            revoker_pubkey_b64u=req.revoker_pubkey_b64u,
            revoker_signature_b64u=req.revoker_signature_b64u,
            revoker_l2=req.revoker_l2,
            reason=req.reason,
            revoke_ts=req.revoke_ts,
            is_recovery=True,
            key_lineage_walker=None,
        )
    except xgc.XGroupConsentError as exc:
        raise _http_for(exc) from exc
    return RevokeResponse(**result)


def _safe_json(s: str) -> Any:
    """Tolerant JSON parse — guard the /pending list against a corrupt row."""
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None
