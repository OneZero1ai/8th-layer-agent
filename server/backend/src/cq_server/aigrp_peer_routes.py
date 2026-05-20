"""Admin route — manual AIGRP peer announcement (agent#337).

Endpoint:

  POST /api/v1/admin/aigrp/peers

Escape hatch for direct-CFN / air-gapped deploys that bypass the
``directory_client.py`` poll loop. Lets a tenancy-scoped admin populate
``aigrp_peers`` directly so intra-Enterprise mesh ops (semantic-centroid
match, Bloom-domain filter, signed forward-query) have peer rows to work
with on L2s that never registered themselves with ``directory.8th-layer.ai``.

The directory poll loop remains the primary path; this endpoint is only
invoked when the directory is unreachable or intentionally not wired
(federal / IL5 / air-gapped customer postures per #337's two-tier deploy
recommendation).

Scope intent (per #337's body discussion):

  * The body shape ``(l2_id, enterprise, group, endpoint_url, pubkey,
    embedding_centroid?, domain_bloom?)`` matches the ``aigrp_peers``
    table (intra-Enterprise peer mesh), NOT ``aigrp_directory_peerings``
    (cross-Enterprise offer/accept). The latter has its own bilateral
    proposal/cosign protocol — admin can't unilaterally fabricate one
    side of a peering.
  * The caller's Enterprise MUST equal ``body.enterprise``. Cross-
    Enterprise peer insertion is intentionally refused (422) — those
    rows belong to the bilateral peering protocol, not this escape
    hatch.

Auth: ``require_admin`` (FO-1c session cookie or bearer JWT) plus the
defence-in-depth tenancy check via the user row's ``enterprise_id``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import require_admin
from .deps import get_store
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/aigrp/peers", tags=["admin", "aigrp"])

# Ed25519 public keys are exactly 32 bytes per RFC 8032 §5.1.5.
_ED25519_PUBLIC_KEY_SIZE = 32
_INVALID_PUBKEY_DETAIL = "pubkey must be base64url-encoded Ed25519 public key (32 bytes)"


class _InvalidPubkeyError(Exception):
    """Internal sentinel for the route handler — convert to 400 response."""


def _invalid_pubkey_response() -> JSONResponse:
    """Standard 400 body for pubkey validation failures (mirrors directory-peer route)."""
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_pubkey", "detail": _INVALID_PUBKEY_DETAIL},
    )


def _validate_pubkey_ed25519(pubkey: str) -> None:
    """Verify ``pubkey`` is a real Ed25519 public key — addresses #346 concern 1.

    Three layers — base64url decode, length == 32, curve-point validity.
    See ``aigrp_directory_peer_routes._validate_pubkey_ed25519`` for the
    long-form docstring; this is the intra-Enterprise sibling, applied
    here because the PR-#345 endpoint shipped with only the implicit
    Pydantic ``min_length=1, max_length=128`` check, which accepted hex
    strings and other non-base64url shapes.
    """
    try:
        padded = pubkey + "=" * (-len(pubkey) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError, binascii.Error) as exc:
        log.info("pubkey rejected (b64u decode): %s", exc)
        raise _InvalidPubkeyError from exc

    if len(raw) != _ED25519_PUBLIC_KEY_SIZE:
        log.info(
            "pubkey rejected (wrong length): got %d bytes, expected %d",
            len(raw),
            _ED25519_PUBLIC_KEY_SIZE,
        )
        raise _InvalidPubkeyError

    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        log.info("pubkey rejected (curve-point): %s", exc)
        raise _InvalidPubkeyError from exc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PeerAnnounceRequest(BaseModel):
    """Inputs for the manual peer-announce endpoint.

    ``embedding_centroid`` arrives as a list of floats (server packs to
    little-endian float32 bytes; ``aigrp_peers.embedding_centroid`` is a
    BLOB column). ``domain_bloom`` arrives as a base64-encoded byte
    string. Both default to ``None`` for first-announce ergonomics — the
    receiving L2 can refresh those fields on a subsequent announce once
    the new peer has computed its own corpus stats.
    """

    l2_id: str = Field(min_length=3, max_length=128, description="Peer L2 in '<enterprise>/<group>' form")
    enterprise: str = Field(min_length=1, max_length=64)
    group: str = Field(min_length=1, max_length=64)
    endpoint_url: str = Field(min_length=7, max_length=512, description="HTTPS URL of the peer L2")
    pubkey: str = Field(min_length=1, max_length=128, description="Base64url-encoded Ed25519 public key")
    embedding_centroid: list[float] | None = Field(default=None, description="Optional packed-on-server centroid")
    domain_bloom: str | None = Field(default=None, description="Optional base64-encoded Bloom filter bytes")
    ku_count: int = Field(default=0, ge=0, description="KU count snapshot at announce time")
    domain_count: int = Field(default=0, ge=0, description="Distinct-domain count snapshot")
    embedding_model: str | None = Field(default=None, max_length=128, description="Embedding model id the peer used")


class PeerAnnounceResponse(BaseModel):
    """The row that landed, plus the audit_id from ``cross_l2_audit``."""

    l2_id: str
    enterprise: str
    group: str
    endpoint_url: str
    embedding_model: str | None
    ku_count: int
    domain_count: int
    first_seen_at: str
    last_seen_at: str
    last_signature_at: str | None
    public_key_ed25519: str | None
    audit_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack_centroid(values: list[float] | None) -> bytes | None:
    """Pack a float list into little-endian float32 BLOB bytes.

    Mirrors ``aigrp._legacy.compute_centroid``'s output format so a
    manually-announced centroid is byte-compatible with one computed
    from the local KU corpus.
    """
    if values is None:
        return None
    if not values:
        # Empty list is ambiguous — treat as "no centroid" rather than
        # storing a zero-byte BLOB that the cosine-match path would
        # mis-interpret.
        return None
    try:
        return struct.pack(f"<{len(values)}f", *values)
    except struct.error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=422, detail=f"centroid pack failed: {exc}") from exc


def _decode_bloom(b64: str | None) -> bytes | None:
    """Decode base64 Bloom bytes; raise 422 on malformed input."""
    if b64 is None:
        return None
    if not b64:
        return None
    try:
        # Accept both standard and URL-safe base64; tolerate missing padding.
        # ``validate=True`` rejects characters outside the base64 alphabet so
        # malformed input lands a clean 422 instead of silently decoding to
        # garbage bytes.
        padded = b64 + "=" * (-len(b64) % 4)
        return base64.b64decode(padded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail=f"domain_bloom is not valid base64: {exc}") from exc


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


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("", response_model=PeerAnnounceResponse, status_code=201)
async def announce_peer(
    req: PeerAnnounceRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> PeerAnnounceResponse | JSONResponse:
    """Insert (or refresh) one ``aigrp_peers`` row from a signed admin request.

    Behaviour:

    1. Validate the body's ``l2_id`` decomposes to the body's
       ``enterprise`` + ``group``.
    2. Tenancy gate — the caller's user-row ``enterprise_id`` must equal
       the body's ``enterprise``. Defence-in-depth on top of
       ``require_admin``; cross-Enterprise fabricated peers are refused
       with 422 (those go through the bilateral peering protocol).
    3. Upsert via ``store.upsert_aigrp_peer`` with
       ``signature_received=True`` — the manual announce is treated as
       a signed peer announcement because the admin has authenticated
       with their bearer token and the inserted row carries the peer's
       Ed25519 public key.
    4. Audit-log to ``cross_l2_audit`` with
       ``policy_applied='manual_peer_announce'`` so the manual route
       leaves a forensic trail distinguishable from directory-pulled
       rows.
    5. Return the resulting row (via ``list_aigrp_peers``) so the admin
       UI can confirm what landed.
    """
    _validate_l2_id(req.l2_id, req.enterprise, req.group)
    try:
        _validate_pubkey_ed25519(req.pubkey)
    except _InvalidPubkeyError:
        # Concern 1 from #346 — return the canonical 400 invalid_pubkey
        # shape (mirrors the cross-Enterprise sibling endpoint).
        return _invalid_pubkey_response()

    user = await store.get_user(admin)
    if user is None:
        # Should be impossible after require_admin, but defensive.
        raise HTTPException(status_code=401, detail="caller user row missing")
    caller_enterprise = user.get("enterprise_id")
    if caller_enterprise is None:
        raise HTTPException(
            status_code=403,
            detail="caller user row has no enterprise_id; peer-announce requires a tenancy-scoped admin",
        )
    if caller_enterprise != req.enterprise:
        raise HTTPException(
            status_code=422,
            detail=(
                f"caller enterprise={caller_enterprise!r} does not match body enterprise={req.enterprise!r}; "
                "cross-Enterprise peer insertion is refused — use the bilateral peering protocol"
            ),
        )

    centroid_blob = _pack_centroid(req.embedding_centroid)
    bloom_blob = _decode_bloom(req.domain_bloom)

    await store.upsert_aigrp_peer(
        l2_id=req.l2_id,
        enterprise=req.enterprise,
        group=req.group,
        endpoint_url=req.endpoint_url,
        embedding_centroid=centroid_blob,
        domain_bloom=bloom_blob,
        ku_count=req.ku_count,
        domain_count=req.domain_count,
        embedding_model=req.embedding_model,
        signature_received=True,
        public_key_ed25519=req.pubkey,
    )

    audit_id = uuid.uuid4().hex
    now_iso = datetime.now(UTC).isoformat()
    try:
        await store.record_cross_l2_audit(
            audit_id=audit_id,
            ts=now_iso,
            requester_l2_id=None,
            requester_enterprise=req.enterprise,
            requester_group=req.group,
            requester_persona=admin,
            responder_l2_id=req.l2_id,
            responder_enterprise=req.enterprise,
            responder_group=req.group,
            policy_applied="manual_peer_announce",
            result_count=1,
            consent_id=None,
        )
    except Exception:  # pragma: no cover - audit-log failure must not break the upsert
        log.exception(
            "manual_peer_announce: audit-log insert failed for l2_id=%s admin=%s",
            req.l2_id,
            admin,
        )

    # Re-read the row we just wrote so the response reflects exactly
    # what the DB sees (first_seen_at + last_signature_at populated by
    # upsert_aigrp_peer, not by us).
    peers = await store.list_aigrp_peers(req.enterprise)
    landed: dict[str, Any] | None = next(
        (p for p in peers if p.get("l2_id") == req.l2_id),
        None,
    )
    if landed is None:  # pragma: no cover - we just inserted it
        raise HTTPException(
            status_code=500,
            detail=f"peer row vanished after upsert: l2_id={req.l2_id!r}",
        )

    log.info(
        "manual_peer_announce: l2_id=%s endpoint=%s admin=%s audit_id=%s",
        req.l2_id,
        req.endpoint_url,
        admin,
        audit_id,
    )

    return PeerAnnounceResponse(
        l2_id=landed["l2_id"],
        enterprise=landed["enterprise"],
        group=landed["group"],
        endpoint_url=landed["endpoint_url"],
        embedding_model=landed.get("embedding_model"),
        ku_count=landed.get("ku_count", 0) or 0,
        domain_count=landed.get("domain_count", 0) or 0,
        first_seen_at=landed["first_seen_at"],
        last_seen_at=landed["last_seen_at"],
        last_signature_at=landed.get("last_signature_at"),
        public_key_ed25519=landed.get("public_key_ed25519"),
        audit_id=audit_id,
    )
