"""Shared Ed25519 + RFC 8785 primitives.

Sprint 4 extracted these helpers from ``directory_client.py`` so they can be
reused by the per-L2 forward-signing path (`forward_sign.py`) without a
circular import. Two callers, one canonical implementation:

- ``directory_client.py``  — enterprise-ROOT keypair, signs /announce + peerings pull
- ``forward_sign.py``      — per-L2 keypair, signs forward-* request bodies

The two key scopes are intentionally separate. The directory key is the
enterprise's identity to the public 8th-Layer directory; the per-L2 key
is the L2's identity to its sibling L2s on the AIGRP mesh. They never
cross paths; key rotation on one does not affect the other.

All bytes that get signed are produced by ``canonicalize`` (RFC 8785 JCS)
so verifiers see byte-identical input regardless of dict ordering or
JSON whitespace.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ---------------------------------------------------------------------------
# base64url helpers (no padding) — directory + forward-sign both use this
# ---------------------------------------------------------------------------


def b64u(b: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_decode(s: str) -> bytes:
    """Decode unpadded base64url back to bytes."""
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


# ---------------------------------------------------------------------------
# Ed25519 key load / public-key extraction
# ---------------------------------------------------------------------------


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a 32-byte raw file."""
    raw = path.read_bytes()
    if len(raw) != 32:
        raise ValueError(f"Ed25519 private key must be 32 raw bytes, got {len(raw)}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_b64u(privkey: Ed25519PrivateKey) -> str:
    """Return the unpadded base64url-encoded raw public key."""
    pub = privkey.public_key().public_bytes_raw()
    return b64u(pub)


def fingerprint_sha256(pubkey_b64u: str) -> str:
    """Match the directory's fingerprint format (`sha256:<hex>`)."""
    raw = b64u_decode(pubkey_b64u)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ---------------------------------------------------------------------------
# Canonical-JSON sign / verify (RFC 8785 envelope shape)
# ---------------------------------------------------------------------------


def canonicalize(payload: dict[str, Any]) -> bytes:
    """RFC 8785 JCS canonical JSON bytes for ``payload``."""
    return rfc8785.dumps(payload)


def sign_envelope(privkey: Ed25519PrivateKey, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a signed envelope per directory-v1 spec.

    Returns the dict the directory expects on the wire:
    ``{payload, payload_canonical, signature, signing_key_id}``.
    """
    canonical = canonicalize(payload)
    signature = privkey.sign(canonical)
    return {
        "payload": payload,
        "payload_canonical": canonical.decode(),
        "signature": b64u(signature),
        "signing_key_id": public_key_b64u(privkey),
    }


def verify_envelope_signature(
    pubkey_b64u: str,
    payload_canonical: str,
    signature_b64u: str,
) -> bool:
    """Constant-time Ed25519 verify of a signed-envelope payload.

    Returns True on success, False on any cryptographic failure (we
    never raise on verify; callers decide policy).
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64u_decode(pubkey_b64u))
        pub.verify(b64u_decode(signature_b64u), payload_canonical.encode())
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# Raw (non-envelope) sign / verify — forward-* uses this path because
# the wire body is a normal JSON request, not a signed envelope. The
# bytes signed are JCS(body) || forwarder_l2_id_bytes (concatenation;
# see forward_sign.signing_input_for docstring).
# ---------------------------------------------------------------------------


def sign_raw(privkey: Ed25519PrivateKey, message: bytes) -> str:
    """Sign arbitrary bytes; return the unpadded-base64url signature."""
    return b64u(privkey.sign(message))


def verify_raw(pubkey_b64u_str: str, message: bytes, signature_b64u: str) -> bool:
    """Verify a raw-bytes signature. False on any failure."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64u_decode(pubkey_b64u_str))
        pub.verify(b64u_decode(signature_b64u), message)
        return True
    except (InvalidSignature, ValueError):
        return False
