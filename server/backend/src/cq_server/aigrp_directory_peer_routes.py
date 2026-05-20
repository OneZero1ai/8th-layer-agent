"""Admin route — manual cross-Enterprise directory-peering announce (agent#347).

Endpoint:

  POST /api/v1/admin/aigrp/directory-peerings

The cross-Enterprise sibling of ``POST /admin/aigrp/peers`` (agent#337,
PR #345). Where that endpoint populates ``aigrp_peers`` for the
intra-Enterprise mesh, **this** endpoint populates
``aigrp_directory_peerings`` for the cross-Enterprise consult-forward
path (``/api/v1/consults/x-enterprise-forward-request``).

Until #347 lands, mvp-* / direct-CFN deploys that have no path to
``directory.8th-layer.ai`` cannot cross-forward consults at all — the
``find_active_directory_peering(from, to)`` lookup returns nothing, the
consult resolver short-circuits, and the request 422s. With this
endpoint, an L2 admin can paste in a peer Enterprise's announce data
(peer L2 id, endpoint, Ed25519 pubkey, optional AAISN) and the
peering row lands, unblocking the forward.

## Option-A (manual paste) vs Option-B (bilateral signed envelopes)

This is the Option-A path from the #347 issue body — a single admin
manually announcing the peer's published data. Trust anchor is the
admin who pasted it; there is no callback / co-signature check.

Option-B (the bilateral offer/accept signed-envelope protocol, mirrored
by the directory poll loop) is the long-term answer. The directory
client's pull cycle already implements the verifying writer for that
shape. This endpoint co-exists with Option-B — a row inserted here is
overwritten on the next directory pull that returns the same
``(from_enterprise, to_enterprise)`` pair under a proper signed
peering.

## Schema impedance

The ``aigrp_directory_peerings`` table was designed around the
offer/accept envelope shape (``offer_payload_canonical``,
``offer_signature_b64u``, ``offer_signing_key_id``, etc.). A
manually-pasted announce has none of those — there's no canonical
payload because there's no protocol exchange that produced it.

The bridge:

* ``offer_id`` — deterministic synthetic id of the form
  ``manual:<from_enterprise>:<to_enterprise>:<l2_id>`` so re-running
  the announce upserts the same row rather than landing a second
  ``manual:<uuid>`` row each time. This gives the endpoint idempotency
  without changing the table's PK shape.
* ``status`` — hard-coded ``'active'`` so
  ``find_active_directory_peering`` returns it.
* ``content_policy`` / ``consult_logging_policy`` — sentinel
  ``'manual'`` so receivers can distinguish manual rows from
  directory-pulled rows when applying policy.
* ``offer_signing_key_id`` / ``accept_signing_key_id`` — set to the
  body's ``pubkey`` field. That key is the only cryptographic anchor
  for this peer; carrying it in both signing-key slots makes downstream
  lookups (``forward_sign.verify_forward_signature``) find it without
  needing two distinct keys.
* ``offer_payload_canonical`` / ``accept_payload_canonical`` — small
  JSON blobs documenting the manual provenance (admin persona, ts,
  source = ``manual_directory_peer_announce``). Not signed; receivers
  detecting ``content_policy='manual'`` know not to treat these as
  signed envelopes.
* ``to_l2_endpoints_json`` — the consequential field for the
  consult-forward path. We populate it with a one-entry roster
  describing the announced peer L2:
  ``[{"l2_id": ..., "endpoint_url": ..., "pubkey": ..., "aaisn": ...}]``
  — exactly the shape ``_resolve_x_enterprise_target`` reads in
  ``consults.py``.
* ``expires_at`` — set to now + 30 days. Configurable per-call if we
  later want shorter TTLs; 30 days matches the spirit of #347's "rotate
  out by re-pasting" expectation.

## AAISN

The table has no ``aaisn`` column. The announce stashes it inside the
endpoint roster entry (``to_l2_endpoints_json[0].aaisn``). Pubkey is
the security anchor anyway — AAISN is human-readable provenance
metadata for the receiving admin.

Auth: ``require_admin`` + body-enterprise-DIFFERS-from-caller-tenancy.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_admin
from .deps import get_store
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/aigrp/directory-peerings", tags=["admin", "aigrp"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel Enterprise id used for misconfigured L2s that haven't been
# bound to a real Enterprise. Refuse to announce a peering against this
# (catches admins paste-bombing a peer announce on an unconfigured L2).
_DEFAULT_ENTERPRISE_SENTINEL = "default-enterprise"

# Manual peerings expire 30 days after the announce by default. The
# operator re-pastes to refresh; if they don't, the row drops out of
# ``find_active_directory_peering`` and the consult forward 422s with a
# more honest "no peering" rather than silently using stale data.
_DEFAULT_TTL = timedelta(days=30)

# Tag in ``content_policy`` + ``consult_logging_policy`` that lets
# downstream consumers identify rows that came in via this endpoint.
_MANUAL_POLICY_TAG = "manual"

# Audit ``policy_applied`` value — distinguishes this route's audit
# rows from the intra-Enterprise ``manual_peer_announce`` rows.
_AUDIT_POLICY = "manual_directory_peer_announce"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DirectoryPeerAnnounceRequest(BaseModel):
    """Inputs for the manual cross-Enterprise peering announce.

    The body shape mirrors what an admin would receive from a peer
    Enterprise's L2 announce — L2 id, endpoint, Ed25519 pubkey, and the
    optional published metadata.
    """

    l2_id: str = Field(
        min_length=3,
        max_length=128,
        description="Peer Enterprise's L2 id in '<enterprise>/<group>' form",
    )
    enterprise: str = Field(
        min_length=1,
        max_length=64,
        description="Peer Enterprise — MUST differ from caller's tenancy",
    )
    group: str = Field(min_length=1, max_length=64, description="Peer L2's group")
    endpoint_url: str = Field(
        min_length=7,
        max_length=512,
        description="HTTPS URL of the peer L2",
    )
    pubkey: str = Field(
        min_length=1,
        max_length=128,
        description="Base64url-encoded Ed25519 public key of the peer L2",
    )
    aaisn: str | None = Field(
        default=None,
        max_length=64,
        description="Optional AAISN identifier for the peer Enterprise",
    )
    embedding_centroid: list[float] | None = Field(
        default=None,
        description="Optional published centroid (not currently consumed by directory peering schema)",
    )
    domain_bloom: str | None = Field(
        default=None,
        description="Optional published Bloom filter, base64 (not currently consumed by directory peering schema)",
    )
    ku_count: int = Field(default=0, ge=0)
    domain_count: int = Field(default=0, ge=0)
    embedding_model: str | None = Field(default=None, max_length=128)
    ttl_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="Override the default 30-day TTL (1..365 days)",
    )


class DirectoryPeerAnnounceResponse(BaseModel):
    """The row that landed, plus the audit_id."""

    offer_id: str
    from_enterprise: str
    to_enterprise: str
    status: str
    active_from: str
    expires_at: str
    l2_id: str
    endpoint_url: str
    pubkey: str
    aaisn: str | None
    last_synced_at: str
    audit_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_l2_id(l2_id: str, enterprise: str, group: str) -> None:
    """Confirm ``l2_id`` parses as ``<enterprise>/<group>`` matching the body."""
    if "/" not in l2_id:
        raise HTTPException(
            status_code=422,
            detail=f"l2_id={l2_id!r} must be in '<enterprise>/<group>' form",
        )
    ent_part, grp_part = l2_id.split("/", 1)
    if ent_part != enterprise or grp_part != group:
        raise HTTPException(
            status_code=422,
            detail=(
                f"l2_id={l2_id!r} must decompose to enterprise={enterprise!r} "
                f"group={group!r}; got enterprise={ent_part!r} group={grp_part!r}"
            ),
        )


def _validate_pubkey_b64u(pubkey: str) -> None:
    """Confirm the pubkey decodes from base64url; 422 if it doesn't.

    Mirrors the validation discipline of the intra-Enterprise route's
    ``_decode_bloom`` — guards against admins pasting a hex string or
    raw bytes into a field consumers will treat as base64url.
    """
    try:
        padded = pubkey + "=" * (-len(pubkey) % 4)
        base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"pubkey is not valid base64url: {exc}",
        ) from exc


def _build_offer_id(from_enterprise: str, to_enterprise: str, l2_id: str) -> str:
    """Deterministic offer_id for manual announces — keeps re-paste idempotent.

    Format: ``manual:<from>:<to>:<l2_id>``. Re-running the announce
    with the same body upserts the same row instead of landing a
    second ``manual:<uuid>`` row.
    """
    return f"manual:{from_enterprise}:{to_enterprise}:{l2_id}"


def _manual_envelope_blob(
    *,
    admin: str,
    role: str,
    from_enterprise: str,
    to_enterprise: str,
    l2_id: str,
    ts: str,
) -> str:
    """Synthesize a non-signed JSON blob for the offer/accept payload slots.

    The schema requires non-NULL canonical payload strings; we use a
    documented manual-provenance blob so downstream forensics show
    where the row came from. Never treated as a signed envelope —
    callers detect ``content_policy='manual'`` first.
    """
    return json.dumps(
        {
            "source": _AUDIT_POLICY,
            "role": role,
            "admin_persona": admin,
            "from_enterprise": from_enterprise,
            "to_enterprise": to_enterprise,
            "peer_l2_id": l2_id,
            "ts": ts,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DirectoryPeerAnnounceResponse,
    status_code=201,
)
async def announce_directory_peering(
    req: DirectoryPeerAnnounceRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> DirectoryPeerAnnounceResponse:
    """Insert (or refresh) one ``aigrp_directory_peerings`` row from a signed admin request.

    Behaviour:

    1. Validate the body's ``l2_id`` decomposes to its ``enterprise`` +
       ``group`` and ``pubkey`` parses as base64url.
    2. Tenancy gate — the body's ``enterprise`` must be **different**
       from the caller's resolved tenancy (this is the cross-Enterprise
       endpoint; same-Enterprise paste-bombing goes through
       ``/admin/aigrp/peers``). Also reject the
       ``default-enterprise`` sentinel.
    3. Upsert via ``store.upsert_directory_peering`` with a deterministic
       ``offer_id`` so re-paste is idempotent. Synthetic offer/accept
       payloads document manual provenance; ``content_policy='manual'``
       distinguishes from directory-pulled rows.
    4. ``to_l2_endpoints_json`` carries the one-entry roster that
       ``consults._resolve_x_enterprise_target`` reads to wire the
       forward (``{l2_id, endpoint_url, pubkey, aaisn?}``).
    5. Audit-log to ``cross_l2_audit`` with
       ``policy_applied='manual_directory_peer_announce'`` so this
       route's rows are forensically distinguishable from #345's
       intra-Enterprise rows AND from directory-pulled rows.
    """
    _validate_l2_id(req.l2_id, req.enterprise, req.group)
    _validate_pubkey_b64u(req.pubkey)

    # ------------------------------------------------------------------
    # Tenancy gate — caller must be admin, AND body MUST be a different
    # Enterprise than the caller (this is the cross-Enterprise endpoint).
    # ------------------------------------------------------------------
    user = await store.get_user(admin)
    if user is None:  # pragma: no cover - require_admin already handles
        raise HTTPException(status_code=401, detail="caller user row missing")
    caller_enterprise = user.get("enterprise_id")
    if caller_enterprise is None:
        raise HTTPException(
            status_code=403,
            detail=("caller user row has no enterprise_id; directory-peer-announce requires a tenancy-scoped admin"),
        )

    if req.enterprise == _DEFAULT_ENTERPRISE_SENTINEL:
        raise HTTPException(
            status_code=422,
            detail=(
                f"enterprise={_DEFAULT_ENTERPRISE_SENTINEL!r} is the sentinel for "
                "unconfigured L2s; bind the peer L2 to a real Enterprise before announcing"
            ),
        )
    if caller_enterprise == _DEFAULT_ENTERPRISE_SENTINEL:
        raise HTTPException(
            status_code=403,
            detail=(
                "caller's L2 is bound to the default-enterprise sentinel; "
                "configure tenancy before announcing cross-Enterprise peerings"
            ),
        )
    if caller_enterprise == req.enterprise:
        raise HTTPException(
            status_code=422,
            detail=(
                f"body enterprise={req.enterprise!r} equals caller's enterprise; "
                "use POST /admin/aigrp/peers for intra-Enterprise peer announces"
            ),
        )

    # ------------------------------------------------------------------
    # Build row
    # ------------------------------------------------------------------
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    ttl = timedelta(days=req.ttl_days) if req.ttl_days else _DEFAULT_TTL
    expires_iso = (now + ttl).isoformat()
    offer_id = _build_offer_id(caller_enterprise, req.enterprise, req.l2_id)

    endpoint_entry: dict[str, Any] = {
        "l2_id": req.l2_id,
        "endpoint_url": req.endpoint_url,
        "pubkey": req.pubkey,
        "group": req.group,
    }
    if req.aaisn is not None:
        endpoint_entry["aaisn"] = req.aaisn
    if req.embedding_model is not None:
        endpoint_entry["embedding_model"] = req.embedding_model
    if req.ku_count:
        endpoint_entry["ku_count"] = req.ku_count
    if req.domain_count:
        endpoint_entry["domain_count"] = req.domain_count
    # Centroid + Bloom are accepted for forward-compat; the directory
    # peering schema doesn't have dedicated columns for them, so they
    # ride along inside the endpoint entry where future consumers can
    # find them without a migration.
    if req.embedding_centroid is not None:
        endpoint_entry["embedding_centroid"] = req.embedding_centroid
    if req.domain_bloom is not None:
        endpoint_entry["domain_bloom"] = req.domain_bloom

    endpoints_json = json.dumps([endpoint_entry], sort_keys=True, separators=(",", ":"))

    offer_payload = _manual_envelope_blob(
        admin=admin,
        role="offer",
        from_enterprise=caller_enterprise,
        to_enterprise=req.enterprise,
        l2_id=req.l2_id,
        ts=now_iso,
    )
    accept_payload = _manual_envelope_blob(
        admin=admin,
        role="accept",
        from_enterprise=caller_enterprise,
        to_enterprise=req.enterprise,
        l2_id=req.l2_id,
        ts=now_iso,
    )

    await store.upsert_directory_peering(
        offer_id=offer_id,
        from_enterprise=caller_enterprise,
        to_enterprise=req.enterprise,
        status="active",
        content_policy=_MANUAL_POLICY_TAG,
        consult_logging_policy=_MANUAL_POLICY_TAG,
        topic_filters_json="[]",
        active_from=now_iso,
        expires_at=expires_iso,
        offer_payload_canonical=offer_payload,
        # Manual announces are not signed envelopes — the signature
        # slots carry an empty string sentinel. Consumers must check
        # ``content_policy == 'manual'`` before attempting to verify.
        offer_signature_b64u="",
        offer_signing_key_id=req.pubkey,
        accept_payload_canonical=accept_payload,
        accept_signature_b64u="",
        accept_signing_key_id=req.pubkey,
        last_synced_at=now_iso,
        to_l2_endpoints_json=endpoints_json,
    )

    audit_id = uuid.uuid4().hex
    try:
        await store.record_cross_l2_audit(
            audit_id=audit_id,
            ts=now_iso,
            requester_l2_id=None,
            requester_enterprise=caller_enterprise,
            requester_group=user.get("group_id"),
            requester_persona=admin,
            responder_l2_id=req.l2_id,
            responder_enterprise=req.enterprise,
            responder_group=req.group,
            policy_applied=_AUDIT_POLICY,
            result_count=1,
            consent_id=None,
        )
    except Exception:  # pragma: no cover - audit failure must not block upsert
        log.exception(
            "manual_directory_peer_announce: audit-log insert failed for offer_id=%s admin=%s",
            offer_id,
            admin,
        )

    log.info(
        "manual_directory_peer_announce: offer_id=%s from=%s to=%s peer_l2=%s admin=%s audit_id=%s",
        offer_id,
        caller_enterprise,
        req.enterprise,
        req.l2_id,
        admin,
        audit_id,
    )

    return DirectoryPeerAnnounceResponse(
        offer_id=offer_id,
        from_enterprise=caller_enterprise,
        to_enterprise=req.enterprise,
        status="active",
        active_from=now_iso,
        expires_at=expires_iso,
        l2_id=req.l2_id,
        endpoint_url=req.endpoint_url,
        pubkey=req.pubkey,
        aaisn=req.aaisn,
        last_synced_at=now_iso,
        audit_id=audit_id,
    )
