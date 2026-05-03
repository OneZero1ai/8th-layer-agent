"""SHA-256 Merkle tree over reputation event hashes (task #108 sub-task 3).

A small, dependency-free implementation tailored to the reputation log:
inputs are leaf hashes already in ``sha256:<hex>`` format (the
``payload_hash`` column from ``reputation_events``). Output is a single
``sha256:<hex>`` root.

Tree shape: classical binary, leaves left-to-right in the order the
caller passes them (callers pre-sort by ``ts`` ASC). When a level has
an odd number of nodes, the last node is duplicated (RFC 6962 / Bitcoin
convention) — keeps the tree balanced at the cost of allowing two
distinct leaf sets to produce the same root only if one is a duplicate
of the other (not a concern here: caller guarantees uniqueness per
day per Enterprise via the chain hash).

Empty input: returns a fixed zero-event constant (``EMPTY_DAY_ROOT``)
so day-over-day roots form a continuous chain even when a day has zero
events. Verifiers treat the constant as "no events that day" without
needing a special case in the wire format.

Inclusion proofs are *not* implemented in v1. They land if/when the
verifier library (#108 sub-task 7) needs them; for now verification
re-derives the full root from the chain.
"""

from __future__ import annotations

import hashlib

# A constant root for days with zero events. Computed as
# sha256("8l-reputation-empty-day-v1") — non-overlapping with any
# real Merkle root because real leaves are sha256(canonical-payload)
# pre-images of canonical event bodies, never this fixed string.
EMPTY_DAY_ROOT: str = (
    "sha256:" + hashlib.sha256(b"8l-reputation-empty-day-v1").hexdigest()
)


def _strip_prefix(h: str) -> bytes:
    """Convert a ``sha256:<hex>`` string to the underlying 32-byte digest."""
    if not h.startswith("sha256:"):
        raise ValueError(f"expected sha256:<hex> form, got {h!r}")
    return bytes.fromhex(h[len("sha256:"):])


def _hash_pair(left: bytes, right: bytes) -> bytes:
    """Concatenate-and-hash two 32-byte digests."""
    return hashlib.sha256(left + right).digest()


def merkle_root(leaf_hashes: list[str]) -> str:
    """Return the Merkle root over a list of ``sha256:<hex>`` leaf hashes.

    Args:
        leaf_hashes: list of leaf payload hashes in ``sha256:<hex>``
            format (the ``payload_hash`` column from
            ``reputation_events``). Order is load-bearing — the caller
            sorts by event ``ts`` ASC before calling. Duplicates are
            permitted (the chain hash rules out same-day collisions
            in practice).

    Returns:
        The root as ``sha256:<hex>``. Returns ``EMPTY_DAY_ROOT`` when
        ``leaf_hashes`` is empty.
    """
    if not leaf_hashes:
        return EMPTY_DAY_ROOT

    level: list[bytes] = [_strip_prefix(h) for h in leaf_hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate last node — RFC 6962 style
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return "sha256:" + level[0].hex()
