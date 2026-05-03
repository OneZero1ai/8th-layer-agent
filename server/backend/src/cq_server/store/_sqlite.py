"""SqliteStore: SQLite-backed implementation of the async Store protocol.

Async surface implemented as a threadpool shim over a sync SQLAlchemy Core
engine. SQLite-native concerns (PRAGMAs, single-writer behaviour) live here;
portable SQL is sourced from ``cq_server.store._queries``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cq.models import KnowledgeUnit
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..scoring import calculate_relevance
from ..tables import ensure_api_keys_table, ensure_review_columns, ensure_users_table
from ._normalize import normalize_domains
from ._queries import (
    COUNT_ACTIVE_KEYS_FOR_USER,
    DELETE_UNIT_DOMAINS,
    INSERT_API_KEY,
    INSERT_UNIT,
    INSERT_UNIT_DOMAIN,
    SELECT_APPROVED_BY_ID,
    SELECT_APPROVED_DATA,
    SELECT_BY_ID,
    SELECT_COUNTS_BY_STATUS,
    SELECT_COUNTS_BY_TIER,
    SELECT_DOMAIN_COUNTS,
    SELECT_KEY_FOR_USER,
    SELECT_PENDING_COUNT,
    SELECT_PENDING_QUEUE,
    SELECT_QUERY_UNITS,
    SELECT_RECENT_ACTIVITY,
    SELECT_REVIEW_STATUS_BY_ID,
    SELECT_TOTAL_COUNT,
    UPDATE_REVIEW_STATUS,
    UPDATE_UNIT_DATA,
)

_logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("/data/cq.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_units (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
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


def _apply_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001  (sqlalchemy event signature)
    """Issue cq's required SQLite PRAGMAs on every new connection.

    Invoked by SQLAlchemy's ``connect`` event so the pool's per-thread
    connections all receive the same pragmas. ``executescript`` is avoided to
    keep each pragma in its own statement (SQLite docs: some pragmas only
    take effect outside a transaction).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
    finally:
        cursor.close()


class SqliteStore:
    """SQLite-backed Store implementation. See module docstring."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._engine: Engine = create_engine(
            f"sqlite:///{self._db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        event.listen(self._engine, "connect", _apply_sqlite_pragmas)
        with self._engine.begin() as conn:
            for stmt in filter(None, (s.strip() for s in _SCHEMA_SQL.split(";"))):
                conn.exec_driver_sql(stmt)
            raw = conn.connection.driver_connection  # underlying sqlite3.Connection.
            assert raw is not None  # active engine connection always has a DBAPI connection.
            ensure_review_columns(raw)
            ensure_users_table(raw)
            ensure_api_keys_table(raw)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._engine.dispose)

    async def confidence_distribution(self) -> dict[str, int]:
        return await self._run_sync(self._confidence_distribution_sync)

    async def count(self) -> int:
        return await self._run_sync(self._count_sync)

    async def count_active_api_keys_for_user(self, user_id: int) -> int:
        return await self._run_sync(self._count_active_api_keys_for_user_sync, user_id)

    async def counts_by_status(self) -> dict[str, int]:
        return await self._run_sync(self._counts_by_status_sync)

    async def counts_by_tier(self) -> dict[str, int]:
        return await self._run_sync(self._counts_by_tier_sync)

    async def create_api_key(
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
        return await self._run_sync(
            self._create_api_key_sync,
            key_id=key_id,
            user_id=user_id,
            name=name,
            labels=labels,
            key_prefix=key_prefix,
            key_hash=key_hash,
            ttl=ttl,
            expires_at=expires_at,
        )

    async def create_user(self, username: str, password_hash: str) -> None:
        await self._run_sync(self._create_user_sync, username, password_hash)

    async def daily_counts(self, *, days: int = 30) -> list[dict[str, Any]]:
        if days <= 0:
            raise ValueError("days must be positive")
        return await self._run_sync(self._daily_counts_sync, days=days)

    async def domain_counts(self) -> dict[str, int]:
        return await self._run_sync(self._domain_counts_sync)

    async def get(self, unit_id: str) -> KnowledgeUnit | None:
        return await self._run_sync(self._get_sync, unit_id)

    async def get_active_api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_active_api_key_by_id_sync, key_id)

    async def get_any(self, unit_id: str) -> KnowledgeUnit | None:
        return await self._run_sync(self._get_any_sync, unit_id)

    async def get_api_key_for_user(self, *, user_id: int, key_id: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_api_key_for_user_sync, user_id=user_id, key_id=key_id)

    async def get_review_status(self, unit_id: str) -> dict[str, str | None] | None:
        return await self._run_sync(self._get_review_status_sync, unit_id)

    async def get_user(self, username: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_user_sync, username)

    async def insert(self, unit: KnowledgeUnit) -> None:
        await self._run_sync(self._insert_sync, unit)

    async def list_api_keys_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self._run_sync(self._list_api_keys_for_user_sync, user_id)

    async def list_units(
        self,
        *,
        domain: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run_sync(
            self._list_units_sync,
            domain=domain,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
            status=status,
            limit=limit,
        )

    async def pending_count(self) -> int:
        return await self._run_sync(self._pending_count_sync)

    async def pending_queue(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        return await self._run_sync(self._pending_queue_sync, limit=limit, offset=offset)

    async def query(
        self,
        domains: list[str],
        *,
        languages: list[str] | None = None,
        frameworks: list[str] | None = None,
        pattern: str = "",
        limit: int = 5,
    ) -> list[KnowledgeUnit]:
        return await self._run_sync(
            self._query_sync,
            domains,
            languages=languages,
            frameworks=frameworks,
            pattern=pattern,
            limit=limit,
        )

    async def recent_activity(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run_sync(self._recent_activity_sync, limit=limit)

    async def revoke_api_key(self, *, user_id: int, key_id: str) -> bool:
        return await self._run_sync(self._revoke_api_key_sync, user_id=user_id, key_id=key_id)

    async def set_review_status(self, unit_id: str, status: str, reviewed_by: str) -> None:
        await self._run_sync(self._set_review_status_sync, unit_id, status, reviewed_by)

    async def touch_api_key_last_used(self, key_id: str) -> None:
        await self._run_sync(self._touch_api_key_last_used_sync, key_id)

    async def update(self, unit: KnowledgeUnit) -> None:
        await self._run_sync(self._update_sync, unit)

    # ------------------------------------------------------------------
    # Fork-delta: directory peerings (#105 PR-A)
    # Mirrors the synchronous methods on RemoteStore. Backed by the
    # ``aigrp_directory_peerings`` table (Alembic migration 0006).
    # ------------------------------------------------------------------

    async def upsert_directory_peering(
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
        to_l2_endpoints_json: str = "[]",
    ) -> None:
        """Mirror one verified peering record from the directory pull loop."""
        await self._run_sync(
            self._upsert_directory_peering_sync,
            offer_id=offer_id,
            from_enterprise=from_enterprise,
            to_enterprise=to_enterprise,
            status=status,
            content_policy=content_policy,
            consult_logging_policy=consult_logging_policy,
            topic_filters_json=topic_filters_json,
            active_from=active_from,
            expires_at=expires_at,
            offer_payload_canonical=offer_payload_canonical,
            offer_signature_b64u=offer_signature_b64u,
            offer_signing_key_id=offer_signing_key_id,
            accept_payload_canonical=accept_payload_canonical,
            accept_signature_b64u=accept_signature_b64u,
            accept_signing_key_id=accept_signing_key_id,
            last_synced_at=last_synced_at,
            to_l2_endpoints_json=to_l2_endpoints_json,
        )

    async def find_active_directory_peering(
        self,
        *,
        from_enterprise: str,
        to_enterprise: str,
        now_iso: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an active, unexpired peering between two specific enterprises."""
        return await self._run_sync(
            self._find_active_directory_peering_sync,
            from_enterprise=from_enterprise,
            to_enterprise=to_enterprise,
            now_iso=now_iso,
        )

    async def list_directory_peerings(
        self,
        *,
        enterprise_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return mirrored directory peering rows."""
        return await self._run_sync(
            self._list_directory_peerings_sync,
            enterprise_id=enterprise_id,
            status=status,
        )

    # ------------------------------------------------------------------
    # Fork-delta: AIGRP peer mesh (#105 PR-A)
    # Backed by ``aigrp_peers`` (Alembic migration 0005).
    # ------------------------------------------------------------------

    async def upsert_aigrp_peer(
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
        public_key_ed25519: str | None = None,
    ) -> None:
        """Insert or update an AIGRP peer record."""
        await self._run_sync(
            self._upsert_aigrp_peer_sync,
            l2_id=l2_id,
            enterprise=enterprise,
            group=group,
            endpoint_url=endpoint_url,
            embedding_centroid=embedding_centroid,
            domain_bloom=domain_bloom,
            ku_count=ku_count,
            domain_count=domain_count,
            embedding_model=embedding_model,
            signature_received=signature_received,
            public_key_ed25519=public_key_ed25519,
        )

    async def get_aigrp_peer_pubkey(self, l2_id: str) -> str | None:
        """Return the base64url-encoded Ed25519 public key for ``l2_id``."""
        return await self._run_sync(self._get_aigrp_peer_pubkey_sync, l2_id)

    async def list_aigrp_peers(self, enterprise: str) -> list[dict[str, Any]]:
        """Return every known peer in the given Enterprise."""
        return await self._run_sync(self._list_aigrp_peers_sync, enterprise)

    async def approved_embeddings_iter(self) -> list[bytes]:
        """Return all non-null approved KU embedding blobs."""
        return await self._run_sync(self._approved_embeddings_iter_sync)

    async def approved_domains(self) -> set[str]:
        """Return distinct domains across approved KUs (Bloom filter input)."""
        return await self._run_sync(self._approved_domains_sync)

    # ------------------------------------------------------------------
    # Fork-delta: tenancy + xgroup helpers (#105 PR-A increment 2)
    # ------------------------------------------------------------------

    async def count_in_enterprise(self, enterprise_id: str) -> int:
        """Return total KU count scoped to one Enterprise.

        Used by /api/v1/stats so global cardinality stops leaking across
        tenants (security finding #39).
        """
        return await self._run_sync(self._count_in_enterprise_sync, enterprise_id)

    async def set_user_role(self, username: str, role: str) -> bool:
        """Set the role on a user; True if a row was updated."""
        return await self._run_sync(self._set_user_role_sync, username, role)

    async def set_ku_cross_group_allowed(self, unit_id: str, allowed: bool) -> bool:
        """Flip the per-KU cross-group sharing flag; True if updated."""
        return await self._run_sync(self._set_ku_cross_group_allowed_sync, unit_id, allowed)

    # ------------------------------------------------------------------
    # Fork-delta: embedding helpers + KU delete (#105 PR-A inc. 6)
    # ------------------------------------------------------------------

    async def set_embedding(
        self,
        unit_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> bool:
        """Update the embedding for an existing KU (backfill script). True on success."""
        return await self._run_sync(
            self._set_embedding_sync,
            unit_id=unit_id,
            embedding=embedding,
            embedding_model=embedding_model,
        )

    async def iter_unembedded(
        self,
        *,
        status: str = "approved",
        limit: int = 1000,
    ) -> list[tuple[str, str]]:
        """Return (id, data) for KUs with NULL embedding (backfill source)."""
        return await self._run_sync(
            self._iter_unembedded_sync,
            status=status,
            limit=limit,
        )

    async def semantic_query(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        status: str = "approved",
    ) -> list[tuple[KnowledgeUnit, float]]:
        """Brute-force cosine similarity over KUs with embeddings (numpy)."""
        return await self._run_sync(
            self._semantic_query_sync,
            query_vec=query_vec,
            limit=limit,
            status=status,
        )

    async def delete(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
    ) -> bool:
        """Hard-delete a KU; tenant-scoped when enterprise_id provided."""
        return await self._run_sync(
            self._delete_sync,
            unit_id=unit_id,
            enterprise_id=enterprise_id,
        )

    # ------------------------------------------------------------------
    # Fork-delta: peer presence (#105 PR-A inc. 6)
    # ------------------------------------------------------------------

    async def upsert_peer(
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
        """UPSERT a presence row keyed by ``persona``."""
        await self._run_sync(
            self._upsert_peer_sync,
            persona=persona,
            user_id=user_id,
            enterprise_id=enterprise_id,
            group_id=group_id,
            last_seen_at=last_seen_at,
            expertise_domains=expertise_domains,
            discoverable=discoverable,
            working_dir_hint=working_dir_hint,
            metadata_json=metadata_json,
        )

    async def list_active_peers(
        self,
        *,
        enterprise_id: str,
        since_iso: str,
        group_id: str | None = None,
        exclude_persona: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return discoverable peers seen at or after ``since_iso``."""
        return await self._run_sync(
            self._list_active_peers_sync,
            enterprise_id=enterprise_id,
            since_iso=since_iso,
            group_id=group_id,
            exclude_persona=exclude_persona,
        )

    # ------------------------------------------------------------------
    # Fork-delta: L3 consults (#105 PR-A increment 5)
    # Backed by ``consults`` + ``consult_messages`` (Alembic 0004).
    # ------------------------------------------------------------------

    async def create_consult(
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
        await self._run_sync(
            self._create_consult_sync,
            thread_id=thread_id,
            from_l2_id=from_l2_id,
            from_persona=from_persona,
            to_l2_id=to_l2_id,
            to_persona=to_persona,
            subject=subject,
            created_at=created_at,
        )

    async def get_consult(self, thread_id: str) -> dict[str, Any] | None:
        """Return one consult thread by id, or None if absent."""
        return await self._run_sync(self._get_consult_sync, thread_id)

    async def append_consult_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        from_l2_id: str,
        from_persona: str,
        content: str,
        created_at: str,
    ) -> None:
        """Append a message to an existing thread."""
        await self._run_sync(
            self._append_consult_message_sync,
            message_id=message_id,
            thread_id=thread_id,
            from_l2_id=from_l2_id,
            from_persona=from_persona,
            content=content,
            created_at=created_at,
        )

    async def list_consult_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """Return messages for a thread, oldest-first."""
        return await self._run_sync(self._list_consult_messages_sync, thread_id)

    async def close_consult(
        self,
        *,
        thread_id: str,
        closed_at: str,
        resolution_summary: str | None,
    ) -> bool:
        """Mark a thread closed; True if it was open before."""
        return await self._run_sync(
            self._close_consult_sync,
            thread_id=thread_id,
            closed_at=closed_at,
            resolution_summary=resolution_summary,
        )

    async def list_inbox(
        self,
        *,
        to_l2_id: str,
        to_persona: str,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return threads addressed to (l2_id, persona)."""
        return await self._run_sync(
            self._list_inbox_sync,
            to_l2_id=to_l2_id,
            to_persona=to_persona,
            include_closed=include_closed,
            limit=limit,
        )

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Fork-delta: cross-Enterprise consents + audit (#105 PR-A inc. 4)
    # ------------------------------------------------------------------

    async def list_cross_enterprise_consents(
        self,
        *,
        include_expired: bool = False,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return cross-Enterprise consent rows, newest first."""
        return await self._run_sync(
            self._list_cross_enterprise_consents_sync,
            include_expired=include_expired,
            now_iso=now_iso,
            limit=limit,
        )

    async def get_cross_enterprise_consent(self, consent_id: str) -> dict[str, Any] | None:
        """Return one consent record by id, or None if absent."""
        return await self._run_sync(self._get_cross_enterprise_consent_sync, consent_id)

    async def revoke_cross_enterprise_consent(
        self,
        *,
        consent_id: str,
        revoked_at: str,
    ) -> bool:
        """Soft-revoke by setting ``expires_at = revoked_at``; True if updated."""
        return await self._run_sync(
            self._revoke_cross_enterprise_consent_sync,
            consent_id=consent_id,
            revoked_at=revoked_at,
        )

    async def find_cross_enterprise_consent(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        """Score-based lookup of an active consent for a (req-ent, resp-ent) pair."""
        return await self._run_sync(
            self._find_cross_enterprise_consent_sync,
            requester_enterprise=requester_enterprise,
            responder_enterprise=responder_enterprise,
            requester_group=requester_group,
            responder_group=responder_group,
            now_iso=now_iso,
        )

    async def find_active_consent_for_pair(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        """Exact-tuple lookup for the duplicate-detection guard."""
        return await self._run_sync(
            self._find_active_consent_for_pair_sync,
            requester_enterprise=requester_enterprise,
            responder_enterprise=responder_enterprise,
            requester_group=requester_group,
            responder_group=responder_group,
            now_iso=now_iso,
        )

    async def insert_cross_enterprise_consent(
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
        """Insert a consent record."""
        await self._run_sync(
            self._insert_cross_enterprise_consent_sync,
            consent_id=consent_id,
            requester_enterprise=requester_enterprise,
            responder_enterprise=responder_enterprise,
            requester_group=requester_group,
            responder_group=responder_group,
            policy=policy,
            signed_by_admin=signed_by_admin,
            signed_at=signed_at,
            expires_at=expires_at,
            audit_log_id=audit_log_id,
        )

    async def record_cross_l2_audit(
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
        """Append a row to ``cross_l2_audit`` for a forward-query call."""
        await self._run_sync(
            self._record_cross_l2_audit_sync,
            audit_id=audit_id,
            ts=ts,
            requester_l2_id=requester_l2_id,
            requester_enterprise=requester_enterprise,
            requester_group=requester_group,
            requester_persona=requester_persona,
            responder_l2_id=responder_l2_id,
            responder_enterprise=responder_enterprise,
            responder_group=responder_group,
            policy_applied=policy_applied,
            result_count=result_count,
            consent_id=consent_id,
        )

    def _confidence_distribution_sync(self) -> dict[str, int]:
        buckets = {"0.0-0.3": 0, "0.3-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_APPROVED_DATA).fetchall()
        for row in rows:
            c = KnowledgeUnit.model_validate_json(row[0]).evidence.confidence
            if c < 0.3:
                buckets["0.0-0.3"] += 1
            elif c < 0.6:
                buckets["0.3-0.6"] += 1
            elif c < 0.8:
                buckets["0.6-0.8"] += 1
            else:
                buckets["0.8-1.0"] += 1
        return buckets

    def _count_active_api_keys_for_user_sync(self, user_id: int) -> int:
        now = datetime.now(UTC).isoformat()
        with self._engine.connect() as conn:
            row = conn.execute(COUNT_ACTIVE_KEYS_FOR_USER, {"user_id": user_id, "now": now}).fetchone()
        return int(row[0]) if row is not None else 0

    def _count_sync(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(SELECT_TOTAL_COUNT).scalar() or 0)

    def _counts_by_status_sync(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_COUNTS_BY_STATUS).fetchall()
        return {row[0]: row[1] for row in rows}

    def _counts_by_tier_sync(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_COUNTS_BY_TIER).fetchall()
        return {row[0]: row[1] for row in rows}

    def _create_api_key_sync(
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
        created_at = datetime.now(UTC).isoformat()
        labels_json = json.dumps(labels)
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    INSERT_API_KEY,
                    {
                        "id": key_id,
                        "user_id": user_id,
                        "name": name,
                        "labels": labels_json,
                        "key_prefix": key_prefix,
                        "key_hash": key_hash,
                        "ttl": ttl,
                        "expires_at": expires_at,
                        "created_at": created_at,
                    },
                )
        except IntegrityError as e:
            if e.orig is not None:
                raise e.orig from e
            raise
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

    def _create_user_sync(self, username: str, password_hash: str) -> None:
        from ._queries import INSERT_USER

        created_at = datetime.now(UTC).isoformat()
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    INSERT_USER,
                    {"username": username, "password_hash": password_hash, "created_at": created_at},
                )
        except IntegrityError as e:
            if e.orig is not None:
                raise e.orig from e
            raise

    def _daily_counts_sync(self, *, days: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        from ._queries import SELECT_APPROVED_DAILY, SELECT_PROPOSED_DAILY, SELECT_REJECTED_DAILY

        with self._engine.connect() as conn:
            proposed = {row[0]: row[1] for row in conn.execute(SELECT_PROPOSED_DAILY, {"cutoff": cutoff}).fetchall()}
            approved = {row[0]: row[1] for row in conn.execute(SELECT_APPROVED_DAILY, {"cutoff": cutoff}).fetchall()}
            rejected = {row[0]: row[1] for row in conn.execute(SELECT_REJECTED_DAILY, {"cutoff": cutoff}).fetchall()}
        all_dates = set(proposed) | set(approved) | set(rejected)
        if not all_dates:
            return []
        start = min(datetime.strptime(d, "%Y-%m-%d").date() for d in all_dates)
        end = datetime.now(UTC).date()
        rows: list[dict[str, Any]] = []
        current = start
        while current <= end:
            key = current.isoformat()
            rows.append(
                {
                    "date": key,
                    "proposed": proposed.get(key, 0),
                    "approved": approved.get(key, 0),
                    "rejected": rejected.get(key, 0),
                }
            )
            current += timedelta(days=1)
        return rows

    def _domain_counts_sync(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_DOMAIN_COUNTS).fetchall()
        return {row[0]: row[1] for row in rows}

    def _get_active_api_key_by_id_sync(self, key_id: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        # JOIN on users to surface the owner's username. Inline because no
        # _queries.py constant covers this shape; promotion left to a
        # follow-up — out of scope per #308.
        stmt = text(
            "SELECT k.id, k.user_id, u.username, k.name, k.labels, k.key_prefix, "
            "k.key_hash, k.ttl, k.expires_at, k.created_at, k.last_used_at, k.revoked_at "
            "FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.id = :key_id AND k.revoked_at IS NULL AND k.expires_at > :now"
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt, {"key_id": key_id, "now": now}).fetchone()
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

    def _get_any_sync(self, unit_id: str) -> KnowledgeUnit | None:
        with self._engine.connect() as conn:
            row = conn.execute(SELECT_BY_ID, {"id": unit_id}).fetchone()
        return KnowledgeUnit.model_validate_json(row[0]) if row is not None else None

    def _get_api_key_for_user_sync(self, *, user_id: int, key_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(SELECT_KEY_FOR_USER, {"key_id": key_id, "user_id": user_id}).fetchone()
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

    def _get_review_status_sync(self, unit_id: str) -> dict[str, str | None] | None:
        with self._engine.connect() as conn:
            row = conn.execute(SELECT_REVIEW_STATUS_BY_ID, {"id": unit_id}).fetchone()
        if row is None:
            return None
        return {"status": row[0], "reviewed_by": row[1], "reviewed_at": row[2]}

    def _get_sync(self, unit_id: str) -> KnowledgeUnit | None:
        with self._engine.connect() as conn:
            row = conn.execute(SELECT_APPROVED_BY_ID, {"id": unit_id}).fetchone()
        return KnowledgeUnit.model_validate_json(row[0]) if row is not None else None

    def _get_user_sync(self, username: str) -> dict[str, Any] | None:
        from ._queries import SELECT_USER_BY_USERNAME

        with self._engine.connect() as conn:
            row = conn.execute(SELECT_USER_BY_USERNAME, {"username": username}).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
        }

    def _insert_sync(self, unit: KnowledgeUnit) -> None:
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        # Persist the normalized form in both the JSON blob and the
        # knowledge_unit_domains rows so calculate_relevance reads the
        # same domains from either source.
        unit = unit.model_copy(update={"domains": domains})
        created_at = (
            unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else datetime.now(UTC).isoformat()
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    INSERT_UNIT,
                    {
                        "id": unit.id,
                        "data": unit.model_dump_json(),
                        "created_at": created_at,
                        "tier": unit.tier.value,
                    },
                )
                for d in domains:
                    conn.execute(INSERT_UNIT_DOMAIN, {"unit_id": unit.id, "domain": d})
        except IntegrityError as e:
            if e.orig is not None:
                raise e.orig from e
            raise

    def _list_api_keys_for_user_sync(self, user_id: int) -> list[dict[str, Any]]:
        # Inline SQL: no _queries.py constant covers this list shape.
        stmt = text(
            "SELECT id, name, labels, key_prefix, ttl, expires_at, created_at, "
            "last_used_at, revoked_at "
            "FROM api_keys WHERE user_id = :user_id ORDER BY created_at DESC"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt, {"user_id": user_id}).fetchall()
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

    def _list_units_sync(
        self,
        *,
        domain: str | None,
        confidence_min: float | None,
        confidence_max: float | None,
        status: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        from ._queries import select_list_units

        normalized_domain: str | None = None
        if domain is not None and domain.strip():
            normalized_domain = domain.strip().lower()

        normalized_status: str | None = status if (status is not None and status.strip()) else None

        confidence_filter_active = confidence_min is not None or confidence_max is not None
        stmt = select_list_units(
            domain=normalized_domain,
            status=normalized_status,
            apply_limit=not confidence_filter_active,
        )
        params: dict[str, Any] = {}
        if normalized_domain is not None:
            params["domain"] = normalized_domain
        if normalized_status is not None:
            params["status"] = normalized_status
        if not confidence_filter_active:
            params["limit"] = limit

        with self._engine.connect() as conn:
            rows = conn.execute(stmt, params).fetchall()

        results: list[dict[str, Any]] = []
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

    def _pending_count_sync(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(SELECT_PENDING_COUNT).scalar() or 0)

    def _pending_queue_sync(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_PENDING_QUEUE, {"limit": limit, "offset": offset}).fetchall()
        return [
            {
                "knowledge_unit": KnowledgeUnit.model_validate_json(row[0]),
                "status": row[1] or "pending",
                "reviewed_by": row[2],
                "reviewed_at": row[3],
            }
            for row in rows
        ]

    def _query_sync(
        self,
        domains: list[str],
        *,
        languages: list[str] | None,
        frameworks: list[str] | None,
        pattern: str,
        limit: int,
    ) -> list[KnowledgeUnit]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        normalized = normalize_domains(domains)
        if not normalized:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_QUERY_UNITS, {"domains": normalized}).fetchall()
        units = [KnowledgeUnit.model_validate_json(row[0]) for row in rows]
        scored = [
            (
                calculate_relevance(
                    u,
                    normalized,
                    query_languages=languages,
                    query_frameworks=frameworks,
                    query_pattern=pattern,
                )
                * u.evidence.confidence,
                u.id,
                u,
            )
            for u in units
        ]
        # Match RemoteStore tie-break: score desc, id desc on tie.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [u for _, _, u in scored[:limit]]

    def _recent_activity_sync(self, *, limit: int) -> list[dict[str, Any]]:
        # Over-fetch by 2x to give buffer; the SELECT already ORDER BYs
        # COALESCE(reviewed_at, created_at) DESC. Final slice trims to limit.
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_RECENT_ACTIVITY, {"limit": limit * 2}).fetchall()
        activity: list[dict[str, Any]] = []
        for row in rows:
            unit = KnowledgeUnit.model_validate_json(row[1])
            proposed_ts = unit.evidence.first_observed.isoformat() if unit.evidence.first_observed else ""
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
        return activity[:limit]

    def _revoke_api_key_sync(self, *, user_id: int, key_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        # Inline SQL: no _queries.py constant covers this update shape.
        # The "revoked_at IS NULL" guard is what makes the second revoke a no-op.
        stmt = text(
            "UPDATE api_keys SET revoked_at = :now WHERE id = :key_id AND user_id = :user_id AND revoked_at IS NULL"
        )
        with self._engine.begin() as conn:
            cursor = conn.execute(stmt, {"now": now, "key_id": key_id, "user_id": user_id})
        return cursor.rowcount > 0

    async def _run_sync(self, fn, /, *args, **kwargs):
        """Run a sync callable on the default executor and await its result.

        All public async methods funnel SQL work through this shim so the
        sqlite3 driver's blocking calls don't tie up the event-loop thread.
        Centralises the closed-store guard so every public method rejects
        calls after ``close()``.
        """
        if self._closed:
            raise RuntimeError("SqliteStore is closed")
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _set_review_status_sync(self, unit_id: str, status: str, reviewed_by: str) -> None:
        reviewed_at = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            cursor = conn.execute(
                UPDATE_REVIEW_STATUS,
                {"id": unit_id, "status": status, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at},
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Knowledge unit not found: {unit_id}")

    def _touch_api_key_last_used_sync(self, key_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        stmt = text("UPDATE api_keys SET last_used_at = :now WHERE id = :key_id")
        try:
            with self._engine.begin() as conn:
                conn.execute(stmt, {"now": now, "key_id": key_id})
        except SQLAlchemyError:
            # Observability hook only; failures must not break the request path.
            _logger.exception("Failed to update last_used_at for api key %s", key_id)

    def _update_sync(self, unit: KnowledgeUnit) -> None:
        domains = normalize_domains(unit.domains)
        if not domains:
            raise ValueError("At least one non-empty domain is required")
        unit = unit.model_copy(update={"domains": domains})
        try:
            with self._engine.begin() as conn:
                cursor = conn.execute(
                    UPDATE_UNIT_DATA,
                    {"id": unit.id, "data": unit.model_dump_json(), "tier": unit.tier.value},
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Knowledge unit not found: {unit.id}")
                conn.execute(DELETE_UNIT_DOMAINS, {"unit_id": unit.id})
                for d in domains:
                    conn.execute(INSERT_UNIT_DOMAIN, {"unit_id": unit.id, "domain": d})
        except IntegrityError as e:
            if e.orig is not None:
                raise e.orig from e
            raise

    # ------------------------------------------------------------------
    # Sync impls for fork-delta methods (#105 PR-A)
    # SQLAlchemy text() + named-binding versions of the RemoteStore code.
    # ------------------------------------------------------------------

    def _upsert_directory_peering_sync(
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
        to_l2_endpoints_json: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO aigrp_directory_peerings (
                        offer_id, from_enterprise, to_enterprise, status,
                        content_policy, consult_logging_policy, topic_filters_json,
                        active_from, expires_at,
                        offer_payload_canonical, offer_signature_b64u, offer_signing_key_id,
                        accept_payload_canonical, accept_signature_b64u, accept_signing_key_id,
                        last_synced_at, to_l2_endpoints_json
                    ) VALUES (
                        :offer_id, :from_enterprise, :to_enterprise, :status,
                        :content_policy, :consult_logging_policy, :topic_filters_json,
                        :active_from, :expires_at,
                        :offer_payload_canonical, :offer_signature_b64u, :offer_signing_key_id,
                        :accept_payload_canonical, :accept_signature_b64u, :accept_signing_key_id,
                        :last_synced_at, :to_l2_endpoints_json
                    )
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
                        last_synced_at = excluded.last_synced_at,
                        to_l2_endpoints_json = excluded.to_l2_endpoints_json
                    """
                ),
                {
                    "offer_id": offer_id,
                    "from_enterprise": from_enterprise,
                    "to_enterprise": to_enterprise,
                    "status": status,
                    "content_policy": content_policy,
                    "consult_logging_policy": consult_logging_policy,
                    "topic_filters_json": topic_filters_json,
                    "active_from": active_from,
                    "expires_at": expires_at,
                    "offer_payload_canonical": offer_payload_canonical,
                    "offer_signature_b64u": offer_signature_b64u,
                    "offer_signing_key_id": offer_signing_key_id,
                    "accept_payload_canonical": accept_payload_canonical,
                    "accept_signature_b64u": accept_signature_b64u,
                    "accept_signing_key_id": accept_signing_key_id,
                    "last_synced_at": last_synced_at,
                    "to_l2_endpoints_json": to_l2_endpoints_json,
                },
            )

    def _find_active_directory_peering_sync(
        self,
        *,
        from_enterprise: str,
        to_enterprise: str,
        now_iso: str | None,
    ) -> dict[str, Any] | None:
        if now_iso is None:
            now_iso = datetime.now(UTC).isoformat()
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT * FROM aigrp_directory_peerings
                    WHERE status = 'active'
                      AND ((from_enterprise = :a AND to_enterprise = :b)
                        OR (from_enterprise = :b AND to_enterprise = :a))
                      AND expires_at > :now
                    ORDER BY active_from DESC
                    LIMIT 1
                    """
                    ),
                    {"a": from_enterprise, "b": to_enterprise, "now": now_iso},
                )
                .mappings()
                .fetchone()
            )
        return dict(row) if row else None

    def _list_directory_peerings_sync(
        self,
        *,
        enterprise_id: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM aigrp_directory_peerings"
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if enterprise_id is not None:
            clauses.append("(from_enterprise = :eid OR to_enterprise = :eid)")
            params["eid"] = enterprise_id
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY active_from DESC NULLS LAST, last_synced_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().fetchall()
        return [dict(r) for r in rows]

    def _upsert_aigrp_peer_sync(
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
        public_key_ed25519: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM aigrp_peers WHERE l2_id = :l2_id"),
                {"l2_id": l2_id},
            ).fetchone()
            if existing is None:
                conn.execute(
                    text(
                        """
                        INSERT INTO aigrp_peers (
                            l2_id, enterprise, "group", endpoint_url,
                            embedding_centroid, domain_bloom, ku_count, domain_count,
                            embedding_model, first_seen_at, last_seen_at, last_signature_at,
                            public_key_ed25519
                        ) VALUES (
                            :l2_id, :enterprise, :grp, :endpoint_url,
                            :embedding_centroid, :domain_bloom, :ku_count, :domain_count,
                            :embedding_model, :now, :now, :sig_at,
                            :public_key_ed25519
                        )
                        """
                    ),
                    {
                        "l2_id": l2_id,
                        "enterprise": enterprise,
                        "grp": group,
                        "endpoint_url": endpoint_url,
                        "embedding_centroid": embedding_centroid,
                        "domain_bloom": domain_bloom,
                        "ku_count": ku_count,
                        "domain_count": domain_count,
                        "embedding_model": embedding_model,
                        "now": now,
                        "sig_at": now if signature_received else None,
                        "public_key_ed25519": public_key_ed25519,
                    },
                )
            elif signature_received:
                conn.execute(
                    text(
                        """
                        UPDATE aigrp_peers SET
                            enterprise = :enterprise, "group" = :grp,
                            endpoint_url = :endpoint_url,
                            embedding_centroid = :embedding_centroid,
                            domain_bloom = :domain_bloom,
                            ku_count = :ku_count, domain_count = :domain_count,
                            embedding_model = :embedding_model,
                            last_seen_at = :now, last_signature_at = :now
                        WHERE l2_id = :l2_id
                        """
                    ),
                    {
                        "l2_id": l2_id,
                        "enterprise": enterprise,
                        "grp": group,
                        "endpoint_url": endpoint_url,
                        "embedding_centroid": embedding_centroid,
                        "domain_bloom": domain_bloom,
                        "ku_count": ku_count,
                        "domain_count": domain_count,
                        "embedding_model": embedding_model,
                        "now": now,
                    },
                )
                if public_key_ed25519 is not None:
                    conn.execute(
                        text("UPDATE aigrp_peers SET public_key_ed25519 = :pk WHERE l2_id = :l2_id"),
                        {"pk": public_key_ed25519, "l2_id": l2_id},
                    )
            else:
                # Touch last_seen but keep cached signature
                conn.execute(
                    text(
                        """
                        UPDATE aigrp_peers SET
                            enterprise = :enterprise, "group" = :grp,
                            endpoint_url = :endpoint_url,
                            last_seen_at = :now
                        WHERE l2_id = :l2_id
                        """
                    ),
                    {
                        "l2_id": l2_id,
                        "enterprise": enterprise,
                        "grp": group,
                        "endpoint_url": endpoint_url,
                        "now": now,
                    },
                )
                if public_key_ed25519 is not None:
                    conn.execute(
                        text("UPDATE aigrp_peers SET public_key_ed25519 = :pk WHERE l2_id = :l2_id"),
                        {"pk": public_key_ed25519, "l2_id": l2_id},
                    )

    def _get_aigrp_peer_pubkey_sync(self, l2_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT public_key_ed25519 FROM aigrp_peers WHERE l2_id = :l2_id"),
                {"l2_id": l2_id},
            ).fetchone()
        return row[0] if row else None

    def _list_aigrp_peers_sync(self, enterprise: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT l2_id, enterprise, "group", endpoint_url,
                           embedding_centroid, domain_bloom,
                           ku_count, domain_count, embedding_model,
                           first_seen_at, last_seen_at, last_signature_at,
                           public_key_ed25519
                    FROM aigrp_peers
                    WHERE enterprise = :enterprise
                    ORDER BY last_seen_at DESC
                    """
                ),
                {"enterprise": enterprise},
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
                "public_key_ed25519": r[12],
            }
            for r in rows
        ]

    def _approved_embeddings_iter_sync(self) -> list[bytes]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT embedding FROM knowledge_units WHERE status = 'approved' AND embedding IS NOT NULL")
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def _count_in_enterprise_sync(self, enterprise_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM knowledge_units WHERE enterprise_id = :eid"),
                {"eid": enterprise_id},
            ).fetchone()
        return int(row[0]) if row else 0

    def _set_user_role_sync(self, username: str, role: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("UPDATE users SET role = :role WHERE username = :u"),
                {"role": role, "u": username},
            )
        return cur.rowcount > 0

    def _set_ku_cross_group_allowed_sync(self, unit_id: str, allowed: bool) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("UPDATE knowledge_units SET cross_group_allowed = :v WHERE id = :id"),
                {"v": 1 if allowed else 0, "id": unit_id},
            )
        return cur.rowcount > 0

    _CONSENT_COLS = (
        "consent_id, requester_enterprise, responder_enterprise, "
        "requester_group, responder_group, policy, signed_by_admin, "
        "signed_at, expires_at, audit_log_id"
    )

    @staticmethod
    def _consent_row_to_dict(row: Any) -> dict[str, Any]:
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

    def _list_cross_enterprise_consents_sync(
        self,
        *,
        include_expired: bool,
        now_iso: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {self._CONSENT_COLS} FROM cross_enterprise_consents"
        params: dict[str, Any] = {}
        if not include_expired:
            sql += " WHERE expires_at IS NULL OR expires_at > :now"
            params["now"] = now_iso
        sql += " ORDER BY signed_at DESC LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [self._consent_row_to_dict(r) for r in rows]

    def _get_cross_enterprise_consent_sync(self, consent_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {self._CONSENT_COLS} FROM cross_enterprise_consents WHERE consent_id = :cid"),
                {"cid": consent_id},
            ).fetchone()
        return self._consent_row_to_dict(row) if row else None

    def _revoke_cross_enterprise_consent_sync(
        self,
        *,
        consent_id: str,
        revoked_at: str,
    ) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("UPDATE cross_enterprise_consents SET expires_at = :rt WHERE consent_id = :cid"),
                {"rt": revoked_at, "cid": consent_id},
            )
        return cur.rowcount > 0

    def _find_cross_enterprise_consent_sync(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT consent_id, requester_group, responder_group,
                           policy, signed_by_admin, signed_at, expires_at,
                           audit_log_id
                    FROM cross_enterprise_consents
                    WHERE requester_enterprise = :req
                      AND responder_enterprise = :resp
                      AND (expires_at IS NULL OR expires_at > :now)
                    """
                ),
                {"req": requester_enterprise, "resp": responder_enterprise, "now": now_iso},
            ).fetchall()
        if not rows:
            return None

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

    def _find_active_consent_for_pair_sync(
        self,
        *,
        requester_enterprise: str,
        responder_enterprise: str,
        requester_group: str | None,
        responder_group: str | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT consent_id FROM cross_enterprise_consents
                    WHERE requester_enterprise = :req
                      AND responder_enterprise = :resp
                      AND ((requester_group IS NULL AND :rg IS NULL)
                           OR requester_group = :rg)
                      AND ((responder_group IS NULL AND :sg IS NULL)
                           OR responder_group = :sg)
                      AND (expires_at IS NULL OR expires_at > :now)
                    LIMIT 1
                    """
                ),
                {
                    "req": requester_enterprise,
                    "resp": responder_enterprise,
                    "rg": requester_group,
                    "sg": responder_group,
                    "now": now_iso,
                },
            ).fetchone()
        if row is None:
            return None
        return self._get_cross_enterprise_consent_sync(row[0])

    def _insert_cross_enterprise_consent_sync(
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
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO cross_enterprise_consents (
                        consent_id, requester_enterprise, responder_enterprise,
                        requester_group, responder_group, policy,
                        signed_by_admin, signed_at, expires_at, audit_log_id
                    ) VALUES (
                        :consent_id, :requester_enterprise, :responder_enterprise,
                        :requester_group, :responder_group, :policy,
                        :signed_by_admin, :signed_at, :expires_at, :audit_log_id
                    )
                    """
                ),
                {
                    "consent_id": consent_id,
                    "requester_enterprise": requester_enterprise,
                    "responder_enterprise": responder_enterprise,
                    "requester_group": requester_group,
                    "responder_group": responder_group,
                    "policy": policy,
                    "signed_by_admin": signed_by_admin,
                    "signed_at": signed_at,
                    "expires_at": expires_at,
                    "audit_log_id": audit_log_id,
                },
            )

    def _record_cross_l2_audit_sync(
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
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO cross_l2_audit (
                        audit_id, ts, requester_l2_id, requester_enterprise,
                        requester_group, requester_persona,
                        responder_l2_id, responder_enterprise, responder_group,
                        policy_applied, result_count, consent_id
                    ) VALUES (
                        :audit_id, :ts, :requester_l2_id, :requester_enterprise,
                        :requester_group, :requester_persona,
                        :responder_l2_id, :responder_enterprise, :responder_group,
                        :policy_applied, :result_count, :consent_id
                    )
                    """
                ),
                {
                    "audit_id": audit_id,
                    "ts": ts,
                    "requester_l2_id": requester_l2_id,
                    "requester_enterprise": requester_enterprise,
                    "requester_group": requester_group,
                    "requester_persona": requester_persona,
                    "responder_l2_id": responder_l2_id,
                    "responder_enterprise": responder_enterprise,
                    "responder_group": responder_group,
                    "policy_applied": policy_applied,
                    "result_count": result_count,
                    "consent_id": consent_id,
                },
            )

    _CONSULT_COLS = (
        "thread_id, from_l2_id, from_persona, to_l2_id, to_persona, "
        "subject, status, claimed_by, created_at, closed_at, resolution_summary"
    )

    @staticmethod
    def _consult_row_to_dict(row: Any) -> dict[str, Any]:
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

    def _create_consult_sync(
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
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO consults (
                        thread_id, from_l2_id, from_persona,
                        to_l2_id, to_persona, subject,
                        status, created_at
                    ) VALUES (
                        :thread_id, :from_l2_id, :from_persona,
                        :to_l2_id, :to_persona, :subject,
                        'open', :created_at
                    )
                    """
                ),
                {
                    "thread_id": thread_id,
                    "from_l2_id": from_l2_id,
                    "from_persona": from_persona,
                    "to_l2_id": to_l2_id,
                    "to_persona": to_persona,
                    "subject": subject,
                    "created_at": created_at,
                },
            )

    def _get_consult_sync(self, thread_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {self._CONSULT_COLS} FROM consults WHERE thread_id = :tid"),
                {"tid": thread_id},
            ).fetchone()
        return self._consult_row_to_dict(row) if row else None

    def _append_consult_message_sync(
        self,
        *,
        message_id: str,
        thread_id: str,
        from_l2_id: str,
        from_persona: str,
        content: str,
        created_at: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO consult_messages (
                        message_id, thread_id, from_l2_id,
                        from_persona, content, created_at
                    ) VALUES (
                        :message_id, :thread_id, :from_l2_id,
                        :from_persona, :content, :created_at
                    )
                    """
                ),
                {
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "from_l2_id": from_l2_id,
                    "from_persona": from_persona,
                    "content": content,
                    "created_at": created_at,
                },
            )

    def _list_consult_messages_sync(self, thread_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT message_id, thread_id, from_l2_id,
                           from_persona, content, created_at
                    FROM consult_messages
                    WHERE thread_id = :tid
                    ORDER BY created_at ASC
                    """
                ),
                {"tid": thread_id},
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

    def _close_consult_sync(
        self,
        *,
        thread_id: str,
        closed_at: str,
        resolution_summary: str | None,
    ) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text(
                    """
                    UPDATE consults
                    SET status = 'closed', closed_at = :closed_at,
                        resolution_summary = :res
                    WHERE thread_id = :tid AND status != 'closed'
                    """
                ),
                {"closed_at": closed_at, "res": resolution_summary, "tid": thread_id},
            )
        return cur.rowcount > 0

    def _list_inbox_sync(
        self,
        *,
        to_l2_id: str,
        to_persona: str,
        include_closed: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {self._CONSULT_COLS} FROM consults WHERE to_l2_id = :to_l2 AND to_persona = :to_p"
        params: dict[str, Any] = {"to_l2": to_l2_id, "to_p": to_persona}
        if not include_closed:
            sql += " AND status != 'closed'"
        sql += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [self._consult_row_to_dict(r) for r in rows]

    def _set_embedding_sync(
        self,
        *,
        unit_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("UPDATE knowledge_units SET embedding = :emb, embedding_model = :model WHERE id = :id"),
                {"emb": embedding, "model": embedding_model, "id": unit_id},
            )
        return cur.rowcount > 0

    def _iter_unembedded_sync(
        self,
        *,
        status: str,
        limit: int,
    ) -> list[tuple[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, data FROM knowledge_units WHERE embedding IS NULL AND status = :status LIMIT :limit"),
                {"status": status, "limit": limit},
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _semantic_query_sync(
        self,
        *,
        query_vec: list[float],
        limit: int,
        status: str,
    ) -> list[tuple[KnowledgeUnit, float]]:
        import numpy as np

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT data, embedding FROM knowledge_units WHERE status = :status AND embedding IS NOT NULL"),
                {"status": status},
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

    def _delete_sync(
        self,
        *,
        unit_id: str,
        enterprise_id: str | None,
    ) -> bool:
        with self._engine.begin() as conn:
            if enterprise_id is not None:
                row = conn.execute(
                    text("SELECT 1 FROM knowledge_units WHERE id = :id AND enterprise_id = :eid"),
                    {"id": unit_id, "eid": enterprise_id},
                ).fetchone()
                if row is None:
                    return False
            conn.execute(
                text("DELETE FROM knowledge_unit_domains WHERE unit_id = :id"),
                {"id": unit_id},
            )
            cur = conn.execute(
                text("DELETE FROM knowledge_units WHERE id = :id"),
                {"id": unit_id},
            )
        return cur.rowcount > 0

    def _upsert_peer_sync(
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
        metadata_json: str | None,
    ) -> None:
        domains_json = json.dumps(expertise_domains) if expertise_domains is not None else None
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO peers (
                        persona, user_id, enterprise_id, group_id, last_seen_at,
                        expertise_domains, discoverable, working_dir_hint,
                        metadata_json
                    ) VALUES (
                        :persona, :user_id, :enterprise_id, :group_id, :last_seen_at,
                        :expertise_domains, :discoverable, :working_dir_hint,
                        :metadata_json
                    )
                    ON CONFLICT(persona) DO UPDATE SET
                        user_id = excluded.user_id,
                        enterprise_id = excluded.enterprise_id,
                        group_id = excluded.group_id,
                        last_seen_at = excluded.last_seen_at,
                        expertise_domains = excluded.expertise_domains,
                        discoverable = excluded.discoverable,
                        working_dir_hint = excluded.working_dir_hint,
                        metadata_json = excluded.metadata_json
                    """
                ),
                {
                    "persona": persona,
                    "user_id": user_id,
                    "enterprise_id": enterprise_id,
                    "group_id": group_id,
                    "last_seen_at": last_seen_at,
                    "expertise_domains": domains_json,
                    "discoverable": 1 if discoverable else 0,
                    "working_dir_hint": working_dir_hint,
                    "metadata_json": metadata_json,
                },
            )

    def _list_active_peers_sync(
        self,
        *,
        enterprise_id: str,
        since_iso: str,
        group_id: str | None,
        exclude_persona: str | None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT persona, user_id, enterprise_id, group_id, last_seen_at, "
            "expertise_domains, discoverable, working_dir_hint, metadata_json "
            "FROM peers "
            "WHERE enterprise_id = :eid AND last_seen_at >= :since "
            "AND discoverable = 1"
        )
        params: dict[str, Any] = {"eid": enterprise_id, "since": since_iso}
        if group_id is not None:
            sql += " AND group_id = :gid"
            params["gid"] = group_id
        if exclude_persona is not None:
            sql += " AND persona != :ex"
            params["ex"] = exclude_persona
        sql += " ORDER BY last_seen_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
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

    def _approved_domains_sync(self) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT d.domain "
                    "FROM knowledge_unit_domains d "
                    "JOIN knowledge_units ku ON ku.id = d.unit_id "
                    "WHERE ku.status = 'approved'"
                )
            ).fetchall()
        return {r[0] for r in rows if r[0]}
