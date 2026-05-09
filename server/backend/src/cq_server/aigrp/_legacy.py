"""AIGRP — peer-to-peer mesh inside an Enterprise (EIGRP-shaped).

Each L2 in an Enterprise establishes peer relationships directly with its
sibling L2s and exchanges *signatures* (centroid + domain Bloom filter),
never raw KU bytes. New L2s join via a seed-peer URL + shared Enterprise
key; the seed floods the new L2's existence to its known peers, converging
to a full mesh over a few polling intervals.

This module owns:
- Peer-key auth dependency (Bearer EnterprisePeerKey)
- Signature computation (centroid of approved KU embeddings + Bloom of domains)
- Peer-table persistence helpers (against the shared SQLite store)
- Forwarder-identity validation (header binding + sprint-4 Ed25519 sig)

The /aigrp/* HTTP endpoints live in app.py and call into this module.

Cross-Enterprise mesh is intentionally out of scope here; that's the AI-BGP
protocol (separate spec, future work).

Environment variables consumed here (sprint 4 — see also ``forward_sign``):
    CQ_AIGRP_PEER_KEY            shared EnterprisePeerKey for /aigrp/* auth.
    CQ_AIGRP_IS_FIRST_DEPLOY     ``true`` on the genesis L2 of an Enterprise.
    CQ_AIGRP_SEED_PEER_URL       seed peer the joiner contacts on startup.
    CQ_AIGRP_SELF_URL            externally reachable URL of this L2.
    CQ_ENTERPRISE / CQ_GROUP     this L2's identity components.
    CQ_AIGRP_L2_PRIVKEY_PATH     forward-signing private key on disk
                                 (default /data/aigrp_l2_key.bin).
    CQ_REQUIRE_SIGNED_FORWARDS   ``true`` flips strict mode on receivers
                                 (legacy unsigned forwards are rejected).
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from ..store._sqlite import SqliteStore

logger = logging.getLogger(__name__)


# Bloom filter parameters — tuned for ~1000 distinct domain tags per L2 with
# < 1% false positive rate. 8192 bits = 1 KB; cheap to ship.
BLOOM_BITS = 8192
BLOOM_HASHES = 5


def is_first_deploy() -> bool:
    """Return True iff this L2 is the genesis node for its Enterprise."""
    return os.environ.get("CQ_AIGRP_IS_FIRST_DEPLOY", "false").lower() == "true"


def seed_peer_url() -> str:
    """The peer URL we should bootstrap from. Empty when first-deploy."""
    return os.environ.get("CQ_AIGRP_SEED_PEER_URL", "").rstrip("/")


def self_url() -> str:
    """Externally reachable URL of *this* L2.

    Included in /aigrp/hello so the seed can call us back / record us in
    its peer table.
    """
    return os.environ.get("CQ_AIGRP_SELF_URL", "").rstrip("/")


def enterprise() -> str:
    """This L2's enterprise id (CQ_ENTERPRISE env)."""
    return os.environ.get("CQ_ENTERPRISE", "default-enterprise")


def group() -> str:
    """This L2's group id within its enterprise (CQ_GROUP env)."""
    return os.environ.get("CQ_GROUP", "default")


def self_l2_id() -> str:
    """Canonical L2 identity — Enterprise/Group."""
    return f"{enterprise()}/{group()}"


def aigrp_enabled() -> bool:
    """Disable AIGRP entirely when no peer key is configured.

    Lets the cq Remote run in legacy single-L2 mode (e.g., the existing
    `mvp` stack with no AIGRP wiring) without crashing or hammering /aigrp
    against itself.
    """
    return bool(os.environ.get("CQ_AIGRP_PEER_KEY"))


def require_peer_key(request: Request) -> None:
    """FastAPI dependency: validate the shared EnterprisePeerKey on /aigrp/* calls."""
    expected = os.environ.get("CQ_AIGRP_PEER_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="AIGRP not configured on this L2")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing peer key")
    presented = auth[7:]
    # constant-time compare
    if len(presented) != len(expected):
        raise HTTPException(status_code=401, detail="invalid peer key")
    diff = 0
    for a, b in zip(presented, expected, strict=True):
        diff |= ord(a) ^ ord(b)
    if diff != 0:
        raise HTTPException(status_code=401, detail="invalid peer key")


# SEC-CRIT #34 — forward-endpoint identity binding.
#
# The shared EnterprisePeerKey gates *transport* (only Enterprise L2s can call
# each other). It does NOT bind a request to a specific *sender* L2 — every L2
# in the Enterprise has the same key, so any L2 with the key can forge any
# sibling's identity in the body.
#
# Until per-L2 Ed25519 keys land (sprint 4 — same primitive the directory and
# reputation log need), the receiver explicitly enforces:
#   1. The forwarder declares its identity in ``X-8L-Forwarder-L2-Id`` (header,
#      not buried in body)
#   2. The body's ``from_l2_id`` / ``requester_l2_id`` matches the header
#   3. The header's Enterprise component equals the receiver's Enterprise —
#      the peer-key gate is supposed to enforce this implicitly, but the body
#      previously didn't have to honour it
#
# This raises the bar from "any body" to "any body matching declared header"
# and closes cross-Enterprise impersonation outright. Sibling-L2 impersonation
# inside an Enterprise is the residual gap closed by Ed25519.
FORWARDER_HEADER = "x-8l-forwarder-l2-id"


async def require_forwarder_identity(
    request: Request,
    claimed_l2_id: str,
    *,
    same_enterprise_only: bool = True,
    body_for_sig: dict | None = None,
    store: SqliteStore | None = None,
) -> str:
    """Validate the forwarder's declared identity on a /forward-* endpoint.

    Layers the CRIT #34 partial fix (header/body binding) with the
    sprint-4 Ed25519 forward signature (#44 — closes the residual
    sibling-L2 spoof).

    Args:
        request: incoming FastAPI request (must contain the forwarder header).
        claimed_l2_id: the L2 id claimed in the request body (e.g.
            body.from_l2_id for consults, body.requester_l2_id for AIGRP).
        same_enterprise_only: when True (intra-Enterprise forwards like
            /consults/forward-*), the forwarder's Enterprise component must
            match the receiver's Enterprise. When False (AIGRP cross-Enterprise
            consent path), the header still must match the body but the
            forwarder may belong to a peered foreign Enterprise — that
            authorisation comes from cross_enterprise_consents downstream.
        body_for_sig: parsed request body (canonicalisable dict). When
            provided alongside ``store``, the receiver verifies the
            ``X-8L-Forwarder-Sig`` header against the peer's recorded
            Ed25519 public key. ``None`` skips signature verification —
            used by legacy callers that haven't been migrated yet.
        store: ``SqliteStore`` instance for pubkey lookup. Pass alongside
            ``body_for_sig``. Typed as ``object`` to avoid an import
            cycle with ``cq_server.store``.

    Returns:
        The header-declared forwarder L2 id (post-validation).

    Raises:
        HTTPException 400 on missing/empty header.
        HTTPException 403 on header/body mismatch, cross-Enterprise spoof,
            missing-sig in strict mode, or invalid signature when the
            peer's pubkey is on file.
    """
    declared = request.headers.get(FORWARDER_HEADER, "").strip()
    if not declared:
        raise HTTPException(
            status_code=400,
            detail=f"missing {FORWARDER_HEADER} header on cross-L2 forward",
        )
    if declared != claimed_l2_id:
        raise HTTPException(
            status_code=403,
            detail=f"forwarder identity mismatch: header={declared!r} body={claimed_l2_id!r}",
        )
    declared_enterprise, sep, _ = declared.partition("/")
    if not declared_enterprise or not sep:
        raise HTTPException(
            status_code=400,
            detail=f"forwarder l2 id must be enterprise/group, got {declared!r}",
        )
    if same_enterprise_only and declared_enterprise != enterprise():
        raise HTTPException(
            status_code=403,
            detail=f"cross-Enterprise forward rejected: forwarder={declared!r} receiver_enterprise={enterprise()!r}",
        )

    # Sprint 4 — Ed25519 forward signature (#44).
    #
    # When the caller supplies the parsed body and the store, look up
    # the peer's pubkey. Three cases:
    #
    #   1. Pubkey on file + valid sig          → verified signed forward.
    #   2. Pubkey on file + missing/invalid sig → 403 (sibling-L2 spoof
    #      attempt or stale peer key — operator must rotate).
    #   3. No pubkey on file (legacy peer)     → log WARNING and accept
    #      via header-only auth, UNLESS CQ_REQUIRE_SIGNED_FORWARDS=true
    #      (strict mode), in which case 403.
    #
    # TODO: post-rollout (after one full bootstrap cycle has populated
    # every peer's pubkey), flip default of CQ_REQUIRE_SIGNED_FORWARDS
    # to true and deprecate the legacy code path.
    if body_for_sig is not None and store is not None:
        from .. import forward_sign

        peer_pubkey = None
        try:
            peer_pubkey = await store.get_aigrp_peer_pubkey(declared)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            # Defensive: a borked store call shouldn't 500 a forward path.
            logger.exception("forward-sign: pubkey lookup failed for peer=%s", declared)
        sig_header = request.headers.get(forward_sign.SIGNATURE_HEADER, "").strip()
        if peer_pubkey:
            if not sig_header:
                raise HTTPException(
                    status_code=403,
                    detail=f"missing {forward_sign.SIGNATURE_HEADER} header from signed peer {declared!r}",
                )
            if not forward_sign.verify_forward_signature(peer_pubkey, body_for_sig, declared, sig_header):
                raise HTTPException(
                    status_code=403,
                    detail=f"forward signature verification failed for peer={declared!r}",
                )
            logger.info("aigrp: verified signed forward from peer=%s", declared)
        else:
            if forward_sign.require_signed_forwards():
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"strict mode (CQ_REQUIRE_SIGNED_FORWARDS=true) and no pubkey "
                        f"on file for peer={declared!r}; require re-hello"
                    ),
                )
            logger.warning(
                "aigrp: legacy unsigned forward from peer=%s (no pubkey on file; "
                "will become 403 once CQ_REQUIRE_SIGNED_FORWARDS=true)",
                declared,
            )
    return declared


def _bloom_hashes(domain: str) -> list[int]:
    """Return BLOOM_HASHES bit indices for a domain string."""
    digest = hashlib.sha256(domain.encode()).digest()
    return [int.from_bytes(digest[i * 4 : i * 4 + 4], "little") % BLOOM_BITS for i in range(BLOOM_HASHES)]


def compute_domain_bloom(domains: Iterable[str]) -> bytes:
    """Build a fixed-size Bloom filter over the given domain set.

    Returns BLOOM_BITS / 8 bytes. Stable, deterministic — same input yields
    same output, so a peer's bloom can be diffed across polls to detect
    growth.
    """
    bitmap = bytearray(BLOOM_BITS // 8)
    for d in domains:
        if not d:
            continue
        for h in _bloom_hashes(d.strip().lower()):
            byte_idx = h // 8
            bit_idx = h % 8
            bitmap[byte_idx] |= 1 << bit_idx
    return bytes(bitmap)


def bloom_contains(bloom: bytes, domain: str) -> bool:
    """Test whether ``domain`` is plausibly present in ``bloom``.

    Returns True when every BLOOM_HASHES bit position for ``domain`` is
    set — meaning either the domain was added or this is a false
    positive (target rate <1% at design size). False negatives are
    impossible by Bloom-filter construction.

    Used by the DSN resolver's prefilter step (issue #22): peers whose
    Bloom doesn't claim ANY of the query's domain tags are dropped
    before cosine ranking, since their corpus can't have a relevant
    KU.
    """
    if not bloom or not domain:
        return False
    if len(bloom) * 8 < BLOOM_BITS:
        # Defensive: caller passed a truncated buffer. Treat as miss
        # rather than IndexError.
        return False
    for h in _bloom_hashes(domain.strip().lower()):
        byte_idx = h // 8
        bit_idx = h % 8
        if not (bloom[byte_idx] & (1 << bit_idx)):
            return False
    return True


def bloom_matches_any(bloom: bytes, domains: Iterable[str]) -> bool:
    """True iff at least one of ``domains`` is plausibly in the Bloom."""
    return any(bloom_contains(bloom, d) for d in domains)


def compute_centroid(embeddings_iter: Iterable[bytes]) -> bytes | None:
    """Compute the L2-normalized centroid of packed-float32 LE embeddings.

    Returns packed-float32 LE bytes, or None if no embeddings. Centroid is
    the average direction of the corpus — semantic match against a query
    gives a coarse "does this L2 know about this topic?" signal.
    """
    import numpy as np

    total = None
    count = 0
    for blob in embeddings_iter:
        if not blob:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.size == 0:
            continue
        norm = np.linalg.norm(vec)
        if norm == 0:
            continue
        vec = vec / norm
        if total is None:
            total = vec.copy()
        else:
            total += vec
        count += 1
    if count == 0 or total is None:
        return None
    centroid = total / count
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm
    return struct.pack(f"<{len(centroid)}f", *centroid.astype(np.float32))


def now_iso() -> str:
    """UTC now as an ISO-8601 string (used in AIGRP signature timestamps)."""
    return datetime.now(UTC).isoformat()
