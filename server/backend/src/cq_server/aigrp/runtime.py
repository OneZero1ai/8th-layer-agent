"""AIGRP runtime wiring — bridges enterprise_root + pair_secret into HTTP auth.

Phase 1.0d (Decision 28). Replaces the per-stack ``CQ_AIGRP_PEER_KEY`` shared
secret with HKDF-derived per-pair secrets so two L2s under the same Enterprise
authenticate cross-L2 traffic without shipping the same Secrets Manager value
to both stacks.

Wire format on intra-Enterprise calls (between sibling L2s):

    X-8L-Forwarder-L2-Id: <sender_l2_id>            # already required
    Authorization: Bearer <bearer_token>            # NEW shape

where ``bearer_token = base64url(HMAC-SHA256(pair_secret, b"aigrp-bearer-v1"))``
and ``pair_secret`` is HKDF-derived from the Enterprise root + lex-canonical
pair name. Both sides compute the same token; receiver constant-time compares.

This is a transport-layer authenticator — message-body integrity is provided
separately by the AIGRP envelope (``aigrp.envelope``) for endpoints that opt
into envelope mode. Existing ``/aigrp/*`` endpoints keep their legacy body
shapes; the bearer is the only auth shift.

Backwards compatibility:
- The cross-Enterprise aggregator path (network.py) keeps using the per-
  Enterprise legacy ``CQ_AIGRP_PEER_KEY`` because aggregator + fleet do NOT
  share an Enterprise root. ``require_peer_key`` falls back to legacy bearer
  when (a) no forwarder header, OR (b) the forwarder declares a different
  Enterprise, OR (c) the new pair-secret bearer doesn't match.
- During the cutover window, an L2 that has ``CQ_AIGRP_PEER_KEY`` set will
  accept either form. Once every sibling has been redeployed with the runtime
  wired, the legacy env var can be unset (operator-driven).

First-boot bootstrap:
- ``CQ_ENTERPRISE_ROOT_BOOTSTRAP=true`` flips on the genesis-L2 path:
  startup mints a 32-byte random root and PUTs it to SSM if absent. Set this
  on the *first* L2 you stand up in an Enterprise; leave default ``false``
  on every subsequent L2.
- ``CQ_AIGRP_IS_FIRST_DEPLOY`` env var is no longer required — runtime
  determines genesis status by SSM presence.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from . import _legacy, enterprise_root, pair_secret

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Versioned constant — bump when the bearer-token derivation shape changes.
_BEARER_INFO = b"aigrp-bearer-v1"

# Per-pair secret cache. Keyed by ``(enterprise_id, peer_l2_id)``.
# Wiped when the Enterprise root cache is invalidated (rotation flow).
_pair_cache: dict[tuple[str, str], bytes] = {}
_pair_cache_lock = threading.Lock()
_pair_cache_ttl_sec = 300  # match enterprise_root default
_pair_cache_meta: dict[tuple[str, str], float] = {}


def invalidate_pair_cache() -> None:
    """Drop all cached pair-secrets. Tests + rotation flow call this."""
    with _pair_cache_lock:
        _pair_cache.clear()
        _pair_cache_meta.clear()


def _bootstrap_enabled() -> bool:
    return os.environ.get("CQ_ENTERPRISE_ROOT_BOOTSTRAP", "false").lower() == "true"


def _bootstrap_kms_key_id() -> str:
    """KMS key id/ARN/alias for first-boot mint. Required when bootstrap=true."""
    return os.environ.get("CQ_ENTERPRISE_ROOT_KMS_KEY_ID", "")


def bootstrap_root_if_needed() -> bool:
    """First-boot helper — mint Enterprise root in SSM iff missing AND bootstrap=true.

    Idempotent: subsequent boots find the param already, take no action. Safe
    to call on every L2 startup; only the genesis L2 (with bootstrap=true)
    will succeed on first call, others log + skip.

    Returns:
        True iff a root was minted (genesis path); False otherwise (already
        present, or bootstrap disabled).
    """
    if not _bootstrap_enabled():
        return False
    enterprise_id = _legacy.enterprise()
    try:
        enterprise_root.get_enterprise_root(enterprise_id)
        # Param exists — nothing to do.
        logger.info("aigrp bootstrap: Enterprise root already present for %s; skipping mint", enterprise_id)
        return False
    except Exception as exc:  # noqa: BLE001 — we want to mint on any read failure
        # boto3 raises ClientError for ParameterNotFound; we catch broadly here
        # because if SSM is broken for any reason, attempting the PUT is the
        # right thing — PUT will surface the real error if e.g. KMS perms are
        # missing.
        logger.info("aigrp bootstrap: Enterprise root absent for %s (%s); attempting mint", enterprise_id, exc)
    kms_key_id = _bootstrap_kms_key_id()
    if not kms_key_id:
        logger.error(
            "aigrp bootstrap: CQ_ENTERPRISE_ROOT_BOOTSTRAP=true but "
            "CQ_ENTERPRISE_ROOT_KMS_KEY_ID is empty; refusing to mint"
        )
        return False
    enterprise_root.bootstrap_enterprise_root(enterprise_id, kms_key_id, overwrite=False)
    logger.warning("aigrp bootstrap: minted fresh Enterprise root for %s (genesis L2)", enterprise_id)
    return True


def runtime_root_present() -> bool:
    """True iff the Enterprise root is reachable via SSM right now.

    Used to compute ``is_first_deploy_runtime`` — a runtime replacement
    for the static ``CQ_AIGRP_IS_FIRST_DEPLOY`` env. Catches all exceptions
    because *any* failure to read the root means we can't claim genesis
    status; safer to treat as "not genesis" and run normal bootstrap path.
    """
    try:
        enterprise_root.get_enterprise_root(_legacy.enterprise())
        return True
    except Exception:  # noqa: BLE001
        return False


def is_first_deploy_runtime() -> bool:
    """Runtime equivalent of the legacy ``CQ_AIGRP_IS_FIRST_DEPLOY`` env.

    The L2 is "first deploy" when no peer relationship has yet been
    established — operationally, when no seed peer is configured AND we
    just minted (or are about to mint) the Enterprise root. This is the
    signal AIGRP bootstrap loop uses to skip the seed-fetch step.

    Falls back to the legacy env if explicitly set (transition aid).
    """
    legacy_env = os.environ.get("CQ_AIGRP_IS_FIRST_DEPLOY", "").lower()
    if legacy_env in ("true", "false"):
        return legacy_env == "true"
    # Runtime determination: genesis when no seed peer URL configured.
    # The Enterprise root presence isn't enough by itself — second L2 in an
    # Enterprise also finds the root present; what distinguishes genesis is
    # the lack of a seed peer to bootstrap from.
    return not _legacy.seed_peer_url()


def _pair_secret_for_peer(peer_l2_id: str) -> bytes:
    """Return the cached or freshly-derived 32-byte pair-secret for ``peer_l2_id``.

    Caches by ``(self_enterprise, peer_l2_id)``. Cache TTL == enterprise_root
    cache TTL so a root rotation lands within one window.

    Raises whatever ``enterprise_root.get_enterprise_root`` raises on read
    failure (ClientError, ValueError) — caller decides 401 vs 503.
    """
    enterprise_id = _legacy.enterprise()
    self_id = _legacy.self_l2_id()
    if peer_l2_id == self_id:
        raise ValueError(f"refusing to derive pair-secret with self ({self_id!r})")
    key = (enterprise_id, peer_l2_id)
    now = time.monotonic()
    with _pair_cache_lock:
        cached = _pair_cache.get(key)
        meta = _pair_cache_meta.get(key, 0.0)
        if cached is not None and (now - meta) < _pair_cache_ttl_sec:
            return cached
    root = enterprise_root.get_enterprise_root(enterprise_id)
    secret = pair_secret.derive_pair_secret(root, self_id, peer_l2_id)
    with _pair_cache_lock:
        _pair_cache[key] = secret
        _pair_cache_meta[key] = now
    return secret


def _bearer_for_secret(secret: bytes) -> str:
    """Compute the b64url HMAC bearer token from a pair-secret.

    Both sides of an intra-Enterprise call compute the same token because
    ``derive_pair_secret`` is symmetric (lex-canonical) and the HMAC info
    string is fixed. The token is therefore equally valid as the
    Authorization value going either direction — it only authenticates
    "the caller possesses the pair-secret", not direction.
    """
    mac = hmac.new(secret, _BEARER_INFO, "sha256").digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def derive_bearer_token(peer_l2_id: str) -> str:
    """Caller-side: build the Authorization-Bearer value for a peer.

    Used by the consults forwarder + AIGRP poll loop when calling a sibling
    L2 inside the same Enterprise. The receiver derives the same token from
    its end and constant-time compares.

    Raises:
        ValueError: if ``peer_l2_id`` is empty or equals self.
        Exception: any underlying SSM/KMS error from the root fetch — caller
            translates to 503.
    """
    if not peer_l2_id:
        raise ValueError("peer_l2_id required")
    secret = _pair_secret_for_peer(peer_l2_id)
    return _bearer_for_secret(secret)


def verify_bearer_against_peer(peer_l2_id: str, presented_bearer: str) -> bool:
    """Receiver-side: constant-time compare presented bearer against expected.

    Returns True on match, False on mismatch / derivation failure. Errors
    from SSM/KMS are swallowed and rendered as False — receiver fails closed
    rather than 5xx-ing on a transient infra error (caller will retry, and
    a real misconfig surfaces as a sustained 401 rather than a noisy 5xx).
    """
    if not presented_bearer or not peer_l2_id:
        return False
    try:
        secret = _pair_secret_for_peer(peer_l2_id)
    except Exception:  # noqa: BLE001
        logger.exception("aigrp pair-secret derive failed for peer=%s; treating as auth failure", peer_l2_id)
        return False
    expected = _bearer_for_secret(secret)
    return hmac.compare_digest(expected, presented_bearer)


__all__ = [
    "bootstrap_root_if_needed",
    "derive_bearer_token",
    "invalidate_pair_cache",
    "is_first_deploy_runtime",
    "runtime_root_present",
    "verify_bearer_against_peer",
]
