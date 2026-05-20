"""HMAC v0 auth for the central transactional-mail service (Decision 34).

# Why HMAC and not AAISN

Decision 34 picks HMAC v0 for ship-now. AAISN (the Ed25519 identity
that signs AIGRP envelopes) is the v1 follow-up. Comparable security
in practice (per-L2 secret in SSM, scoped to one role), strictly
simpler to implement.

# Signing scheme

The L2 signs the raw request body with HMAC-SHA256, keyed by its
per-L2 ``tx_send_key`` (also stored in SSM at
``/8th-layer/l2/{enterprise}/{group}/tx_send_key``). It posts:

* ``X-8L-L2-Id: {enterprise}/{group}`` — identifies the caller.
* ``X-8L-Signature: sha256={hex digest}`` — body MAC.

The server resolves the key by ``l2_id``, recomputes the digest, and
compares with :func:`hmac.compare_digest` (constant-time).

Format choice notes:

* ``sha256=...`` prefix matches GitHub's webhook signature convention
  — operators recognise the shape; trivial to grep in logs.
* Hex (not base64) because the prefix grammar is documented and the
  extra ~20% size is negligible at sub-kB body sizes.
* Body-only MAC (no headers in the input) keeps verification simple
  and lets reverse proxies / WAFs add headers without breaking auth.
  The body itself carries everything tenancy-relevant (``from_persona``,
  ``to``, ``category``).
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

log = logging.getLogger(__name__)

# X-8L-Signature shape: "sha256=<hex>". Lifted from GitHub's webhook
# signature convention for grep-friendliness.
SIGNATURE_PREFIX = "sha256="


class HmacKeyResolver(Protocol):
    """Pluggable lookup: ``l2_id`` → per-L2 ``tx_send_key`` string.

    Production resolver hits SSM at
    ``/8th-layer/l2/{enterprise}/{group}/tx_send_key`` once and caches
    in-process. Test resolver returns from a dict.
    """

    def __call__(self, l2_id: str) -> str | None:  # pragma: no cover
        ...


@dataclass
class StaticKeyResolver:
    """In-memory key store, primarily for tests.

    Production resolver implementations subscribe to the same protocol
    but read from SSM with a small TTL cache (60s) so a rotation lands
    within a minute without per-request SSM cost.
    """

    keys: dict[str, str]

    def __call__(self, l2_id: str) -> str | None:
        return self.keys.get(l2_id)


from dataclasses import field as _field  # noqa: E402  — local alias


@dataclass
class SsmKeyResolver:
    """Production resolver — fetches ``tx_send_key`` from SSM on first hit.

    The control-plane runtime needs *read* access to every L2's
    ``tx_send_key`` parameter. The expected layout is one parameter
    per L2, written by the provisioning service (or the
    ``bin/backfill-tx-keys`` script for L2s that pre-date Decision 34):

        /8th-layer/l2/{enterprise}/{group}/tx_send_key

    Cache TTL is per-process; on rotation, the operator restarts the
    control-plane task or waits ``ttl_seconds``.
    """

    ttl_seconds: int = 60
    _cache: dict[str, tuple[float, str]] = _field(default_factory=dict)

    def __call__(self, l2_id: str) -> str | None:
        import time

        now = time.monotonic()
        entry = self._cache.get(l2_id)
        if entry is not None and now - entry[0] < self.ttl_seconds:
            return entry[1]

        try:
            import boto3
        except ImportError:  # pragma: no cover — boto3 is a runtime dep
            log.error("boto3 unavailable; cannot resolve tx_send_key for l2_id=%s", l2_id)
            return None

        try:
            enterprise, group = l2_id.split("/", 1)
        except ValueError:
            log.warning("malformed l2_id passed to SsmKeyResolver: %r", l2_id)
            return None

        client = boto3.client("ssm", region_name=os.environ.get("CQ_AWS_REGION", "us-east-1"))
        param_name = f"/8th-layer/l2/{enterprise}/{group}/tx_send_key"
        try:
            resp = client.get_parameter(Name=param_name, WithDecryption=True)
        except Exception as exc:  # pragma: no cover — env-specific
            log.warning("SSM get_parameter failed for %s: %s", param_name, exc)
            return None

        value = resp.get("Parameter", {}).get("Value")
        if value:
            self._cache[l2_id] = (now, value)
        return value


def compute_signature(key: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature for ``body`` under ``key``.

    Exposed so the L2-side client can sign without reimplementing the
    scheme. Both sides MUST use this function — divergence shows up
    as a 401, which masks more interesting failures.
    """
    mac = hmac.new(key.encode("utf-8"), body, sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{mac}"


def verify_hmac_signature(
    *,
    body: bytes,
    signature_header: str | None,
    l2_id: str,
    resolver: HmacKeyResolver,
) -> bool:
    """Verify a request's HMAC. Returns True iff the digest matches.

    Constant-time compare; returns False on every failure mode
    (missing header, unknown l2_id, malformed signature, mismatched
    digest). The caller maps False → 401 — no need to leak which
    failure mode triggered the reject (denies a key-enumeration
    oracle).
    """
    if not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False
    expected_key = resolver(l2_id)
    if expected_key is None:
        return False
    expected = compute_signature(expected_key, body)
    return hmac.compare_digest(expected, signature_header)
