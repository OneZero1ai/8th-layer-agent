"""``transactional_suppression`` read/write helpers (Decision 34).

Two callers:

* Send path (:mod:`transactional.routes`) — checks ``check_suppression``
  before SES dispatch; 409s on a hit.
* Bounce/complaint writer (Lambda or local worker subscribed to the
  SES → SNS topics) — calls ``record_suppression`` from the
  ``ses-bounces`` / ``ses-complaints`` topic handler.

The reads/writes go through the existing ``SqliteStore`` ``_engine``
so they share the same connection pool + PRAGMAs as the rest of
``cq-server``. Plain SQLAlchemy ``text()`` — no ORM mapping for a
4-column table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)


@dataclass
class SuppressionEntry:
    """One row of ``transactional_suppression``."""

    address: str
    reason: str
    suppressed_at: str
    source_event_id: str | None


def check_suppression(store: Any, address: str) -> SuppressionEntry | None:
    """Return the suppression row for ``address`` if any, else None.

    ``address`` is lowercased before the lookup — the writer also
    lowercases on insert, so the two converge on the same canonical
    form.

    Synchronous; the send path is itself sync code (the route is
    ``async def`` but the SQLite I/O is sync). Keeping this as a
    plain function avoids the async/sync seam adding noise for a
    single-row PK lookup.
    """
    engine = store._engine  # noqa: SLF001 — direct engine access matches store conventions
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT address, reason, suppressed_at, source_event_id "
                "FROM transactional_suppression WHERE address = :a"
            ),
            {"a": address.lower()},
        ).fetchone()
    if row is None:
        return None
    return SuppressionEntry(
        address=row[0],
        reason=row[1],
        suppressed_at=row[2],
        source_event_id=row[3],
    )


def record_suppression(
    store: Any,
    *,
    address: str,
    reason: str,
    source_event_id: str | None = None,
    suppressed_at: str | None = None,
) -> bool:
    """Insert a suppression row. Returns True if inserted, False if dup.

    Idempotent: the writer is subscribed to SNS, and SNS at-least-once
    delivery means we will occasionally see duplicate events. PK on
    ``address`` + ``INSERT OR IGNORE`` semantics means the second hit
    is a clean no-op. First reason wins (see migration docstring).
    """
    suppressed_at = suppressed_at or datetime.now(UTC).isoformat()
    engine = store._engine  # noqa: SLF001
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO transactional_suppression "
                "(address, reason, suppressed_at, source_event_id) "
                "VALUES (:a, :r, :s, :e)"
            ),
            {
                "a": address.lower(),
                "r": reason,
                "s": suppressed_at,
                "e": source_event_id,
            },
        )
    inserted = (result.rowcount or 0) > 0
    if inserted:
        log.info("suppression recorded address=%s reason=%s", address.lower(), reason)
    return inserted
