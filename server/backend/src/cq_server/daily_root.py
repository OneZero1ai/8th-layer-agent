"""Daily Merkle root computation + persistence (task #108 sub-task 3).

Per [decision 13](crosstalk-enterprise/docs/decisions/13) §"daily root publish",
the L2 computes a SHA-256 Merkle tree over each UTC day's reputation_events
and writes the signed root to ``reputation_roots``. A separate
publish step (sub-task 4) POSTs the row to the directory.

Two entry points:

- ``compute_root_for_day(conn, enterprise_id, day_iso)`` — synchronous
  helper. Idempotent: if a row already exists for that
  (enterprise_id, day) it returns the existing row instead of
  recomputing. Tests can call directly.

- ``daily_root_loop(get_store)`` — asyncio task started in app.py
  lifespan. Sleeps until the next UTC midnight, then computes the
  prior day's root for every Enterprise present in
  ``reputation_chain_meta``. Errors are logged; the loop never
  exits (runs for the lifetime of the process).

Single-writer assumption: in v1, one cq-server per Enterprise computes
the root. Multi-L2 leadership handover is decision-13 future work.
For now we serialize via ``reputation_chain_meta.last_root_published_day``
— a process that finds that column already advanced for the target
day skips its own write (idempotent + race-safe under SQLite WAL).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .merkle import merkle_root
from .reputation import (
    canonical_payload_bytes,
    sign_canonical_bytes,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_window(day_iso: str) -> tuple[str, str]:
    """Return (start_ts, end_ts) covering one UTC day.

    Reputation event ts strings are RFC 3339 / ISO-8601 UTC
    (``YYYY-MM-DDTHH:MM:SSZ``). Lexicographic comparison on those
    strings is correct for date-windowed queries — no need to parse.
    """
    return f"{day_iso}T00:00:00Z", f"{day_iso}T23:59:59Z"


def _read_day_events(conn: sqlite3.Connection, enterprise_id: str, day_iso: str) -> list[dict[str, Any]]:
    """Return events for ``enterprise_id`` on ``day_iso``, ordered by ts ASC."""
    start_ts, end_ts = _day_window(day_iso)
    rows = conn.execute(
        """
        SELECT event_id, ts, payload_hash
        FROM reputation_events
        WHERE enterprise_id = ?
          AND ts >= ? AND ts <= ?
        ORDER BY ts ASC, event_id ASC
        """,
        (enterprise_id, start_ts, end_ts),
    ).fetchall()
    return [{"event_id": r[0], "ts": r[1], "payload_hash": r[2]} for r in rows]


def _existing_root(conn: sqlite3.Connection, enterprise_id: str, day_iso: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT enterprise_id, root_date, event_count, merkle_root_hash,
               first_event_id, last_event_id, signature_b64u, signing_key_id,
               computed_at, published_to_directory_at
        FROM reputation_roots
        WHERE enterprise_id = ? AND root_date = ?
        """,
        (enterprise_id, day_iso),
    ).fetchone()
    if row is None:
        return None
    return {
        "enterprise_id": row[0],
        "root_date": row[1],
        "event_count": row[2],
        "merkle_root_hash": row[3],
        "first_event_id": row[4],
        "last_event_id": row[5],
        "signature_b64u": row[6],
        "signing_key_id": row[7],
        "computed_at": row[8],
        "published_to_directory_at": row[9],
    }


def compute_root_for_day(conn: sqlite3.Connection, enterprise_id: str, day_iso: str) -> dict[str, Any]:
    """Compute (or fetch existing) Merkle root for one Enterprise-day.

    Idempotent — returns the existing row if already computed. The
    canonical payload signed is the JCS form of
    ``{enterprise_id, root_date, event_count, merkle_root_hash,
       first_event_id, last_event_id}``; signature lands in
    ``signature_b64u`` next to the row.

    Args:
        conn: open sqlite3 connection (caller owns the transaction).
        enterprise_id: tenant id whose chain we're rolling up.
        day_iso: ``YYYY-MM-DD`` UTC date.

    Returns:
        The persisted row as a dict.
    """
    existing = _existing_root(conn, enterprise_id, day_iso)
    if existing is not None:
        return existing

    events = _read_day_events(conn, enterprise_id, day_iso)
    leaf_hashes = [e["payload_hash"] for e in events]
    root_hash = merkle_root(leaf_hashes)
    first_event_id = events[0]["event_id"] if events else None
    last_event_id = events[-1]["event_id"] if events else None
    event_count = len(events)
    computed_at = _utc_now_iso()

    payload = {
        "enterprise_id": enterprise_id,
        "root_date": day_iso,
        "event_count": event_count,
        "merkle_root_hash": root_hash,
        "first_event_id": first_event_id,
        "last_event_id": last_event_id,
    }
    canonical = canonical_payload_bytes(payload)
    signature_b64u, signing_key_id = sign_canonical_bytes(canonical)

    conn.execute(
        """
        INSERT INTO reputation_roots
            (enterprise_id, root_date, event_count, merkle_root_hash,
             first_event_id, last_event_id, signature_b64u, signing_key_id,
             computed_at, published_to_directory_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            enterprise_id,
            day_iso,
            event_count,
            root_hash,
            first_event_id,
            last_event_id,
            signature_b64u,
            signing_key_id,
            computed_at,
        ),
    )

    # Bump chain meta so a subsequent run on the same day no-ops.
    conn.execute(
        """
        UPDATE reputation_chain_meta
        SET last_root_published_day = ?, updated_at = ?
        WHERE enterprise_id = ? AND (
            last_root_published_day IS NULL OR last_root_published_day < ?
        )
        """,
        (day_iso, computed_at, enterprise_id, day_iso),
    )

    return {
        "enterprise_id": enterprise_id,
        "root_date": day_iso,
        "event_count": event_count,
        "merkle_root_hash": root_hash,
        "first_event_id": first_event_id,
        "last_event_id": last_event_id,
        "signature_b64u": signature_b64u,
        "signing_key_id": signing_key_id,
        "computed_at": computed_at,
        "published_to_directory_at": None,
    }


def _all_enterprise_ids(conn: sqlite3.Connection) -> list[str]:
    """Return Enterprise ids that have any reputation activity.

    Sources from ``reputation_chain_meta`` rather than scanning events
    directly — chain meta is one row per Enterprise so this is cheap.
    """
    rows = conn.execute("SELECT enterprise_id FROM reputation_chain_meta").fetchall()
    return [r[0] for r in rows]


def _seconds_until_next_utc_midnight() -> float:
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).date()
    next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
    return (next_midnight - now).total_seconds()


async def daily_root_loop(get_conn: Callable[[], sqlite3.Connection]) -> None:
    """Asyncio task: roll up yesterday's events at every UTC midnight.

    Started by ``app.py`` lifespan. Sleeps until next midnight, then
    iterates every Enterprise present in ``reputation_chain_meta`` and
    calls ``compute_root_for_day`` for the prior UTC day. Errors are
    logged per-Enterprise — one bad day doesn't break the loop.

    Args:
        get_conn: zero-arg callable returning a fresh sqlite3 connection
            against the L2's database. The loop owns the lifecycle of
            each connection it opens (per-iteration), so the underlying
            Store can keep its own.
    """
    while True:
        try:
            sleep_for = _seconds_until_next_utc_midnight()
            logger.info(
                "daily_root_loop: sleeping %.0fs until next UTC midnight",
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            logger.info("daily_root_loop: cancelled, exiting")
            return

        yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        try:
            conn = get_conn()
            try:
                for enterprise_id in _all_enterprise_ids(conn):
                    try:
                        result = compute_root_for_day(conn, enterprise_id, yesterday)
                        conn.commit()
                        logger.info(
                            "daily_root_loop: computed root enterprise=%s day=%s events=%d root=%s",
                            enterprise_id,
                            yesterday,
                            result["event_count"],
                            result["merkle_root_hash"],
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "daily_root_loop: failed for enterprise=%s day=%s",
                            enterprise_id,
                            yesterday,
                            exc_info=True,
                        )
                        with contextlib.suppress(sqlite3.Error):
                            conn.rollback()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — never let this loop die
            logger.warning("daily_root_loop: outer iteration crashed", exc_info=True)


def compute_yesterday_root_now(conn: sqlite3.Connection, enterprise_id: str) -> dict[str, Any]:
    """Convenience: compute yesterday's root immediately (no scheduling).

    Used by tests + an admin trigger endpoint. Never called from the
    loop itself — the loop computes inline.
    """
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    return compute_root_for_day(conn, enterprise_id, yesterday)
