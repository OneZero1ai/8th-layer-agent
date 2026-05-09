"""AIGRP package.

Phase 1.0b — Decision 28 expanded the original ``aigrp.py`` module into a
package so the per-pair-secret + Enterprise-root protocol pieces can live in
their own files alongside the existing peer/Bloom/centroid helpers.

Backwards compatibility: every name the legacy ``aigrp.py`` exported is
re-exported here, so callers using ``from cq_server import aigrp`` and
``aigrp.self_l2_id()`` keep working. Phase 1.0b modules add:

- ``cq_server.aigrp.pair_secret`` — HKDF-SHA256 per-pair derivation
- ``cq_server.aigrp.enterprise_root`` — SSM-backed Enterprise root w/ cache
- ``cq_server.aigrp.envelope`` — HMAC-SHA256 envelope sign/verify

The package is import-cycle-safe: only ``_legacy`` imports from
``forward_sign`` (lazily), and the new modules depend only on stdlib +
``cryptography`` for primitives.
"""

from ._legacy import (
    BLOOM_BITS,
    BLOOM_HASHES,
    FORWARDER_HEADER,
    aigrp_enabled,
    bloom_contains,
    bloom_matches_any,
    compute_centroid,
    compute_domain_bloom,
    enterprise,
    group,
    is_first_deploy,
    now_iso,
    require_forwarder_identity,
    require_peer_key,
    seed_peer_url,
    self_l2_id,
    self_url,
)

__all__ = [
    "BLOOM_BITS",
    "BLOOM_HASHES",
    "FORWARDER_HEADER",
    "aigrp_enabled",
    "bloom_contains",
    "bloom_matches_any",
    "compute_centroid",
    "compute_domain_bloom",
    "enterprise",
    "group",
    "is_first_deploy",
    "now_iso",
    "require_forwarder_identity",
    "require_peer_key",
    "seed_peer_url",
    "self_l2_id",
    "self_url",
]
