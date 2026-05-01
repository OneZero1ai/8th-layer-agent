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

The /aigrp/* HTTP endpoints live in app.py and call into this module.

Cross-Enterprise mesh is intentionally out of scope here; that's the AI-BGP
protocol (separate spec, future work).
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from datetime import UTC, datetime
from typing import Iterable

from fastapi import HTTPException, Request

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
    """Externally reachable URL of *this* L2 — included in /aigrp/hello so
    the seed can call us back / record us in its peer table."""
    return os.environ.get("CQ_AIGRP_SELF_URL", "").rstrip("/")


def enterprise() -> str:
    return os.environ.get("CQ_ENTERPRISE", "default-enterprise")


def group() -> str:
    return os.environ.get("CQ_GROUP", "default")


def self_l2_id() -> str:
    """Canonical L2 identity — Enterprise/Group."""
    return f"{enterprise()}/{group()}"


def aigrp_enabled() -> bool:
    """Disable AIGRP entirely when no peer key is configured.

    Lets the cq Remote run in legacy single-L2 mode (e.g., the existing
    `mvp` stack with no AIGRP wiring) without crashing or hammering /aigrp
    against itself."""
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
    for d in domains:
        if bloom_contains(bloom, d):
            return True
    return False


def compute_centroid(embeddings_iter: Iterable[bytes]) -> bytes | None:
    """Compute the L2-normalized centroid of an iterable of packed-float32 LE
    embedding blobs. Returns packed-float32 LE bytes, or None if no embeddings.

    Centroid is the average direction of the corpus — semantic match against
    a query gives a coarse "does this L2 know about this topic?" signal.
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
    return datetime.now(UTC).isoformat()
