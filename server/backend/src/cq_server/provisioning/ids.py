"""ULID-style job ID generator for provisioning jobs.

Produces ``prov_<26-char-ULID>`` identifiers: 10 Crockford-base32 chars
of millisecond timestamp followed by 16 chars of cryptographic randomness.
Same pattern as ``cq_server.activity.generate_activity_id`` — inlined
here to stay dependency-free.

Decision 31 §Authentication: job_id is a 26-char ULID under a ``prov_``
prefix so it is unguessable and single-use (DB-enforced unique PK).
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret


def generate_job_id() -> str:
    """Return a ``prov_<26-char ULID>`` string."""
    millis = int(time.time() * 1000)
    ts_chars: list[str] = []
    for _ in range(10):
        ts_chars.append(_CROCKFORD[millis & 0x1F])
        millis >>= 5
    ts_part = "".join(reversed(ts_chars))
    rand_part = "".join(secrets.choice(_CROCKFORD) for _ in range(16))
    return f"prov_{ts_part}{rand_part}"
