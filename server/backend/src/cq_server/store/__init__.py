"""SQLite-backed remote knowledge store.

Stores knowledge units in a SQLite database for remote sharing.
Auto-creates the database directory and schema on first use.
Implements the context manager protocol for deterministic resource cleanup.
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from cq.models import KnowledgeUnit

from ..scoring import calculate_relevance
from ..tables import (
    DEFAULT_ENTERPRISE_ID,
    DEFAULT_GROUP_ID,
    ensure_aigrp_peers_table,
    ensure_directory_peerings_schema,
    ensure_api_keys_table,
    ensure_consults_schema,
    ensure_embedding_columns,
    ensure_peers_schema,
    ensure_review_columns,
    ensure_tenancy_columns,
    ensure_user_role_column,
    ensure_users_table,
    ensure_xgroup_consent_schema,
)
from ._protocol import Store

__all__ = ["DEFAULT_DB_PATH", "RemoteStore", "Store", "normalize_domains"]

_logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("/data/cq.db")

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS knowledge_units (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    enterprise_id TEXT NOT NULL DEFAULT '{DEFAULT_ENTERPRISE_ID}',
    group_id TEXT NOT NULL DEFAULT '{DEFAULT_GROUP_ID}'
);

CREATE TABLE IF NOT EXISTS knowledge_unit_domains (
    unit_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    FOREIGN KEY (unit_id) REFERENCES knowledge_units(id) ON DELETE CASCADE,
    PRIMARY KEY (unit_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_domains_domain
    ON knowledge_unit_domains(domain);
"""


def normalize_domains(domains: list[str]) -> list[str]:
    """Lowercase, strip whitespace, drop empties, and deduplicate domain tags."""
    return list(dict.fromkeys(d.strip().lower() for d in domains if d.strip()))


class RemoteStore:
    """SQLite-backed remote knowledge store.

    Holds a single persistent connection for the lifetime of the instance.
    Use as a context manager or call ``close()`` explicitly.

    Thread-safe: all connection access is serialized via an internal lock.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise the store, creating the database and schema if needed.

        Args:
            db_path: Path to the SQLite database file. Defaults to /data/cq.db.
        """
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._lock = threading.Lock()
        self._conn = self._open_connection()
        self._ensure_schema()

    def _open_connection(self) -> sqlite3.Connection:
        """Open and configure a SQLite connection."""
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        self._conn.executescript(_SCHEMA_SQL)
        ensure_review_columns(self._conn)
        ensure_embedding_columns(self._conn)
        ensure_users_table(self._conn)
        ensure_api_keys_table(self._conn)
        ensure_aigrp_peers_table(self._conn)
        # Sprint 2 (issue #20) — L3 consult tables. Same idempotent
        # shape; safe to call on every startup.
        ensure_consults_schema(self._conn)
        # Sprint 3 — directory peering mirror. Idempotent.
        ensure_directory_peerings_schema(self._conn)
        # Phase 6 step 1 — additive tenancy columns. Idempotent; safe to
        # run on every startup. Backfills legacy rows so the columns can
        # be queried without NULL handling once enforcement lands.
        ensure_tenancy_columns(self._conn)
        # Phase 6 step 2 — cross-group flag + cross-Enterprise consent
        # registry + cross-L2 audit log. Same idempotent shape so the
        # runtime path matches the Alembic migration.
        ensure_xgroup_consent_schema(self._conn)
        # Phase 6 step 3 — admin role column + presence registry. Both
        # idempotent; the ``peers`` table is created lazily on every
        # startup so a legacy DB picks it up without an explicit migration.
        ensure_user_role_column(self._conn)
        ensure_peers_schema(self._conn)

    def _check_open(self) -> None:
        """Raise if the store has been closed."""
        if self._closed:
            raise RuntimeError("RemoteStore is closed")

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._closed:
            return
        self._closed = True
        self._conn.close()

    def __enter__(self) -> "RemoteStore":
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager, closing the connection."""
        self.close()

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""
        return self._db_path

    def insert(
        self,
        unit: KnowledgeUnit,
        *,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Insert a knowledge unit into the store.

        Args:
            unit: The knowledge unit to insert.
            embedding: Optional packed float32 LE bytes from Titan (or other model).
            embedding_model: The model id that produced the embedding.

        Raises:
            sqlite3.IntegrityError: If a unit with the same ID already exists.
            ValueError: If domain normalization results in no valid domains.
        """
        self._check_open()
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        unit = unit.model_copy(update={"domains": domains})
        data = unit.model_dump_json()
        created_at = (
            unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else datetime.now(UTC).isoformat()
        )
        with self._lock, self._conn:
            # Phase 6 step 1: stamp default tenancy scope on every new
            # row. Future PRs will pull these from JWT claims (and an
            # API-key payload extension); for now the columns are
            # additive and every row lands in the default scope.
            self._conn.execute(
                "INSERT INTO knowledge_units "
                "(id, data, created_at, tier, embedding, embedding_model, "
                "enterprise_id, group_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    unit.id,
                    data,
                    created_at,
                    unit.tier.value,
                    embedding,
                    embedding_model,
                    DEFAULT_ENTERPRISE_ID,
                    DEFAULT_GROUP_ID,
                ),
            )
            self._conn.executemany(
                "INSERT INTO knowledge_unit_domains (unit_id, domain) VALUES (?, ?)",
                [(unit.id, d) for d in domains],
            )

    def set_embedding(self, unit_id: str, embedding: bytes, embedding_model: str) -> bool:
        """Update the embedding for an existing KU. Used by the backfill script.

        Returns True if a row was updated, False if no such ID existed.
        """
        self._check_open()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_units SET embedding = ?, embedding_model = ? WHERE id = ?",
                (embedding, embedding_model, unit_id),
            )
            return cur.rowcount > 0

    def iter_unembedded(self, *, status: str = "approved", limit: int = 1000) -> list[tuple[str, str]]:
        """Return (id, data) for KUs with NULL embedding, used for backfill."""
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, data FROM knowledge_units "
                "WHERE embedding IS NULL AND status = ? LIMIT ?",
                (status, limit),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def semantic_query(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        status: str = "approved",
    ) -> list[tuple[KnowledgeUnit, float]]:
        """Brute-force cosine similarity over all KUs with embeddings.

        Returns list of (unit, similarity) sorted by similarity desc.
        At ~1k KUs in 1024-dim, this is sub-50ms in numpy. Swap for
        sqlite-vss / pgvector when corpus exceeds ~10k.
        """
        import numpy as np

        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, embedding FROM knowledge_units "
                "WHERE status = ? AND embedding IS NOT NULL",
                (status,),
            ).fetchall()
        if not rows:
            return []

        query = np.array(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            return []
        query = query / q_norm

        scored: list[tuple[KnowledgeUnit, float]] = []
        for data_str, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.size == 0:
                continue
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            sim = float(np.dot(query, vec / v_norm))
            unit = KnowledgeUnit.model_validate_json(data_str)
            scored.append((unit, sim))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def delete(self, unit_id: str, *, enterprise_id: str | None = None) -> bool:
        """Hard-delete a knowledge unit by ID.

        When ``enterprise_id`` is provided, the row is only deleted if it
        belongs to that Enterprise — cross-tenant deletes return False
        (same shape as missing-id, so probes can't fingerprint other
        tenants' IDs).

        Returns True if a row was deleted, False if no such ID existed
        (or it was out of tenant scope).
        """
        self._check_open()
        with self._lock, self._conn:
            if enterprise_id is not None:
                row = self._conn.execute(
                    "SELECT 1 FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
                if row is None:
                    return False
            self._conn.execute(
                "DELETE FROM knowledge_unit_domains WHERE unit_id = ?",
                (unit_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM knowledge_units WHERE id = ?",
                (unit_id,),
            )
            return cur.rowcount > 0

    def get(self, unit_id: str) -> KnowledgeUnit | None:
        """Retrieve an approved knowledge unit by ID.

        Agent-facing: only returns KUs that have passed human review.
        For internal access regardless of status, use get_any().

        Args:
            unit_id: The knowledge unit identifier.

        Returns:
            The knowledge unit, or None if not found or not approved.
        """
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM knowledge_units WHERE id = ? AND status = 'approved'",
                (unit_id,),
            ).fetchone()
        if row is None:
            return None
        return KnowledgeUnit.model_validate_json(row[0])

    def get_any(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
    ) -> KnowledgeUnit | None:
        """Retrieve a knowledge unit by ID regardless of review status.

        Internal use only — review endpoints and activity feed. When
        ``enterprise_id`` is provided, returns None for KUs in other
        tenants (same shape as missing-id, no fingerprinting).
        """
        self._check_open()
        with self._lock:
            if enterprise_id is None:
                row = self._conn.execute(
                    "SELECT data FROM knowledge_units WHERE id = ?",
                    (unit_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT data FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
        if row is None:
            return None
        return KnowledgeUnit.model_validate_json(row[0])

    def get_review_status(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
    ) -> dict[str, str | None] | None:
        """Return review metadata for a knowledge unit.

        When ``enterprise_id`` is provided, returns None for KUs in other
        tenants.
        """
        self._check_open()
        with self._lock:
            if enterprise_id is None:
                row = self._conn.execute(
                    "SELECT status, reviewed_by, reviewed_at "
                    "FROM knowledge_units WHERE id = ?",
                    (unit_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT status, reviewed_by, reviewed_at "
                    "FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "reviewed_by": row[1], "reviewed_at": row[2]}

    def set_review_status(
        self,
        unit_id: str,
        status: str,
        reviewed_by: str,
        *,
        enterprise_id: str | None = None,
    ) -> None:
        """Update the review status of a knowledge unit.

        When ``enterprise_id`` is provided, the row is only updated if it
        belongs to that Enterprise — cross-tenant updates raise KeyError.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            if enterprise_id is None:
                cursor = self._conn.execute(
                    "UPDATE knowledge_units SET status = ?, reviewed_by = ?, "
                    "reviewed_at = ? WHERE id = ?",
                    (status, reviewed_by, now, unit_id),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE knowledge_units SET status = ?, reviewed_by = ?, "
                    "reviewed_at = ? WHERE id = ? AND enterprise_id = ?",
                    (status, reviewed_by, now, unit_id, enterprise_id),
                )
            if cursor.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit_id}")

    def update(self, unit: KnowledgeUnit) -> None:
        """Replace an existing knowledge unit in the store.

        Args:
            unit: The updated knowledge unit.

        Raises:
            KeyError: If no unit with the given ID exists.
            ValueError: If domain normalization results in no valid domains.
        """
        self._check_open()
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        unit = unit.model_copy(update={"domains": domains})
        data = unit.model_dump_json()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE knowledge_units SET data = ?, tier = ? WHERE id = ?",
                (data, unit.tier.value, unit.id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit.id}")
            self._conn.execute(
                "DELETE FROM knowledge_unit_domains WHERE unit_id = ?",
                (unit.id,),
            )
            self._conn.executemany(
                "INSERT INTO knowledge_unit_domains (unit_id, domain) VALUES (?, ?)",
                [(unit.id, d) for d in domains],
            )

    def query(
        self,
        domains: list[str],
        *,
        languages: list[str] | None = None,
        frameworks: list[str] | None = None,
        pattern: str = "",
        limit: int = 5,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[KnowledgeUnit]:
        """Search for knowledge units by domain tags with relevance ranking.

        Args:
            domains: Domain tags to search for.
            languages: Optional language ranking signal. KUs matching any
                listed language rank higher but non-matching KUs are still returned.
            frameworks: Optional framework ranking signal. KUs matching any
                listed framework rank higher but non-matching KUs are still returned.
            pattern: Optional pattern ranking signal. KUs whose context.pattern
                matches rank higher but non-matching KUs are still returned.
            limit: Maximum number of results to return. Must be positive.
            enterprise_id: When provided, restrict results to KUs in this Enterprise.
                Cross-Enterprise discovery flows through /aigrp/forward-query
                (consent + audit), not the local /query.
            group_id: When ``enterprise_id`` is also provided, restrict results
                to KUs in this Group OR KUs flagged ``cross_group_allowed=1``.
                Cross-Group access without the flag flows through forward-query.

        Returns:
            Knowledge units ranked by relevance * confidence, descending.

        Raises:
            ValueError: If limit is not positive.
        """
        self._check_open()
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not domains:
            return []

        normalized = normalize_domains(domains)
        if not normalized:
            return []
        # Safe: placeholders is only '?' characters, never user input.
        placeholders = ",".join("?" for _ in normalized)
        where_clauses = [
            "ku.status = 'approved'",
            f"ku.id IN ("
            f"SELECT DISTINCT unit_id FROM knowledge_unit_domains "
            f"WHERE domain IN ({placeholders}))",
        ]
        params: list[Any] = list(normalized)
        if enterprise_id is not None:
            where_clauses.append("ku.enterprise_id = ?")
            params.append(enterprise_id)
            if group_id is not None:
                where_clauses.append("(ku.group_id = ? OR ku.cross_group_allowed = 1)")
                params.append(group_id)
        sql = f"SELECT ku.data FROM knowledge_units ku WHERE {' AND '.join(where_clauses)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # PoC: all filtering and scoring is in-memory after deserialization.
        # For larger stores, push coarse filters into SQL.
        units = [KnowledgeUnit.model_validate_json(row[0]) for row in rows]

        scored = []
        for unit in units:
            relevance = calculate_relevance(
                unit,
                normalized,
                query_languages=languages,
                query_frameworks=frameworks,
                query_pattern=pattern,
            )
            scored.append((relevance * unit.evidence.confidence, unit))

        scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        return [unit for _, unit in scored[:limit]]

    def count(self) -> int:
        """Return the total number of knowledge units in the store."""
        self._check_open()
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()
        return row[0]

    def count_in_enterprise(self, enterprise_id: str) -> int:
        """Return total KU count scoped to one Enterprise.

        Used by /api/v1/stats (SEC-HIGH #39) so global cardinality
        stops leaking across tenants.
        """
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM knowledge_units WHERE enterprise_id = ?",
                (enterprise_id,),
            ).fetchone()
        return row[0]

    def domain_counts(self, *, enterprise_id: str | None = None) -> dict[str, int]:
        """Return the count of approved knowledge units per domain tag.

        When ``enterprise_id`` is provided, restrict to that Enterprise.
        """
        self._check_open()
        sql = (
            "SELECT d.domain, COUNT(*) "
            "FROM knowledge_unit_domains d "
            "JOIN knowledge_units ku ON ku.id = d.unit_id "
            "WHERE ku.status = 'approved' "
        )
        params: list[Any] = []
        if enterprise_id is not None:
            sql += "AND ku.enterprise_id = ? "
            params.append(enterprise_id)
        sql += "GROUP BY d.domain ORDER BY COUNT(*) DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {row[0]: row[1] for row in rows}

    def pending_queue(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        enterprise_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return pending KUs with review metadata, oldest first.

        When ``enterprise_id`` is provided, restrict to that Enterprise.
        """
        self._check_open()
        sql = (
            "SELECT data, status, reviewed_by, reviewed_at "
            "FROM knowledge_units WHERE status = 'pending' "
        )
        params: list[Any] = []
        if enterprise_id is not None:
            sql += "AND enterprise_id = ? "
            params.append(enterprise_id)
        sql += "ORDER BY created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "knowledge_unit": KnowledgeUnit.model_validate_json(row[0]),
                "status": row[1],
                "reviewed_by": row[2],
                "reviewed_at": row[3],
            }
            for row in rows
        ]

    def pending_count(self, *, enterprise_id: str | None = None) -> int:
        """Return the number of pending KUs (optionally Enterprise-scoped)."""
        self._check_open()
        sql = "SELECT COUNT(*) FROM knowledge_units WHERE status = 'pending'"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " AND enterprise_id = ?"
            params.append(enterprise_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0]

    def counts_by_status(self, *, enterprise_id: str | None = None) -> dict[str, int]:
        """Return KU counts grouped by review status (optionally Enterprise-scoped)."""
        self._check_open()
        sql = "SELECT status, COUNT(*) FROM knowledge_units"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " WHERE enterprise_id = ?"
            params.append(enterprise_id)
        sql += " GROUP BY status"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {row[0]: row[1] for row in rows}

    def counts_by_tier(self, *, enterprise_id: str | None = None) -> dict[str, int]:
        """Return approved KU counts grouped by tier (optionally Enterprise-scoped)."""
        self._check_open()
        sql = "SELECT tier, COUNT(*) FROM knowledge_units WHERE status = 'approved'"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " AND enterprise_id = ?"
            params.append(enterprise_id)
        sql += " GROUP BY tier"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {row[0]: row[1] for row in rows}

    def list_units(
        self,
        *,
        domain: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        status: str | None = None,
        limit: int = 100,
        enterprise_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return KUs with review metadata, filtered by domain, confidence, or status.

        Confidence filtering is applied in-memory after deserialization
        since confidence lives in the JSON blob. When ``enterprise_id``
        is provided, restrict to that Enterprise.
        """
        self._check_open()
        params: list[Any] = []
        conditions: list[str] = []

        if status:
            conditions.append("ku.status = ?")
            params.append(status)

        if domain:
            normalized = normalize_domains([domain])
            if not normalized:
                return []
            conditions.append("ku.id IN (  SELECT DISTINCT unit_id FROM knowledge_unit_domains WHERE domain = ?)")
            params.append(normalized[0])

        if enterprise_id is not None:
            conditions.append("ku.enterprise_id = ?")
            params.append(enterprise_id)

        has_confidence_filter = confidence_min is not None or confidence_max is not None
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql_limit = "" if has_confidence_filter else f"LIMIT {limit}"
        sql = (
            "SELECT ku.data, ku.status, ku.reviewed_by, ku.reviewed_at "
            f"FROM knowledge_units ku {where} "
            f"ORDER BY ku.created_at DESC {sql_limit}"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            unit = KnowledgeUnit.model_validate_json(row[0])
            c = unit.evidence.confidence
            if confidence_min is not None and c < confidence_min:
                continue
            if confidence_max is not None and (c > confidence_max or (c >= confidence_max and confidence_max < 1.0)):
                continue
            results.append(
                {
                    "knowledge_unit": unit,
                    "status": row[1] or "pending",
                    "reviewed_by": row[2],
                    "reviewed_at": row[3],
                }
            )
            if len(results) >= limit:
                break
        return results

    def create_user(self, username: str, password_hash: str) -> None:
        """Insert a new user.

        Args:
            username: The user's login name.
            password_hash: Bcrypt hash of the user's password.

        Raises:
            sqlite3.IntegrityError: If a user with the same username already exists.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            # Phase 6 step 1: stamp default tenancy scope on every new user.
            self._conn.execute(
                "INSERT INTO users "
                "(username, password_hash, created_at, enterprise_id, group_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    username,
                    password_hash,
                    now,
                    DEFAULT_ENTERPRISE_ID,
                    DEFAULT_GROUP_ID,
                ),
            )

    def get_user(self, username: str) -> dict[str, Any] | None:
        """Retrieve a user by username.

        Args:
            username: The user's login name.

        Returns:
            A dict with id, username, password_hash, created_at, role,
            enterprise_id, and group_id keys, or None if no user with
            that username exists.
        """
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, created_at, role, "
                "enterprise_id, group_id FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
            "role": row[4] or "user",
            "enterprise_id": row[5] or DEFAULT_ENTERPRISE_ID,
            "group_id": row[6] or DEFAULT_GROUP_ID,
        }

    def set_user_role(self, username: str, role: str) -> bool:
        """Set the role on a user. Returns True if a row was updated.

        Used by tests / bootstrap scripts to promote a user to admin.
        """
        self._check_open()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (role, username),
            )
        return cur.rowcount > 0

    def count_active_api_keys_for_user(self, user_id: int) -> int:
        """Return the number of active API keys for the given user.

        Active means not revoked and not yet expired.

        Args:
            user_id: The user's integer id.

        Returns:
            Count of active keys.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (user_id, now),
            ).fetchone()
        return int(row[0])

    def create_api_key(
        self,
        *,
        key_id: str,
        user_id: int,
        name: str,
        labels: list[str],
        key_prefix: str,
        key_hash: str,
        ttl: str,
        expires_at: str,
    ) -> dict[str, Any]:
        """Insert a new API key row.

        Args:
            key_id: Unique identifier (uuid4 hex).
            user_id: Owning user's integer id.
            name: Human-readable name for the key.
            labels: Free-form tags attached to the key for later grouping.
            key_prefix: First 8 characters of the plaintext token.
            key_hash: HMAC-SHA256 hex digest of the plaintext token.
            ttl: Original duration string supplied by the caller (e.g. "90d").
            expires_at: ISO-8601 UTC timestamp at which the key expires.

        Returns:
            A dict representing the inserted row.

        Raises:
            sqlite3.IntegrityError: If the hash collides with an existing key.
        """
        self._check_open()
        created_at = datetime.now(UTC).isoformat()
        labels_json = json.dumps(labels)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO api_keys "
                "(id, user_id, name, labels, key_prefix, key_hash, ttl, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, name, labels_json, key_prefix, key_hash, ttl, expires_at, created_at),
            )
        return {
            "id": key_id,
            "user_id": user_id,
            "name": name,
            "labels": list(labels),
            "key_prefix": key_prefix,
            "key_hash": key_hash,
            "ttl": ttl,
            "expires_at": expires_at,
            "created_at": created_at,
            "last_used_at": None,
            "revoked_at": None,
        }

    def get_api_key_for_user(self, *, user_id: int, key_id: str) -> dict[str, Any] | None:
        """Return a key row if it exists and is owned by the given user.

        Args:
            user_id: The caller's user id.
            key_id: The key's id.

        Returns:
            The row (including revoked keys), or None if not found or not
            owned by this user.
        """
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, name, labels, key_prefix, ttl, expires_at, "
                "created_at, last_used_at, revoked_at "
                "FROM api_keys WHERE id = ? AND user_id = ?",
                (key_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "name": row[2],
            "labels": json.loads(row[3] or "[]"),
            "key_prefix": row[4],
            "ttl": row[5],
            "expires_at": row[6],
            "created_at": row[7],
            "last_used_at": row[8],
            "revoked_at": row[9],
        }

    def get_active_api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        """Retrieve an active API key row by id, including the owner's username.

        "Active" means the same thing as in ``count_active_api_keys_for_user``:
        not revoked and not yet expired. The caller is expected to compare
        the stored ``key_hash`` against a fresh hash of the presented
        secret in constant time.

        Args:
            key_id: The key's id (uuid4 hex).

        Returns:
            A dict with api key fields (including ``key_hash``) plus the
            owner's username, or None if the key does not exist, has
            been revoked, or has expired.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT k.id, k.user_id, u.username, k.name, k.labels, k.key_prefix, "
                "k.key_hash, k.ttl, k.expires_at, k.created_at, k.last_used_at, k.revoked_at "
                "FROM api_keys k JOIN users u ON u.id = k.user_id "
                "WHERE k.id = ? AND k.revoked_at IS NULL AND k.expires_at > ?",
                (key_id, now),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "username": row[2],
            "name": row[3],
            "labels": json.loads(row[4] or "[]"),
            "key_prefix": row[5],
            "key_hash": row[6],
            "ttl": row[7],
            "expires_at": row[8],
            "created_at": row[9],
            "last_used_at": row[10],
            "revoked_at": row[11],
        }

    def list_api_keys_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return all API keys owned by the given user, newest first.

        Args:
            user_id: The user's integer id.

        Returns:
            A list of dicts; empty if the user has no keys.
        """
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, labels, key_prefix, ttl, expires_at, created_at, "
                "last_used_at, revoked_at "
                "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "labels": json.loads(row[2] or "[]"),
                "key_prefix": row[3],
                "ttl": row[4],
                "expires_at": row[5],
                "created_at": row[6],
                "last_used_at": row[7],
                "revoked_at": row[8],
            }
            for row in rows
        ]

    def revoke_api_key(self, *, user_id: int, key_id: str) -> bool:
        """Mark the given key as revoked if it belongs to the user and is not already revoked.

        Args:
            user_id: The caller's user id; the key must belong to this user.
            key_id: The key's id.

        Returns:
            True if a row was updated, False if the key does not exist,
            belongs to a different user, or was already revoked.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (now, key_id, user_id),
            )
        return cursor.rowcount > 0

    def touch_api_key_last_used(self, key_id: str) -> None:
        """Update ``last_used_at`` for the given key, swallowing errors.

        This is a best-effort observability signal; failures must not break
        the request that triggered the update.

        Args:
            key_id: The key's id.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (now, key_id),
                )
        except sqlite3.Error:
            _logger.exception("Failed to update last_used_at for api key %s", key_id)

    # -- Directory peerings (sprint 3) -----------------------------------

    def upsert_directory_peering(
        self,
        *,
        offer_id: str,
        from_enterprise: str,
        to_enterprise: str,
        status: str,
        content_policy: str,
        consult_logging_policy: str,
        topic_filters_json: str,
        active_from: str | None,
        expires_at: str,
        offer_payload_canonical: str,
        offer_signature_b64u: str,
        offer_signing_key_id: str,
        accept_payload_canonical: str,
        accept_signature_b64u: str,
        accept_signing_key_id: str,
        last_synced_at: str,
    ) -> None:
        """Mirror one verified peering record from the directory pull loop.

        Both signatures (offer + accept) are persisted alongside the
        canonical payloads so any later code can re-verify offline
        without going back to the directory.
        """
        self._check_open()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO aigrp_directory_peerings (
                    offer_id, from_enterprise, to_enterprise, status,
                    content_policy, consult_logging_policy, topic_filters_json,
                    active_from, expires_at,
                    offer_payload_canonical, offer_signature_b64u, offer_signing_key_id,
                    accept_payload_canonical, accept_signature_b64u, accept_signing_key_id,
                    last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id) DO UPDATE SET
                    status = excluded.status,
                    content_policy = excluded.content_policy,
                    consult_logging_policy = excluded.consult_logging_policy,
                    topic_filters_json = excluded.topic_filters_json,
                    active_from = excluded.active_from,
                    expires_at = excluded.expires_at,
                    offer_payload_canonical = excluded.offer_payload_canonical,
                    offer_signature_b64u = excluded.offer_signature_b64u,
                    offer_signing_key_id = excluded.offer_signing_key_id,
                    accept_payload_canonical = excluded.accept_payload_canonical,
                    accept_signature_b64u = excluded.accept_signature_b64u,
                    accept_signing_key_id = excluded.accept_signing_key_id,
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    offer_id, from_enterprise, to_enterprise, status,
                    content_policy, consult_logging_policy, topic_filters_json,
                    active_from, expires_at,
                    offer_payload_canonical, offer_signature_b64u, offer_signing_key_id,
                    accept_payload_canonical, accept_signature_b64u, accept_signing_key_id,
                    last_synced_at,
                ),
            )

    def list_directory_peerings(
        self,
        *,
        enterprise_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return mirrored directory peering rows.

        ``enterprise_id`` matches either side of the peering (from or to);
        callers that need one-sided filtering can post-filter.
        """
        self._check_open()
        sql = "SELECT * FROM aigrp_directory_peerings"
        clauses: list[str] = []
        params: list[Any] = []
        if enterprise_id is not None:
            clauses.append("(from_enterprise = ? OR to_enterprise = ?)")
            params.extend([enterprise_id, enterprise_id])
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY active_from DESC NULLS LAST, last_synced_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            cols = [c[0] for c in self._conn.execute(
                "SELECT * FROM aigrp_directory_peerings LIMIT 0"
            ).description]
        return [dict(zip(cols, row, strict=True)) for row in rows]

    # -- AIGRP peer mesh -------------------------------------------------

    def upsert_aigrp_peer(
        self,
        *,
        l2_id: str,
        enterprise: str,
        group: str,
        endpoint_url: str,
        embedding_centroid: bytes | None,
        domain_bloom: bytes | None,
        ku_count: int,
        domain_count: int,
        embedding_model: str | None,
        signature_received: bool,
    ) -> None:
        """Insert or update an AIGRP peer record.

        signature_received=True means the caller is supplying a fresh
        signature; we update last_signature_at. signature_received=False
        means the caller is just announcing the peer's existence (e.g.
        from /aigrp/hello before the peer's signature is fetched); we
        leave the signature columns alone if the row already exists.
        """
        self._check_open()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT 1 FROM aigrp_peers WHERE l2_id = ?", (l2_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO aigrp_peers (
                        l2_id, enterprise, "group", endpoint_url,
                        embedding_centroid, domain_bloom, ku_count, domain_count,
                        embedding_model, first_seen_at, last_seen_at, last_signature_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        l2_id, enterprise, group, endpoint_url,
                        embedding_centroid, domain_bloom, ku_count, domain_count,
                        embedding_model, now, now, now if signature_received else None,
                    ),
                )
            elif signature_received:
                self._conn.execute(
                    """
                    UPDATE aigrp_peers SET
                        enterprise = ?, "group" = ?, endpoint_url = ?,
                        embedding_centroid = ?, domain_bloom = ?,
                        ku_count = ?, domain_count = ?, embedding_model = ?,
                        last_seen_at = ?, last_signature_at = ?
                    WHERE l2_id = ?
                    """,
                    (
                        enterprise, group, endpoint_url,
                        embedding_centroid, domain_bloom,
                        ku_count, domain_count, embedding_model,
                        now, now, l2_id,
                    ),
                )
            else:
                # touch last_seen but keep cached signature
                self._conn.execute(
                    """
                    UPDATE aigrp_peers SET
                        enterprise = ?, "group" = ?, endpoint_url = ?,
                        last_seen_at = ?
                    WHERE l2_id = ?
                    """,
                    (enterprise, group, endpoint_url, now, l2_id),
                )

    def list_aigrp_peers(self, enterprise: str) -> list[dict[str, Any]]:
        """Return every known peer in the given Enterprise.

        No TTL filter — peers age out via the periodic poll task
        overwriting ``last_seen``.
        """
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT l2_id, enterprise, "group", endpoint_url,
                       embedding_centroid, domain_bloom,
                       ku_count, domain_count, embedding_model,
                       first_seen_at, last_seen_at, last_signature_at
                FROM aigrp_peers
                WHERE enterprise = ?
                ORDER BY last_seen_at DESC
                """,
                (enterprise,),
            ).fetchall()
        return [
            {
                "l2_id": r[0],
                "enterprise": r[1],
                "group": r[2],
                "endpoint_url": r[3],
                "embedding_centroid": r[4],
                "domain_bloom": r[5],
                "ku_count": r[6],
                "domain_count": r[7],
                "embedding_model": r[8],
                "first_seen_at": r[9],
                "last_seen_at": r[10],
                "last_signature_at": r[11],
            }
            for r in rows
        ]

    def approved_embeddings_iter(self) -> list[bytes]:
        """Return all non-null approved KU embedding blobs.

        Used to compute this L2's signature centroid.
        """
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT embedding FROM knowledge_units "
                "WHERE status = 'approved' AND embedding IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def approved_domains(self) -> set[str]:
        """Return distinct domains across approved KUs — for the Bloom filter."""
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT d.domain "
                "FROM knowledge_unit_domains d "
                "JOIN knowledge_units ku ON ku.id = d.unit_id "
                "WHERE ku.status = 'approved'"
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    # -- Phase 6 step 2: cross-L2 forward-query support ------------------

    def semantic_query_with_scope(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        status: str = "approved",
    ) -> list[dict[str, Any]]:
        """Cosine-rank approved KUs, returning scope + xgroup flag per row.

        Used by /aigrp/forward-query — the policy-evaluation step needs
        ``enterprise_id`` / ``group_id`` / ``cross_group_allowed`` per
        candidate KU, which the plain ``semantic_query`` path doesn't
        return. Kept separate to avoid widening the returned tuple shape
        of the existing call sites.

        Returns one dict per hit, sorted by similarity desc, limited.
        """
        import numpy as np

        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, embedding, enterprise_id, group_id, "
                "cross_group_allowed FROM knowledge_units "
                "WHERE status = ? AND embedding IS NOT NULL",
                (status,),
            ).fetchall()
        if not rows:
            return []

        query = np.array(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            return []
        query = query / q_norm

        scored: list[tuple[float, KnowledgeUnit, str, str, int]] = []
        for data_str, blob, ent, grp, xgroup in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.size == 0:
                continue
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            sim = float(np.dot(query, vec / v_norm))
            unit = KnowledgeUnit.model_validate_json(data_str)
            scored.append((sim, unit, ent or DEFAULT_ENTERPRISE_ID, grp or DEFAULT_GROUP_ID, int(xgroup or 0)))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "unit": unit,
                "similarity": sim,
                "enterprise_id": ent,
                "group_id": grp,
                "cross_group_allowed": bool(xgroup),
            }
            for sim, unit, ent, grp, xgroup in scored[:limit]
        ]

    def find_cross_enterprise_consent(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        """Look up an active consent record for a (req-ent, resp-ent) pair.

        Group-level columns are nullable in the schema — null means "any
        group on that side". The lookup picks the most-specific match
        first (both groups specified) and falls back through the wildcard
        rows. ``now_iso`` lets the caller pin "active" to a specific
        clock; expired rows are skipped.
        """
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT consent_id, requester_group, responder_group,
                       policy, signed_by_admin, signed_at, expires_at,
                       audit_log_id
                FROM cross_enterprise_consents
                WHERE requester_enterprise = ?
                  AND responder_enterprise = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (requester_enterprise, responder_enterprise, now_iso),
            ).fetchall()
        if not rows:
            return None

        # Score: exact group match worth more than null wildcard.
        def _score(req_g: str | None, resp_g: str | None) -> int:
            return (
                (2 if req_g == requester_group and req_g is not None else 0)
                + (2 if resp_g == responder_group and resp_g is not None else 0)
                + (1 if req_g is None else 0)
                + (1 if resp_g is None else 0)
            )

        best = None
        best_score = -1
        for r in rows:
            req_g, resp_g = r[1], r[2]
            # Only accept rows where the group constraints are satisfied
            # — exact match OR null (wildcard).
            if req_g is not None and req_g != requester_group:
                continue
            if resp_g is not None and resp_g != responder_group:
                continue
            score = _score(req_g, resp_g)
            if score > best_score:
                best_score = score
                best = r
        if best is None:
            return None
        return {
            "consent_id": best[0],
            "requester_group": best[1],
            "responder_group": best[2],
            "policy": best[3],
            "signed_by_admin": best[4],
            "signed_at": best[5],
            "expires_at": best[6],
            "audit_log_id": best[7],
        }

    def insert_cross_enterprise_consent(
        self,
        *,
        consent_id: str,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        policy: str,
        signed_by_admin: str,
        signed_at: str,
        expires_at: str | None,
        audit_log_id: str,
    ) -> None:
        """Insert a consent record.

        Used by tests today; the admin sign-consent endpoint (Lane D)
        will call this in a follow-up PR.
        """
        self._check_open()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cross_enterprise_consents (
                    consent_id, requester_enterprise, responder_enterprise,
                    requester_group, responder_group, policy,
                    signed_by_admin, signed_at, expires_at, audit_log_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consent_id, requester_enterprise, responder_enterprise,
                    requester_group, responder_group, policy,
                    signed_by_admin, signed_at, expires_at, audit_log_id,
                ),
            )

    def record_cross_l2_audit(
        self,
        *,
        audit_id: str,
        ts: str,
        requester_l2_id: str | None,
        requester_enterprise: str | None,
        requester_group: str | None,
        requester_persona: str | None,
        responder_l2_id: str | None,
        responder_enterprise: str | None,
        responder_group: str | None,
        policy_applied: str,
        result_count: int,
        consent_id: str | None,
    ) -> None:
        """Append a row to ``cross_l2_audit``.

        Every /aigrp/forward-query call writes one row regardless of
        outcome — even denied requests are logged so an Enterprise admin
        can see who tried to ask what.
        """
        self._check_open()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cross_l2_audit (
                    audit_id, ts, requester_l2_id, requester_enterprise,
                    requester_group, requester_persona,
                    responder_l2_id, responder_enterprise, responder_group,
                    policy_applied, result_count, consent_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id, ts, requester_l2_id, requester_enterprise,
                    requester_group, requester_persona,
                    responder_l2_id, responder_enterprise, responder_group,
                    policy_applied, result_count, consent_id,
                ),
            )

    def set_ku_cross_group_allowed(self, unit_id: str, allowed: bool) -> bool:
        """Flip the per-KU cross-group sharing flag.

        Returns True if a row was updated.
        """
        self._check_open()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_units SET cross_group_allowed = ? WHERE id = ?",
                (1 if allowed else 0, unit_id),
            )
        return cur.rowcount > 0

    def confidence_distribution(self, *, enterprise_id: str | None = None) -> dict[str, int]:
        """Return confidence distribution buckets for approved KUs.

        When ``enterprise_id`` is provided, restrict to that Enterprise.
        """
        self._check_open()
        sql = "SELECT data FROM knowledge_units WHERE status = 'approved'"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " AND enterprise_id = ?"
            params.append(enterprise_id)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        buckets = {"0.0-0.3": 0, "0.3-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for (data,) in rows:
            unit = KnowledgeUnit.model_validate_json(data)
            c = unit.evidence.confidence
            if c < 0.3:
                buckets["0.0-0.3"] += 1
            elif c < 0.6:
                buckets["0.3-0.6"] += 1
            elif c < 0.8:
                buckets["0.6-0.8"] += 1
            else:
                buckets["0.8-1.0"] += 1
        return buckets

    def recent_activity(
        self,
        limit: int = 20,
        *,
        enterprise_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent activity as one event per knowledge unit.

        Each KU appears once: reviewed KUs show as approved/rejected,
        pending KUs show as proposed. Ordered by the most recent
        timestamp (reviewed_at for reviewed KUs, created_at otherwise).
        When ``enterprise_id`` is provided, restrict to that Enterprise.
        """
        self._check_open()
        sql = "SELECT id, data, status, reviewed_by, reviewed_at FROM knowledge_units"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " WHERE enterprise_id = ?"
            params.append(enterprise_id)
        sql += " ORDER BY COALESCE(reviewed_at, created_at) DESC LIMIT ?"
        params.append(limit * 2)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        activity = []
        for row in rows:
            unit = KnowledgeUnit.model_validate_json(row[1])
            proposed_ts = unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else ""
            # Show only the terminal state per KU: the review event if
            # reviewed, otherwise the proposed event.
            if row[2] in ("approved", "rejected"):
                activity.append(
                    {
                        "type": row[2],
                        "unit_id": row[0],
                        "summary": unit.insight.summary,
                        "reviewed_by": row[3],
                        "timestamp": row[4] or proposed_ts,
                    }
                )
            else:
                activity.append(
                    {
                        "type": "proposed",
                        "unit_id": row[0],
                        "summary": unit.insight.summary,
                        "timestamp": proposed_ts,
                    }
                )
        activity.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return activity[:limit]

    def daily_counts(
        self,
        *,
        days: int = 30,
        enterprise_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return daily proposal and approval counts with contiguous dates.

        When ``enterprise_id`` is provided, restrict to that Enterprise.

        Raises:
            ValueError: If days is not positive.
        """
        if days <= 0:
            raise ValueError("days must be positive")
        self._check_open()
        cutoff = f"-{days} days"
        ent_clause = " AND enterprise_id = ?" if enterprise_id is not None else ""
        ent_params: tuple[Any, ...] = (enterprise_id,) if enterprise_id is not None else ()
        with self._lock:
            proposed_rows = self._conn.execute(
                "SELECT date(created_at) as day, COUNT(*) as cnt "
                "FROM knowledge_units "
                "WHERE created_at >= date('now', ?)" + ent_clause + " "
                "GROUP BY day",
                (cutoff, *ent_params),
            ).fetchall()
            approved_rows = self._conn.execute(
                "SELECT date(reviewed_at) as day, COUNT(*) as cnt "
                "FROM knowledge_units "
                "WHERE status = 'approved' "
                "AND reviewed_at >= date('now', ?)" + ent_clause + " "
                "GROUP BY day",
                (cutoff, *ent_params),
            ).fetchall()
            rejected_rows = self._conn.execute(
                "SELECT date(reviewed_at) as day, COUNT(*) as cnt "
                "FROM knowledge_units "
                "WHERE status = 'rejected' "
                "AND reviewed_at >= date('now', ?)" + ent_clause + " "
                "GROUP BY day",
                (cutoff, *ent_params),
            ).fetchall()
        proposed = {row[0]: row[1] for row in proposed_rows}
        approved = {row[0]: row[1] for row in approved_rows}
        rejected = {row[0]: row[1] for row in rejected_rows}
        all_dates = set(proposed) | set(approved) | set(rejected)
        if not all_dates:
            return []
        start = min(datetime.strptime(d, "%Y-%m-%d").date() for d in all_dates)
        end = datetime.now(UTC).date()
        result: list[dict[str, Any]] = []
        current = start
        while current <= end:
            key = current.isoformat()
            result.append(
                {
                    "date": key,
                    "proposed": proposed.get(key, 0),
                    "approved": approved.get(key, 0),
                    "rejected": rejected.get(key, 0),
                }
            )
            current += timedelta(days=1)
        return result

    # -- Phase 6 step 3: presence registry ------------------------------

    def upsert_peer(
        self,
        *,
        persona: str,
        user_id: int | None,
        enterprise_id: str,
        group_id: str,
        last_seen_at: str,
        expertise_domains: list[str] | None,
        discoverable: bool,
        working_dir_hint: str | None,
        metadata_json: str | None = None,
    ) -> None:
        """UPSERT a presence row keyed by ``persona``.

        Updates ``last_seen_at`` to the supplied timestamp and replaces
        the other mutable fields on conflict; the ``expertise_vector``
        column is left untouched here (recomputed by a future
        embedding-cron pass — the heartbeat path stays cheap).
        """
        self._check_open()
        domains_json = json.dumps(expertise_domains) if expertise_domains is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO peers (
                    persona, user_id, enterprise_id, group_id, last_seen_at,
                    expertise_domains, discoverable, working_dir_hint,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona) DO UPDATE SET
                    user_id = excluded.user_id,
                    enterprise_id = excluded.enterprise_id,
                    group_id = excluded.group_id,
                    last_seen_at = excluded.last_seen_at,
                    expertise_domains = excluded.expertise_domains,
                    discoverable = excluded.discoverable,
                    working_dir_hint = excluded.working_dir_hint,
                    metadata_json = excluded.metadata_json
                """,
                (
                    persona, user_id, enterprise_id, group_id, last_seen_at,
                    domains_json, 1 if discoverable else 0, working_dir_hint,
                    metadata_json,
                ),
            )

    def list_active_peers(
        self,
        *,
        enterprise_id: str,
        since_iso: str,
        group_id: str | None = None,
        exclude_persona: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return discoverable peers seen at or after ``since_iso``.

        Scoped to a single Enterprise — presence is intentionally
        Enterprise-bounded (consent unlocks knowledge access, not
        presence visibility). ``group_id`` narrows further within the
        Enterprise; ``exclude_persona`` filters out the caller.
        """
        self._check_open()
        sql = (
            "SELECT persona, user_id, enterprise_id, group_id, last_seen_at, "
            "expertise_domains, discoverable, working_dir_hint, metadata_json "
            "FROM peers "
            "WHERE enterprise_id = ? AND last_seen_at >= ? AND discoverable = 1"
        )
        params: list[Any] = [enterprise_id, since_iso]
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        if exclude_persona is not None:
            sql += " AND persona != ?"
            params.append(exclude_persona)
        sql += " ORDER BY last_seen_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "persona": r[0],
                "user_id": r[1],
                "enterprise_id": r[2],
                "group_id": r[3],
                "last_seen_at": r[4],
                "expertise_domains": json.loads(r[5]) if r[5] else None,
                "discoverable": bool(r[6]),
                "working_dir_hint": r[7],
                "metadata_json": r[8],
            }
            for r in rows
        ]

    # -- Phase 6 step 3: consent admin -----------------------------------

    def list_cross_enterprise_consents(
        self,
        *,
        include_expired: bool = False,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return cross-Enterprise consent rows, newest first.

        ``include_expired=False`` filters out rows whose ``expires_at``
        is in the past (NULL ``expires_at`` rows are kept — they never
        expire). ``include_expired=True`` returns everything.
        """
        self._check_open()
        sql = (
            "SELECT consent_id, requester_enterprise, responder_enterprise, "
            "requester_group, responder_group, policy, signed_by_admin, "
            "signed_at, expires_at, audit_log_id "
            "FROM cross_enterprise_consents"
        )
        params: list[Any] = []
        if not include_expired:
            sql += " WHERE expires_at IS NULL OR expires_at > ?"
            params.append(now_iso)
        sql += " ORDER BY signed_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "consent_id": r[0],
                "requester_enterprise": r[1],
                "responder_enterprise": r[2],
                "requester_group": r[3],
                "responder_group": r[4],
                "policy": r[5],
                "signed_by_admin": r[6],
                "signed_at": r[7],
                "expires_at": r[8],
                "audit_log_id": r[9],
            }
            for r in rows
        ]

    def get_cross_enterprise_consent(self, consent_id: str) -> dict[str, Any] | None:
        """Return a single consent record by id, or None if absent."""
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT consent_id, requester_enterprise, responder_enterprise, "
                "requester_group, responder_group, policy, signed_by_admin, "
                "signed_at, expires_at, audit_log_id "
                "FROM cross_enterprise_consents WHERE consent_id = ?",
                (consent_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "consent_id": row[0],
            "requester_enterprise": row[1],
            "responder_enterprise": row[2],
            "requester_group": row[3],
            "responder_group": row[4],
            "policy": row[5],
            "signed_by_admin": row[6],
            "signed_at": row[7],
            "expires_at": row[8],
            "audit_log_id": row[9],
        }

    def find_active_consent_for_pair(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        """Return any non-expired consent matching the exact tuple.

        Distinct from ``find_cross_enterprise_consent`` which scores
        wildcard matches. This helper is for the duplicate-detection
        guard on the admin sign endpoint — only an *exact* match (same
        groups, both NULL or both equal) counts as a duplicate.
        """
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT consent_id FROM cross_enterprise_consents
                WHERE requester_enterprise = ?
                  AND responder_enterprise = ?
                  AND ((requester_group IS NULL AND ? IS NULL)
                       OR requester_group = ?)
                  AND ((responder_group IS NULL AND ? IS NULL)
                       OR responder_group = ?)
                  AND (expires_at IS NULL OR expires_at > ?)
                LIMIT 1
                """,
                (
                    requester_enterprise, responder_enterprise,
                    requester_group, requester_group,
                    responder_group, responder_group,
                    now_iso,
                ),
            ).fetchone()
        if row is None:
            return None
        return self.get_cross_enterprise_consent(row[0])

    def revoke_cross_enterprise_consent(
        self, *, consent_id: str, revoked_at: str
    ) -> bool:
        """Soft-revoke a consent by setting ``expires_at = revoked_at``.

        Returns True if a row was updated. Does not hard-delete; the
        record remains for the audit trail. Idempotent: revoking an
        already-revoked consent updates the timestamp again (cheap and
        consistent with the "expiry advances" semantics elsewhere).
        """
        self._check_open()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE cross_enterprise_consents SET expires_at = ? WHERE consent_id = ?",
                (revoked_at, consent_id),
            )
        return cur.rowcount > 0

    # ---------------------------------------------------------------
    # L3 consults (issue #20). Sprint 2 — same-L2 path. Cross-L2
    # routing comes in a follow-up PR.
    # ---------------------------------------------------------------

    def create_consult(
        self,
        *,
        thread_id: str,
        from_l2_id: str,
        from_persona: str,
        to_l2_id: str,
        to_persona: str,
        subject: str | None,
        created_at: str,
    ) -> None:
        """Open a new consult thread. Status starts at 'open'."""
        self._check_open()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO consults (
                    thread_id, from_l2_id, from_persona,
                    to_l2_id, to_persona, subject,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    thread_id, from_l2_id, from_persona,
                    to_l2_id, to_persona, subject,
                    created_at,
                ),
            )

    def get_consult(self, thread_id: str) -> dict[str, Any] | None:
        self._check_open()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT thread_id, from_l2_id, from_persona,
                       to_l2_id, to_persona, subject,
                       status, claimed_by, created_at,
                       closed_at, resolution_summary
                FROM consults WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "thread_id": row[0],
            "from_l2_id": row[1],
            "from_persona": row[2],
            "to_l2_id": row[3],
            "to_persona": row[4],
            "subject": row[5],
            "status": row[6],
            "claimed_by": row[7],
            "created_at": row[8],
            "closed_at": row[9],
            "resolution_summary": row[10],
        }

    def append_consult_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        from_l2_id: str,
        from_persona: str,
        content: str,
        created_at: str,
    ) -> None:
        """Append a message to an existing thread.

        Caller is responsible for verifying the thread exists and is
        not closed (raise 404 / 409 at the API layer).
        """
        self._check_open()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO consult_messages (
                    message_id, thread_id, from_l2_id,
                    from_persona, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, thread_id, from_l2_id,
                    from_persona, content, created_at,
                ),
            )

    def list_consult_messages(self, thread_id: str) -> list[dict[str, Any]]:
        self._check_open()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT message_id, thread_id, from_l2_id,
                       from_persona, content, created_at
                FROM consult_messages
                WHERE thread_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id,),
            ).fetchall()
        return [
            {
                "message_id": r[0],
                "thread_id": r[1],
                "from_l2_id": r[2],
                "from_persona": r[3],
                "content": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def close_consult(
        self,
        *,
        thread_id: str,
        closed_at: str,
        resolution_summary: str | None,
    ) -> bool:
        """Mark a thread closed. Returns True if it was open before."""
        self._check_open()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE consults
                SET status = 'closed', closed_at = ?, resolution_summary = ?
                WHERE thread_id = ? AND status != 'closed'
                """,
                (closed_at, resolution_summary, thread_id),
            )
        return cur.rowcount > 0

    def list_inbox(
        self,
        *,
        to_l2_id: str,
        to_persona: str,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return threads addressed to (l2_id, persona).

        Default excludes closed threads; pass include_closed=True for an
        audit-style view.
        """
        self._check_open()
        sql = """
            SELECT thread_id, from_l2_id, from_persona,
                   to_l2_id, to_persona, subject,
                   status, claimed_by, created_at,
                   closed_at, resolution_summary
            FROM consults
            WHERE to_l2_id = ? AND to_persona = ?
        """
        params: list[Any] = [to_l2_id, to_persona]
        if not include_closed:
            sql += " AND status != 'closed'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "thread_id": r[0],
                "from_l2_id": r[1],
                "from_persona": r[2],
                "to_l2_id": r[3],
                "to_persona": r[4],
                "subject": r[5],
                "status": r[6],
                "claimed_by": r[7],
                "created_at": r[8],
                "closed_at": r[9],
                "resolution_summary": r[10],
            }
            for r in rows
        ]
