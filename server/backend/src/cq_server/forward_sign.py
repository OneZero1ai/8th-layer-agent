"""Per-L2 Ed25519 keypair management + forward-* request signing.

Sprint 4 — closes residual sibling-L2 spoof gap left by CRIT #34
(issue #44). The peer-key-only auth in CRIT #34's partial fix proved
the *forwarder header matches the body's from_l2_id*, but every L2 in
an Enterprise shares the same EnterprisePeerKey, so any L2 with the
key could still set ``X-8L-Forwarder-L2-Id`` to a sibling's id and the
receiver had no way to tell. This module makes that lie cryptographic
rather than declarative.

The shape:

- Each L2 owns a 32-byte Ed25519 private key on disk at
  ``CQ_AIGRP_L2_PRIVKEY_PATH`` (default ``/data/aigrp_l2_key.bin``,
  mode 0600). Generated lazily on first startup.
- The L2 includes its base64url-encoded public key in every
  ``/aigrp/hello`` (initial bootstrap + every poll cycle thereafter,
  for self-healing if a re-hello is missed). The receiver upserts it
  onto the ``aigrp_peers.public_key_ed25519`` column.
- Outbound /forward-* calls sign a deterministic message — the
  RFC 8785 JCS canonical bytes of the *request body* concatenated with
  the forwarder L2's id (UTF-8 bytes). Signature goes in
  ``X-8L-Forwarder-Sig`` (unpadded base64url). See
  ``signing_input_for`` for the exact byte layout.
- Inbound /forward-* receivers look up the peer's pubkey, verify the
  signature against the same bytes, and 403 on mismatch. If the peer
  has no pubkey on file (legacy peer that hasn't re-helloed since the
  rollout), the receiver falls back to the CRIT-#34-partial behaviour
  and logs a WARNING. ``CQ_REQUIRE_SIGNED_FORWARDS=true`` flips strict
  mode and rejects unsigned forwards even for legacy peers.

V1 is filesystem-mounted keys; KMS/HSM-backed signing is V2 (#48-class
follow-up). The directory client uses a *separate* enterprise-root
keypair — different scope, different trust anchor, intentionally
isolated. Don't conflate the two.

Environment variables
    CQ_AIGRP_L2_PRIVKEY_PATH    where to read/write the 32-byte private key.
                                Default: /data/aigrp_l2_key.bin
    CQ_REQUIRE_SIGNED_FORWARDS  ``true`` → strict; legacy unsigned forwards 403.
                                Default: false (rollout window).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import (
    b64u,
    canonicalize,
    public_key_b64u,
    sign_raw,
    verify_raw,
)

log = logging.getLogger(__name__)

DEFAULT_PRIVKEY_PATH = "/data/aigrp_l2_key.bin"
SIGNATURE_HEADER = "x-8l-forwarder-sig"


def privkey_path() -> Path:
    return Path(os.environ.get("CQ_AIGRP_L2_PRIVKEY_PATH", DEFAULT_PRIVKEY_PATH))


def require_signed_forwards() -> bool:
    """When True, receivers reject legacy unsigned forwards with 403.

    During the rollout window this is False so peers that haven't yet
    re-helloed under the new schema (no pubkey in receiver's table)
    still work. Once a full bootstrap cycle has elapsed and every peer
    has a non-NULL pubkey, operators flip this to True cluster-wide.
    """
    return os.environ.get("CQ_REQUIRE_SIGNED_FORWARDS", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Keypair load / generate
# ---------------------------------------------------------------------------


def _generate_and_persist(path: Path) -> Ed25519PrivateKey:
    """Create a fresh Ed25519 keypair and write the 32 raw private bytes
    with mode 0600. Parent directory is created if missing.
    """
    raw = secrets.token_bytes(32)
    privkey = Ed25519PrivateKey.from_private_bytes(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_CREAT|O_WRONLY|O_EXCL would race; we accept a short
    # window where another process could read the directory entry. The
    # subsequent chmod tightens to 0600 immediately.
    path.write_bytes(raw)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort on filesystems that don't support unix mode bits
        # (e.g. FAT-formatted bind mount). Log but don't fail startup.
        log.warning("forward-sign: chmod 0600 failed on %s", path)
    return privkey


def load_or_create_l2_privkey() -> Ed25519PrivateKey | None:
    """Return this L2's Ed25519 private key, generating it if absent.

    Returns ``None`` (signing disabled) on any unrecoverable load
    failure — receiving peers will then see unsigned forwards and fall
    through to the legacy-peer code path. We never crash startup over a
    bad key file; corrupt key + strict-mode receivers is a deployment
    bug we want surfaced as 403s in logs, not as a crashing L2.
    """
    path = privkey_path()
    try:
        if path.exists():
            raw = path.read_bytes()
            if len(raw) != 32:
                log.error(
                    "forward-sign: existing key at %s has wrong length=%d (want 32) — "
                    "signing disabled; rotate the file to recover",
                    path,
                    len(raw),
                )
                return None
            return Ed25519PrivateKey.from_private_bytes(raw)
        log.info("forward-sign: no key at %s; generating fresh Ed25519 keypair", path)
        return _generate_and_persist(path)
    except OSError as e:
        log.error(
            "forward-sign: cannot load/create key at %s err=%s — signing disabled",
            path,
            e,
        )
        return None


# Process-level cache — lazy load on first call. Tests can monkeypatch
# the env var and call ``reload_l2_privkey()`` to get a fresh handle.
_cached_privkey: Ed25519PrivateKey | None = None
_cached_loaded: bool = False


def get_l2_privkey() -> Ed25519PrivateKey | None:
    """Return the cached private key (loading on first call). ``None``
    when load failed; callers must treat that as 'sign disabled, fall
    back to header-only identity declaration'.
    """
    global _cached_privkey, _cached_loaded  # noqa: PLW0603
    if not _cached_loaded:
        _cached_privkey = load_or_create_l2_privkey()
        _cached_loaded = True
    return _cached_privkey


def reload_l2_privkey() -> Ed25519PrivateKey | None:
    """Force a re-read from disk. Test hook + ops hook for hot rotate."""
    global _cached_privkey, _cached_loaded  # noqa: PLW0603
    _cached_loaded = False
    _cached_privkey = None
    return get_l2_privkey()


def self_public_key_b64u() -> str | None:
    """Base64url public key for inclusion in /aigrp/hello payloads.

    Returns ``None`` when signing is disabled — receivers then store
    NULL in ``aigrp_peers.public_key_ed25519`` for this peer, which
    correctly downgrades them to the legacy-unsigned code path.
    """
    pk = get_l2_privkey()
    if pk is None:
        return None
    return public_key_b64u(pk)


# ---------------------------------------------------------------------------
# Sign / verify a forward-* request
# ---------------------------------------------------------------------------


def signing_input_for(body: dict[str, Any], forwarder_l2_id: str) -> bytes:
    """Build the deterministic byte string we sign / verify.

    Layout:

        canonical_body_bytes || forwarder_l2_id_utf8

    where ``canonical_body_bytes`` is the RFC 8785 JCS canonicalisation
    of ``body`` and the concatenation is plain bytewise (no separator).
    Including ``forwarder_l2_id`` in the signed input prevents a
    sibling L2 with a leaked body+sig pair from replaying it under its
    own header; the bytes wouldn't match. The body alone isn't enough
    because the AIGRP forward-query body contains the requester id but
    /consults/forward-* bodies use ``from_l2_id`` — both are pinned via
    ``require_forwarder_identity`` already, but binding the header to
    the signature gives the receiver a single canonical thing to verify.
    """
    return canonicalize(body) + forwarder_l2_id.encode("utf-8")


def sign_forward_request(
    body: dict[str, Any], forwarder_l2_id: str
) -> str | None:
    """Sign a forward-* request body. Returns the b64url signature or
    ``None`` when signing is disabled (no key on disk).

    Caller adds the result as ``X-8L-Forwarder-Sig`` header. When
    ``None``, caller omits the header — receivers with the legacy code
    path accept that; receivers in strict mode reject it.
    """
    pk = get_l2_privkey()
    if pk is None:
        return None
    return sign_raw(pk, signing_input_for(body, forwarder_l2_id))


def verify_forward_signature(
    pubkey_b64u_str: str,
    body: dict[str, Any],
    forwarder_l2_id: str,
    signature_b64u: str,
) -> bool:
    """Verify a forward-* signature. False on any cryptographic failure."""
    return verify_raw(
        pubkey_b64u_str,
        signing_input_for(body, forwarder_l2_id),
        signature_b64u,
    )


# Convenience: expose the b64u helper on this module so callers can
# encode peer pubkeys for the wire without reaching into ``crypto``.
__all__ = [
    "DEFAULT_PRIVKEY_PATH",
    "SIGNATURE_HEADER",
    "b64u",
    "get_l2_privkey",
    "load_or_create_l2_privkey",
    "privkey_path",
    "reload_l2_privkey",
    "require_signed_forwards",
    "self_public_key_b64u",
    "sign_forward_request",
    "signing_input_for",
    "verify_forward_signature",
]
