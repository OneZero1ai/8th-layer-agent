"""Enterprise AIGRP root: SSM-backed, CMK-encrypted, in-process cached.

Decision 28 §1.2 — the Enterprise AIGRP root is the 32-byte ``ikm`` that
``pair_secret.derive_pair_secret`` HKDFs against. It lives in:

    /8th-layer/aigrp/enterprise-root/<enterprise_id>

as an AWS SSM SecureString encrypted with a per-Enterprise customer-managed
KMS CMK. The L2 task IAM role gets ``ssm:GetParameter`` + ``kms:Decrypt``
scoped to its Enterprise's resource ARNs only.

This module owns:

- ``get_enterprise_root(enterprise_id)`` — read + decrypt + cache (5min TTL).
- ``bootstrap_enterprise_root(enterprise_id, kms_key_id)`` — admin-only
  helper that mints a fresh 32-byte random root and PUT-Parameters it. The
  ``kms_key_id`` is operator-supplied at deploy time per the constraint in
  Phase 1.0b's brief; this module does not mint KMS keys.
- ``invalidate_cache(enterprise_id=None)`` — wipe one or all entries (used
  by rotation flow + tests).

Decision 28 explicitly disallows persisting the derived ``pair_secret``;
that's a concern of the caller. We *do* cache the root itself in-process
because re-fetching it on every AIGRP message would 10x the SSM bill and
add per-message latency. Cache TTL = 5 min, configurable via the
``CQ_AIGRP_ROOT_CACHE_TTL_SEC`` env var (set to 0 in tests).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default 5-minute TTL — long enough to coalesce traffic-bursts, short
# enough that a fresh rotation lands within a single overlap window.
_DEFAULT_TTL_SEC = 300
_ROOT_BYTES = 32
_PARAM_PATH_PREFIX = "/8th-layer/aigrp/enterprise-root/"


def _ttl_sec() -> int:
    raw = os.environ.get("CQ_AIGRP_ROOT_CACHE_TTL_SEC")
    if raw is None:
        return _DEFAULT_TTL_SEC
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("CQ_AIGRP_ROOT_CACHE_TTL_SEC=%r not int; using default", raw)
        return _DEFAULT_TTL_SEC


def _param_path(enterprise_id: str) -> str:
    if not enterprise_id or "/" in enterprise_id:
        # SSM treats "/" as a path separator; an enterprise id with a slash
        # would either silently fan out under another path or 400 on PUT.
        # Reject early with a useful message.
        raise ValueError(f"enterprise_id must be non-empty and slash-free, got {enterprise_id!r}")
    return _PARAM_PATH_PREFIX + enterprise_id


@dataclass
class _CacheEntry:
    root: bytes
    fetched_at: float


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def invalidate_cache(enterprise_id: str | None = None) -> None:
    """Drop one or all cached roots.

    Called by the rotation flow when ``/pending`` becomes ``current``,
    and by test fixtures between cases.
    """
    with _cache_lock:
        if enterprise_id is None:
            _cache.clear()
        else:
            _cache.pop(enterprise_id, None)


def _get_ssm_client():  # noqa: ANN202 — boto3 client type is dynamic
    """Lazy boto3 client builder.

    Lazy so test environments that never touch SSM don't pay boto's
    import-time cost, and so monkeypatching ``boto3.client`` in tests
    works regardless of import order.
    """
    import boto3  # noqa: PLC0415 — intentional lazy import

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return boto3.client("ssm", region_name=region)


def _read_root_from_ssm(enterprise_id: str) -> bytes:
    """Fetch + decrypt the SecureString. Raises on missing parameter."""
    path = _param_path(enterprise_id)
    client = _get_ssm_client()
    resp = client.get_parameter(Name=path, WithDecryption=True)
    raw = resp["Parameter"]["Value"]
    # Stored as hex (PUT-time encoding). 32 raw bytes == 64 hex chars.
    try:
        root = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"enterprise root at {path} is not valid hex (corrupt parameter or schema mismatch)") from exc
    if len(root) != _ROOT_BYTES:
        raise ValueError(f"enterprise root at {path} must decode to {_ROOT_BYTES} bytes, got {len(root)}")
    return root


def get_enterprise_root(enterprise_id: str) -> bytes:
    """Return the 32-byte Enterprise AIGRP root, fetching from SSM on cache miss.

    Cache lookup is racy-safe: two concurrent misses both fetch, both
    write, last-writer wins; both writers wrote the same value (SSM is
    consistent for our access pattern), so observable behaviour is fine.

    Raises:
        ClientError: SSM-side errors propagate (ParameterNotFound,
            AccessDenied, KMS errors — caller decides whether to surface
            500 vs 503).
        ValueError: malformed enterprise_id or corrupted parameter value.
    """
    ttl = _ttl_sec()
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(enterprise_id)
        if entry is not None and ttl > 0 and (now - entry.fetched_at) < ttl:
            return entry.root
    # Fetch outside the lock — boto call can be slow.
    root = _read_root_from_ssm(enterprise_id)
    with _cache_lock:
        _cache[enterprise_id] = _CacheEntry(root=root, fetched_at=now)
    return root


def bootstrap_enterprise_root(
    enterprise_id: str,
    kms_key_id: str,
    *,
    overwrite: bool = False,
) -> bytes:
    """Mint a fresh random root and PUT it to SSM as a SecureString.

    Args:
        enterprise_id: identifies the Enterprise; goes into the SSM path.
        kms_key_id: ARN/alias/id of the per-Enterprise customer-managed
            CMK (operator-provisioned, Decision 28 Addendum). This module
            does NOT create KMS keys; passing the wrong id surfaces as a
            boto exception at PUT time.
        overwrite: when False (the default), refuse to PUT if a value
            already exists at that path. Set True only from a TOTP-gated
            rotation path.

    Returns:
        The 32 freshly-minted random bytes (caller may use these in the
        same process to bootstrap an L2; never log them).
    """
    if not kms_key_id:
        raise ValueError("kms_key_id is required (operator-provisioned per-Enterprise CMK)")
    root = secrets.token_bytes(_ROOT_BYTES)
    client = _get_ssm_client()
    client.put_parameter(
        Name=_param_path(enterprise_id),
        Value=root.hex(),
        Type="SecureString",
        KeyId=kms_key_id,
        Overwrite=overwrite,
    )
    # Bust the cache so the next get_enterprise_root call picks up the new
    # value rather than a pre-bootstrap miss-result.
    invalidate_cache(enterprise_id)
    return root


__all__ = [
    "bootstrap_enterprise_root",
    "get_enterprise_root",
    "invalidate_cache",
]
