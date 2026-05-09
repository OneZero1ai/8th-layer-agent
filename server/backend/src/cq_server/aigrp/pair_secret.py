"""Per-pair AIGRP secret derivation (Decision 28 §1.1).

Each L2 in an Enterprise holds the Enterprise AIGRP root and derives a
deterministic 32-byte ``pair_secret`` for each (self, peer) pair on demand
using HKDF-SHA256:

    pair_secret = HKDF-SHA256(
        ikm    = enterprise_root,                       # 32 bytes
        salt   = SHA-256("aigrp-pair-v1"),
        info   = b"aigrp-pair:" + lex_min(a, b) + b":" + lex_max(a, b),
        length = 32,
    )

Lexicographic min/max canonicalization gives both sides the same secret
without coordination — A→B and B→A both compute the same lex-canonical
``info`` string and therefore the same secret.

This module is intentionally I/O-free. It does not read SSM, does not
cache, does not log. ``enterprise_root.py`` owns the storage layer; this
module is the pure cryptographic kernel and is therefore trivial to
unit-test against KAT vectors.
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Versioned salt — bump the literal if/when the wire format changes.
# Hashed-salt avoids leaking version-string semantics to the HKDF output
# while keeping the version comparable in code.
_SALT_VERSION_LITERAL = b"aigrp-pair-v1"
_SALT = hashlib.sha256(_SALT_VERSION_LITERAL).digest()
_INFO_PREFIX = b"aigrp-pair:"
_PAIR_SECRET_BYTES = 32
_ROOT_BYTES = 32


def lex_min_canonical(l2_a_id: str, l2_b_id: str) -> bytes:
    """Return the lex-min canonical info bytes for a pair of L2 ids.

    Format: ``b"aigrp-pair:" + min(a,b).utf8 + b":" + max(a,b).utf8``.

    Lex-min canonicalization means ``derive_pair_secret(root, a, b)`` and
    ``derive_pair_secret(root, b, a)`` produce byte-identical secrets —
    the property that makes decentralized derivation work.

    Self-pairs (a == b) are rejected: there is no "pair" with yourself,
    and treating it as legal would silently mask a caller bug (e.g. a
    self-loop in the AIGRP mesh).
    """
    if not l2_a_id or not l2_b_id:
        raise ValueError("l2 ids must be non-empty")
    if l2_a_id == l2_b_id:
        raise ValueError(f"pair-secret requires distinct L2 ids; got {l2_a_id!r} twice")
    lo, hi = sorted((l2_a_id, l2_b_id))
    return _INFO_PREFIX + lo.encode("utf-8") + b":" + hi.encode("utf-8")


def derive_pair_secret(
    enterprise_root: bytes,
    l2_a_id: str,
    l2_b_id: str,
) -> bytes:
    """Derive the 32-byte AIGRP pair-secret for ``(l2_a_id, l2_b_id)``.

    Args:
        enterprise_root: 32-byte symmetric Enterprise AIGRP root. Caller
            is responsible for fetching this from the per-Enterprise
            SecureString (see ``enterprise_root.py``); this function
            never touches SSM.
        l2_a_id: one side of the pair (caller's L2 id, typically).
        l2_b_id: the other side. ``a == b`` is rejected.

    Returns:
        32 bytes of HKDF-SHA256 output, suitable for use as an HMAC key
        on the AIGRP envelope (§1.6).

    Raises:
        ValueError: on malformed inputs (wrong root length, empty/equal ids).
    """
    if not isinstance(enterprise_root, bytes | bytearray):
        raise TypeError(f"enterprise_root must be bytes, got {type(enterprise_root).__name__}")
    if len(enterprise_root) != _ROOT_BYTES:
        raise ValueError(f"enterprise_root must be {_ROOT_BYTES} bytes, got {len(enterprise_root)}")
    info = lex_min_canonical(l2_a_id, l2_b_id)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_PAIR_SECRET_BYTES,
        salt=_SALT,
        info=info,
    ).derive(bytes(enterprise_root))


__all__ = [
    "derive_pair_secret",
    "lex_min_canonical",
]
