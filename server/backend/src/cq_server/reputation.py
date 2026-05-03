"""Reputation log v1-alpha — append-only hash chain.

Per [decision 13](crosstalk-enterprise/docs/decisions/13) and
[reputation-v1 spec](crosstalk-enterprise/docs/specs/reputation-v1.md).

This module is the writer-side foundation: it builds canonical event
records, hash-chains them via ``prev_event_hash``, persists to the
``reputation_events`` table, and updates ``reputation_chain_meta``.

What's IN v1-alpha (this module):
    * Event canonicalisation via RFC 8785 (JCS).
    * Hash chain via SHA-256 over the canonical bytes.
    * ``record_event(...)`` helper — single API surface for callers.

What's deferred to v1 (follow-up):
    * Ed25519 signing (``signature_b64u`` is NULL in alpha; column
      already exists in the schema so signing lands without another
      migration).
    * Daily Merkle root publish to the directory.
    * Sibling-L2 chain leader lease (single-L2 enterprise only in
      alpha — chain hash is per-enterprise but writes are
      single-writer).

Callers hook ``record_event`` at the three event sites called out in
decision 13: consult-close, KU lifecycle transitions, AIGRP peer
heartbeat. Hooks are best-effort: a failure to record an event must
NOT break the original operation, since recording is downstream of
the actual state change. ``record_event`` swallows + logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "GENESIS_PREV_HASH",
    "canonical_payload_bytes",
    "compute_payload_hash",
    "make_event_id",
    "record_event",
]

logger = logging.getLogger(__name__)

# Genesis sentinel for the first event in a chain. Per spec
# (`reputation-v1.md` §"Chain rule").
GENESIS_PREV_HASH = "sha256:" + ("0" * 64)


def make_event_id() -> str:
    """Return a fresh ``evt_<random>`` event id."""
    return f"evt_{secrets.token_hex(16)}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Return RFC-8785-canonicalised JSON bytes for the payload.

    Stand-in implementation: ``json.dumps`` with ``sort_keys=True``,
    tight separators, and ``ensure_ascii=False`` so non-ASCII
    characters are emitted as raw UTF-8 (per RFC 8785 §3.2.2) instead
    of ``\\uXXXX`` escapes. Without ``ensure_ascii=False``, a body
    containing any accented character (persona name, summary fragment)
    would produce canonical bytes that differ from a conformant JCS
    verifier's output — once Ed25519 signing lands, signatures would
    only verify against another Python-default mistake. The directory's
    ``/announce`` path uses the same approach today; we follow it so
    verifier code is shared. Float serialisation (RFC 8785 requires
    Grisu3/Dragon4) is still a future swap-in for a real JCS library;
    avoid float values in event bodies until then.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_payload_hash(canonical_bytes: bytes) -> str:
    """Return ``sha256:<hex>`` for the canonical bytes."""
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def _self_l2_id() -> str:
    """Best-effort self L2 id for this process.

    Mirrors ``aigrp.self_l2_id()`` but kept inline to avoid an import
    cycle (aigrp.py would otherwise import reputation, which imports
    the store, which imports ...). Reads ``CQ_ENTERPRISE`` and
    ``CQ_GROUP`` from env, returns ``"<enterprise>/<group>"``.
    """
    enterprise = os.environ.get("CQ_ENTERPRISE", "default-enterprise")
    group = os.environ.get("CQ_GROUP", "default-group")
    return f"{enterprise}/{group}"


def _enterprise_id() -> str:
    return os.environ.get("CQ_ENTERPRISE", "default-enterprise")


def _read_chain_meta(conn: sqlite3.Connection, enterprise_id: str) -> tuple[str | None, str]:
    """Return ``(last_event_id, last_event_hash)`` for this Enterprise."""
    row = conn.execute(
        "SELECT last_event_id, last_event_hash FROM reputation_chain_meta WHERE enterprise_id = ?",
        (enterprise_id,),
    ).fetchone()
    if row is None:
        return None, GENESIS_PREV_HASH
    return row[0], row[1]


def _upsert_chain_meta(
    conn: sqlite3.Connection,
    enterprise_id: str,
    last_event_id: str,
    last_event_hash: str,
) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO reputation_chain_meta
            (enterprise_id, last_event_id, last_event_hash, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(enterprise_id) DO UPDATE SET
            last_event_id = excluded.last_event_id,
            last_event_hash = excluded.last_event_hash,
            updated_at = excluded.updated_at
        """,
        (enterprise_id, last_event_id, last_event_hash, now),
    )


def record_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    body: dict[str, Any],
    enterprise_id: str | None = None,
    l2_id: str | None = None,
    ts: str | None = None,
) -> str | None:
    """Append one event to the reputation chain. Returns event_id.

    Best-effort: any exception is caught and logged at WARNING; the
    caller's original operation is not affected. This lets callers
    hook the helper without worrying about reputation-write failures
    (a missing event is bad but a broken consult-close is worse).

    Args:
        conn: An open SQLite connection. The caller owns the
            transaction; ``record_event`` does not commit. (Wrap the
            call in the same transaction as the underlying state
            change so an event without state, or vice versa, is not
            possible.)
        event_type: One of ``consult.closed``, ``ku.event``,
            ``peer.heartbeat``. Validated lightly here.
        body: Event-type-specific body. See ``reputation-v1.md``.
        enterprise_id, l2_id, ts: Optional overrides. Default to
            this L2's identity + UTC now.

    Returns:
        The new event_id on success, or ``None`` if recording was
        skipped (logged) or failed (logged at WARNING).
    """
    # SAVEPOINT wraps the two writes (event row + chain-meta upsert) so
    # that a failure in either is rolled back together inside the
    # caller's outer transaction. Without this, an INSERT-then-upsert
    # sequence could commit a partial state: the event row lands but
    # `last_event_hash` stays stale, silently forking the chain on the
    # next call. The savepoint ROLLBACK touches only reputation-layer
    # work — the caller's surrounding state-change transaction remains
    # intact, preserving the "best-effort, never breaks the caller"
    # contract documented above.
    savepoint_open = False
    try:
        ent = enterprise_id or _enterprise_id()
        l2 = l2_id or _self_l2_id()
        ts_str = ts or _utc_now_iso()

        conn.execute("SAVEPOINT rep_write")
        savepoint_open = True

        last_event_id, prev_event_hash = _read_chain_meta(conn, ent)

        event_id = make_event_id()
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "enterprise_id": ent,
            "l2_id": l2,
            "ts": ts_str,
            "prev_event_hash": prev_event_hash,
            "body": body,
        }
        canonical = canonical_payload_bytes(payload)
        payload_hash = compute_payload_hash(canonical)

        conn.execute(
            """
            INSERT INTO reputation_events
                (event_id, event_type, enterprise_id, l2_id, ts,
                 prev_event_hash, payload_canonical, payload_hash,
                 signature_b64u, signing_key_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                event_id,
                event_type,
                ent,
                l2,
                ts_str,
                prev_event_hash,
                canonical.decode("utf-8"),
                payload_hash,
                _utc_now_iso(),
            ),
        )
        _upsert_chain_meta(conn, ent, event_id, payload_hash)
        conn.execute("RELEASE SAVEPOINT rep_write")
        return event_id
    except Exception:  # noqa: BLE001 — this MUST NOT break callers
        if savepoint_open:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT rep_write")
                conn.execute("RELEASE SAVEPOINT rep_write")
            except Exception:  # noqa: BLE001
                # If even the rollback fails, the connection is in a
                # weird place — but we still must not propagate.
                logger.warning(
                    "reputation: SAVEPOINT rollback failed; connection "
                    "may be in inconsistent state",
                    exc_info=True,
                )
        logger.warning(
            "reputation: failed to record %s event; chain not advanced",
            event_type,
            exc_info=True,
        )
        return None
