"""Activity log of record — shared module for #108 Stage 1 substrate.

Stage 1 (this PR) ships the schema + a minimal store helper that
Stage 2 (instrumentation) calls from FastAPI ``BackgroundTask`` writes
on every query / propose / confirm / flag / review / consult /
crosstalk handler. Stage 2 also adds the ``GET /api/v1/activity``
read endpoint.

Single source of truth for:

* The locked event-type enum (mirrors the CHECK constraint in
  ``alembic/versions/0011_activity_log.py``).
* The ``act_<ULID>`` row-id generator.
* The ``Z``-suffix ISO-8601 timestamp helper used on the wire and in
  the row.
* The default 90-day retention constant.

Nothing here imports the store. Store-bound logic lives on
``SqliteStore.append_activity`` / ``purge_activity_older_than``.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "EVENT_TYPES",
    "generate_activity_id",
    "now_iso_z",
]


# Locked enum — must stay in sync with the CHECK constraint in
# ``alembic/versions/0011_activity_log.py`` and with the schema
# sketch in issue #108. Adding a new value requires a new Alembic
# migration that swaps the constraint via batch-recreate.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "query",
        "propose",
        "confirm",
        "flag",
        "review_start",
        "review_resolve",
        "crosstalk_send",
        "crosstalk_reply",
        "crosstalk_close",
        "consult_open",
        "consult_reply",
        "consult_close",
    }
)


# Default retention window. Per-enterprise overrides live in
# ``activity_retention_config(enterprise_id, retention_days, ...)``.
# Absence of a row means "use this default".
DEFAULT_RETENTION_DAYS: int = 90


_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret


def generate_activity_id() -> str:
    """Produce an ``act_<26-char-ULID>`` id.

    Lexicographically sortable on insert order: 10-char Crockford-base32
    timestamp (millis since epoch) + 16 chars of cryptographic
    randomness. Same shape ``python-ulid`` produces; inlined to avoid
    a dependency for one helper, mirroring ``reflect._generate_submission_id``.
    """
    millis = int(time.time() * 1000)
    ts_chars: list[str] = []
    for _ in range(10):
        ts_chars.append(_CROCKFORD_BASE32[millis & 0x1F])
        millis >>= 5
    ts_part = "".join(reversed(ts_chars))
    rand_part = "".join(secrets.choice(_CROCKFORD_BASE32) for _ in range(16))
    return f"act_{ts_part}{rand_part}"


def now_iso_z() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix.

    Same convention as ``cq_server.reflect._iso``: emit ``Z`` rather
    than ``+00:00`` so wire payloads, log rows, and dashboard renders
    all agree on a single timestamp shape.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
