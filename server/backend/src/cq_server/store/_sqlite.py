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
        from ..migrations import run_migrations

        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        run_migrations(f"sqlite:///{self._db_path}")
        self._engine: Engine = create_engine(
            f"sqlite:///{self._db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        event.listen(self._engine, "connect", _apply_sqlite_pragmas)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._engine.dispose)

    def close_sync(self) -> None:
        """Sync close — for callers outside an async context (tests, scripts)."""
        if self._closed:
            return
        self._closed = True
        self._engine.dispose()

    @property
    def _lock(self):  # type: ignore[no-untyped-def]
        """Compat shim: legacy callers used ``with store._lock:`` to serialise.

        SqliteStore relies on SQLAlchemy + WAL for concurrency; this shim
        returns a no-op context manager so legacy patterns keep working.
        Transitional only — delete once test fixtures have been ported off.
        """
        import contextlib

        return contextlib.nullcontext()

    @property
    def _conn(self):  # type: ignore[no-untyped-def]
        """Compat shim: legacy callers used ``store._conn.execute(...)`` for raw SQL.

        Returns a cached DBAPI connection so legacy patterns like
        ``with store._lock, store._conn: store._conn.execute(...)`` use
        the same connection across both ``store._conn`` references in the
        with-statement. Cached on first access; cleaned up by ``close()``.
        Transitional only — delete once test fixtures have been ported off.
        """
        if not hasattr(self, "_compat_conn") or self._compat_conn is None:
            self._compat_conn = self._engine.raw_connection()
        return self._compat_conn

    @property
    def sync(self) -> _SyncStoreProxy:
        """Sync proxy for callers not in an async context.

        Tests + sync fixtures use ``store.sync.method(...)`` instead of
        ``await store.method(...)``. Each call forwards to the matching
        ``_<name>_sync`` private implementation. Transitional shim for
        the PR-B cutover.
        """
        return _SyncStoreProxy(self)

    async def confidence_distribution(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        return await self._run_sync(
            self._confidence_distribution_sync,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def count(self) -> int:
        return await self._run_sync(self._count_sync)

    async def count_active_api_keys_for_user(self, user_id: int) -> int:
        return await self._run_sync(self._count_active_api_keys_for_user_sync, user_id)

    async def counts_by_status(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        return await self._run_sync(
            self._counts_by_status_sync,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def counts_by_tier(self, *, enterprise_id: str | None = None) -> dict[str, int]:
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

    # --- WebAuthn / passkey credentials (FO-1a, #191) ---------------------

    async def insert_webauthn_credential(
        self,
        *,
        user_id: int,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: str | None = None,
        aaguid: bytes | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        return await self._run_sync(
            self._insert_webauthn_credential_sync,
            user_id=user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            aaguid=aaguid,
            name=name,
        )

    async def list_webauthn_credentials_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self._run_sync(self._list_webauthn_credentials_for_user_sync, user_id)

    async def get_webauthn_credential_by_id(self, credential_id: bytes) -> dict[str, Any] | None:
        return await self._run_sync(self._get_webauthn_credential_by_id_sync, credential_id)

    async def update_webauthn_sign_count(self, *, credential_id: bytes, new_sign_count: int, last_used_at: str) -> None:
        await self._run_sync(
            self._update_webauthn_sign_count_sync,
            credential_id=credential_id,
            new_sign_count=new_sign_count,
            last_used_at=last_used_at,
        )

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_user_by_email_sync, email)

    async def get_l2_brand(self) -> dict[str, Any] | None:
        """Return the single-row L2 brand override row, or ``None``.

        FO-1d (#199) — the ``l2_brand`` table has a CHECK (id = 1)
        constraint so at most one row exists. Reader uses ``WHERE id = 1``
        to make the "row absent" case explicit and the "row present"
        case a primary-key lookup. Returns ``None`` when the table is
        empty (admin hasn't customised the L2 brand yet); the resolver
        in ``cq_server.theme`` falls back to env-pinned defaults.
        """
        return await self._run_sync(self._get_l2_brand_sync)

    def _get_l2_brand_sync(self) -> dict[str, Any] | None:
        stmt = text("SELECT id, l2_label, subaccent_hex, hero_motif, updated_at, updated_by FROM l2_brand WHERE id = 1")
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return dict(row)

    async def daily_counts(
        self,
        *,
        days: int = 30,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if days <= 0:
            raise ValueError("days must be positive")
        return await self._run_sync(
            self._daily_counts_sync,
            days=days,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def domain_counts(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        return await self._run_sync(
            self._domain_counts_sync,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def get(self, unit_id: str) -> KnowledgeUnit | None:
        return await self._run_sync(self._get_sync, unit_id)

    async def get_active_api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_active_api_key_by_id_sync, key_id)

    async def get_any(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> KnowledgeUnit | None:
        return await self._run_sync(
            self._get_any_sync,
            unit_id,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def get_api_key_for_user(self, *, user_id: int, key_id: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_api_key_for_user_sync, user_id=user_id, key_id=key_id)

    async def get_review_status(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, str | None] | None:
        return await self._run_sync(
            self._get_review_status_sync,
            unit_id,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def get_user(self, username: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_user_sync, username)

    async def insert(
        self,
        unit: KnowledgeUnit,
        *,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """Insert a KU; optionally pin its tenancy.

        ``enterprise_id`` / ``group_id`` are the auth-claim values from
        the propose request. When set, they are written into the row
        directly — closes #89 (KUs landing in ``default-enterprise``
        regardless of the caller's actual scope). When unset (legacy
        fixture / migration-smoke-test path), the schema-level
        ``server_default`` populates the columns instead.

        The two-variant approach keeps the legacy test surface
        (``store.sync.insert(unit)`` with no kwargs) green while making
        every API path honour real tenancy.
        """
        await self._run_sync(
            self._insert_sync,
            unit,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )
        if embedding is not None and embedding_model is not None:
            await self.set_embedding(unit.id, embedding, embedding_model)

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
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run_sync(
            self._list_units_sync,
            domain=domain,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
            status=status,
            limit=limit,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def pending_count(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> int:
        return await self._run_sync(
            self._pending_count_sync,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def pending_queue(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run_sync(
            self._pending_queue_sync,
            limit=limit,
            offset=offset,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def query(
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
        return await self._run_sync(
            self._query_sync,
            domains,
            languages=languages,
            frameworks=frameworks,
            pattern=pattern,
            limit=limit,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def recent_activity(
        self,
        limit: int = 20,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run_sync(
            self._recent_activity_sync,
            limit=limit,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def revoke_api_key(self, *, user_id: int, key_id: str) -> bool:
        return await self._run_sync(self._revoke_api_key_sync, user_id=user_id, key_id=key_id)

    async def set_review_status(
        self,
        unit_id: str,
        status: str,
        reviewed_by: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> bool:
        """Transition a KU's review status with optimistic concurrency.

        Returns ``True`` when the UPDATE matched a row (the caller won
        the race) and ``False`` when the row was already in a terminal
        state (another admin resolved it concurrently). Raises
        ``KeyError`` only when the unit_id doesn't exist at all — the
        race-loss case (row exists but is already terminal) is the
        ``False`` branch, distinguished so route handlers can return
        409 instead of 404.
        """
        return await self._run_sync(
            self._set_review_status_sync,
            unit_id,
            status,
            reviewed_by,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    # --- pending_review tier (#103) -------------------------------------
    #
    # Hard-finding queue: KUs flagged by reflect's VIBE√ classifier as
    # needing human approval before tier-promotion. ``submit_pending_review``
    # is the entry point (called from /reflect/submit?queue_hard_findings=true
    # in a follow-up); ``list_pending_review`` drives the admin queue UI;
    # ``expire_pending_reviews`` is the TTL sweeper. Reuses the existing
    # ``status`` column rather than adding a new tier (cq SDK Tier enum is
    # PyPI-pinned; see migration 0012 for the rationale).

    async def submit_pending_review(
        self,
        unit: KnowledgeUnit,
        *,
        reason: str,
        expires_at: str,
        enterprise_id: str,
        group_id: str,
    ) -> None:
        """Insert a KU at ``status='pending_review'`` with reason + TTL.

        Same code path as ``insert``, then a follow-up UPDATE sets
        ``status='pending_review'`` plus the two pending_review columns.
        Doing it as INSERT-then-UPDATE rather than a custom INSERT keeps
        the standard insert path (with quality guards, domain rows, and
        embedding stub) intact.
        """
        await self._run_sync(
            self._submit_pending_review_sync,
            unit,
            reason=reason,
            expires_at=expires_at,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def list_pending_review(
        self,
        *,
        enterprise_id: str,
        limit: int = 20,
        offset: int = 0,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return KUs in pending_review state for the admin queue."""
        return await self._run_sync(
            self._list_pending_review_sync,
            enterprise_id=enterprise_id,
            limit=limit,
            offset=offset,
            group_id=group_id,
        )

    async def count_pending_review(self, *, enterprise_id: str, group_id: str | None = None) -> int:
        """Return the size of the pending_review queue for one tenant."""
        return await self._run_sync(
            self._count_pending_review_sync,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def expire_pending_reviews(self, *, enterprise_id: str, now_iso: str) -> list[str]:
        """Sweep expired pending_review rows for one tenant.

        Transitions every row with ``status='pending_review'`` AND
        ``pending_review_expires_at < now_iso`` to ``status='dropped'``.
        Returns the list of unit_ids transitioned so the caller can log
        the per-row drops to the activity log.
        """
        return await self._run_sync(
            self._expire_pending_reviews_sync,
            enterprise_id=enterprise_id,
            now_iso=now_iso,
        )

    async def touch_api_key_last_used(self, key_id: str) -> None:
        await self._run_sync(self._touch_api_key_last_used_sync, key_id)

    async def update(self, unit: KnowledgeUnit) -> None:
        await self._run_sync(self._update_sync, unit)

    # ------------------------------------------------------------------
    # Fork-delta: directory peerings (#105 PR-A)
    # Sync mirror of the async surface for callers without a running event loop. Backed by the
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

    async def semantic_query_with_scope(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        status: str = "approved",
    ) -> list[dict[str, Any]]:
        """Cosine-rank approved KUs returning scope + xgroup flag per row.

        Used by /aigrp/forward-query — policy-eval needs enterprise_id /
        group_id / cross_group_allowed per candidate KU.
        """
        return await self._run_sync(
            self._semantic_query_with_scope_sync,
            query_vec=query_vec,
            limit=limit,
            status=status,
        )

    async def delete(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> bool:
        """Hard-delete a KU; tenant-scoped when enterprise_id provided."""
        return await self._run_sync(
            self._delete_sync,
            unit_id=unit_id,
            enterprise_id=enterprise_id,
            group_id=group_id,
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

    # --- activity_log (#108) ---------------------------------------------
    #
    # Append-only audit-of-record. Stage-1 substrate (this PR) ships the
    # write path + retention helpers; Stage-2 wires every existing
    # handler (query / propose / confirm / flag / review / consult /
    # crosstalk) to call ``append_activity`` from a FastAPI
    # ``BackgroundTask``. Failures inside the helper are *not* swallowed
    # here — the caller wraps the call in its own try/except so the
    # response path stays green when the audit log is unavailable.

    async def append_activity(
        self,
        *,
        activity_id: str,
        ts: str,
        tenant_enterprise: str,
        tenant_group: str | None,
        persona: str | None,
        human: str | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        thread_or_chain_id: str | None = None,
    ) -> None:
        """Append one row to ``activity_log``.

        Args carry the schema sketch from #108 verbatim. ``payload`` and
        ``result_summary`` are JSON-serialised here; the table column
        type is TEXT (SQLite has no JSON type — see migration 0011 for
        the full rationale). ``event_type`` is validated against
        ``cq_server.activity.EVENT_TYPES`` before hitting the DB so we
        get a clean ``ValueError`` rather than a CHECK-constraint
        IntegrityError on the wire.
        """
        from ..activity import EVENT_TYPES

        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown activity event_type {event_type!r}; expected one of {sorted(EVENT_TYPES)}")
        await self._run_sync(
            self._append_activity_sync,
            activity_id=activity_id,
            ts=ts,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            persona=persona,
            human=human,
            event_type=event_type,
            payload=payload,
            result_summary=result_summary,
            thread_or_chain_id=thread_or_chain_id,
        )

    async def get_activity_retention_days(self, *, enterprise_id: str) -> int:
        """Return the configured retention window for an Enterprise.

        Falls back to ``DEFAULT_RETENTION_DAYS`` (90) when no override
        row exists. Stage-2 cron uses this to compute the per-tenant
        cutoff before calling ``purge_activity_older_than``.
        """
        return await self._run_sync(
            self._get_activity_retention_days_sync,
            enterprise_id=enterprise_id,
        )

    async def set_activity_retention_days(self, *, enterprise_id: str, retention_days: int) -> None:
        """Upsert the retention window for one Enterprise."""
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        await self._run_sync(
            self._set_activity_retention_days_sync,
            enterprise_id=enterprise_id,
            retention_days=retention_days,
        )

    async def purge_activity_older_than(self, *, tenant_enterprise: str, cutoff_iso: str) -> int:
        """Delete activity rows for one tenant older than ``cutoff_iso``.

        Returns the number of deleted rows so the cron can log
        observability metrics. Scoped per-Enterprise so a slow
        large-tenant sweep doesn't lock writes for every other tenant
        on the same L2.
        """
        return await self._run_sync(
            self._purge_activity_older_than_sync,
            tenant_enterprise=tenant_enterprise,
            cutoff_iso=cutoff_iso,
        )

    async def list_activity(
        self,
        *,
        tenant_enterprise: str,
        tenant_group: str | None = None,
        persona: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        cursor: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read rows from ``activity_log`` with filter + cursor pagination.

        Powers ``GET /api/v1/activity`` (Stage 2 of #108). All filters
        compose with AND. ``cursor`` is a ``(ts, id)`` tuple from the
        last row of the previous page — the next page reads strictly
        before that point on the ``(ts DESC, id DESC)`` ordering. Pure
        keyset pagination; no offset arithmetic.

        Tenancy is mandatory (``tenant_enterprise`` is required, never
        nullable on this read path) so the route layer can never
        accidentally return cross-tenant rows.
        """
        return await self._run_sync(
            self._list_activity_sync,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            persona=persona,
            since_iso=since_iso,
            until_iso=until_iso,
            event_type=event_type,
            limit=limit,
            cursor=cursor,
        )

    # --- crosstalk_threads + crosstalk_messages (#124) -------------------
    #
    # L2-mediated inter-session messaging. Migration 0014 creates the
    # tables; routes in cq_server/crosstalk_routes.py consume these
    # helpers. Tenancy is pinned at create-time from the caller's user
    # row; reads are tenant-scoped by the route layer.

    async def create_crosstalk_thread(
        self,
        *,
        thread_id: str,
        subject: str,
        enterprise_id: str,
        group_id: str,
        created_at: str,
        created_by_username: str,
        participants: list[str],
    ) -> None:
        """Insert a new ``crosstalk_threads`` row.

        ``participants`` is a list of usernames who can read/reply on
        this thread; at minimum it should contain the creator + the
        initial recipient. JSON-encoded on the wire.
        """
        await self._run_sync(
            self._create_crosstalk_thread_sync,
            thread_id=thread_id,
            subject=subject,
            enterprise_id=enterprise_id,
            group_id=group_id,
            created_at=created_at,
            created_by_username=created_by_username,
            participants=participants,
        )

    async def append_crosstalk_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        from_username: str,
        from_persona: str | None,
        to_username: str | None,
        content: str,
        sent_at: str,
        enterprise_id: str,
        group_id: str,
    ) -> None:
        """Insert a new message into an existing thread.

        ``to_username`` is optional for fan-out / multi-party threads;
        for two-party direct messages the route layer should always
        populate it so the inbox query can target it cleanly.
        """
        await self._run_sync(
            self._append_crosstalk_message_sync,
            message_id=message_id,
            thread_id=thread_id,
            from_username=from_username,
            from_persona=from_persona,
            to_username=to_username,
            content=content,
            sent_at=sent_at,
            enterprise_id=enterprise_id,
            group_id=group_id,
        )

    async def get_crosstalk_thread(
        self,
        *,
        thread_id: str,
        tenant_enterprise: str,
        tenant_group: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch one thread row by id, tenancy-scoped.

        Returns ``None`` when the thread doesn't exist OR exists in a
        different enterprise (route layer should treat both as 404 to
        avoid information leakage).
        """
        return await self._run_sync(
            self._get_crosstalk_thread_sync,
            thread_id=thread_id,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
        )

    async def list_crosstalk_messages(
        self,
        *,
        thread_id: str,
        tenant_enterprise: str,
        tenant_group: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return messages on a thread, oldest first, tenancy-scoped."""
        return await self._run_sync(
            self._list_crosstalk_messages_sync,
            thread_id=thread_id,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            limit=limit,
        )

    async def list_crosstalk_threads_for_user(
        self,
        *,
        username: str,
        tenant_enterprise: str,
        tenant_group: str | None = None,
        is_admin: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Threads visible to ``username`` in ``tenant_enterprise``.

        Non-admin: filtered to threads where the user is a participant.
        Admin: all threads in the tenant (audit shape per Pass 2 Part 3
        Ch 16's directory-sees-only-metadata model — admin needs the
        oversight surface).
        """
        return await self._run_sync(
            self._list_crosstalk_threads_for_user_sync,
            username=username,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            is_admin=is_admin,
            limit=limit,
        )

    async def close_crosstalk_thread(
        self,
        *,
        thread_id: str,
        closed_by_username: str,
        closed_at: str,
        reason: str | None,
        tenant_enterprise: str,
        tenant_group: str | None = None,
    ) -> bool:
        """Mark thread closed. Returns False if thread doesn't exist or already closed."""
        return await self._run_sync(
            self._close_crosstalk_thread_sync,
            thread_id=thread_id,
            closed_by_username=closed_by_username,
            closed_at=closed_at,
            reason=reason,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
        )

    async def crosstalk_inbox_for_user(
        self,
        *,
        username: str,
        tenant_enterprise: str,
        tenant_group: str | None = None,
        limit: int = 50,
        mark_read: bool = False,
        read_at_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Unread messages addressed to ``username``, tenancy-scoped.

        If ``mark_read=True``, atomically populate ``read_at`` for each
        returned message with ``read_at_iso`` (caller-supplied so the
        timestamp matches whatever clock semantics the route layer
        uses). Default is read-only (no mutation).
        """
        return await self._run_sync(
            self._crosstalk_inbox_for_user_sync,
            username=username,
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            limit=limit,
            mark_read=mark_read,
            read_at_iso=read_at_iso,
        )

    # --- reflect_submissions (#67) ---------------------------------------
    #
    # Persistence layer for the batch-reflect contract. The router layer
    # calls these via the async wrappers; the worker (separate process,
    # not in this PR) will use the same surface to drive Anthropic Batch
    # dispatch and ingest results.

    async def create_reflect_submission(
        self,
        *,
        submission_id: str,
        session_id: str,
        user_id: int,
        enterprise_id: str,
        group_id: str | None,
        context_hash: str,
        mode: str,
        max_candidates: int,
        since_ts: str | None,
        submitted_at: str,
    ) -> None:
        await self._run_sync(
            self._create_reflect_submission_sync,
            submission_id=submission_id,
            session_id=session_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            group_id=group_id,
            context_hash=context_hash,
            mode=mode,
            max_candidates=max_candidates,
            since_ts=since_ts,
            submitted_at=submitted_at,
        )

    async def get_reflect_submission(self, submission_id: str) -> dict[str, Any] | None:
        return await self._run_sync(self._get_reflect_submission_sync, submission_id)

    async def find_recent_reflect_dedup(
        self,
        *,
        session_id: str,
        context_hash: str,
        window_start_iso: str,
    ) -> dict[str, Any] | None:
        """Return the most recent submission with matching (session, hash) within the dedup window."""
        return await self._run_sync(
            self._find_recent_reflect_dedup_sync,
            session_id=session_id,
            context_hash=context_hash,
            window_start_iso=window_start_iso,
        )

    async def count_recent_reflect_submissions(
        self,
        *,
        session_id: str,
        window_start_iso: str,
    ) -> int:
        return await self._run_sync(
            self._count_recent_reflect_submissions_sync,
            session_id=session_id,
            window_start_iso=window_start_iso,
        )

    async def get_oldest_reflect_in_window(
        self,
        *,
        session_id: str,
        window_start_iso: str,
    ) -> str | None:
        """Return ``submitted_at`` of the oldest submission for this session within the rate-limit window."""
        return await self._run_sync(
            self._get_oldest_reflect_in_window_sync,
            session_id=session_id,
            window_start_iso=window_start_iso,
        )

    async def get_last_reflect_for_session(
        self,
        *,
        session_id: str,
        enterprise_id: str,
    ) -> dict[str, Any] | None:
        return await self._run_sync(
            self._get_last_reflect_for_session_sync,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )

    async def list_reflect_submissions_for_recovery(self, *, lookback_iso: str) -> list[dict[str, Any]]:
        """Return non-terminal submissions with non-null ``anthropic_batch_id`` newer than ``lookback_iso``.

        Used by the worker's R6 startup recovery scan; not exercised by
        the contract surface this PR ships, but the store method lives
        with its peers.
        """
        return await self._run_sync(
            self._list_reflect_submissions_for_recovery_sync,
            lookback_iso=lookback_iso,
        )

    def _confidence_distribution_sync(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        buckets = {"0.0-0.3": 0, "0.3-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT data FROM knowledge_units WHERE status = 'approved' AND enterprise_id = ? AND group_id = ?",
                    (enterprise_id, group_id),
                ).fetchall()
            elif enterprise_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT data FROM knowledge_units WHERE status = 'approved' AND enterprise_id = ?",
                    (enterprise_id,),
                ).fetchall()
            else:
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

    def _counts_by_status_sync(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT COALESCE(status, 'pending'), COUNT(*) FROM knowledge_units "
                    "WHERE enterprise_id = ? AND group_id = ? GROUP BY status",
                    (enterprise_id, group_id),
                ).fetchall()
            elif enterprise_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT COALESCE(status, 'pending'), COUNT(*) FROM knowledge_units "
                    "WHERE enterprise_id = ? GROUP BY status",
                    (enterprise_id,),
                ).fetchall()
            else:
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

    def _daily_counts_sync(
        self,
        *,
        days: int = 30,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if days <= 0:
            raise ValueError("days must be positive")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        from ._queries import SELECT_APPROVED_DAILY, SELECT_PROPOSED_DAILY, SELECT_REJECTED_DAILY

        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                proposed = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(created_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE created_at >= ? AND enterprise_id = ? AND group_id = ? "
                        "GROUP BY day",
                        (cutoff, enterprise_id, group_id),
                    ).fetchall()
                }
                approved = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(reviewed_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE status = 'approved' AND reviewed_at >= ? "
                        "AND enterprise_id = ? AND group_id = ? GROUP BY day",
                        (cutoff, enterprise_id, group_id),
                    ).fetchall()
                }
                rejected = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(reviewed_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE status = 'rejected' AND reviewed_at >= ? "
                        "AND enterprise_id = ? AND group_id = ? GROUP BY day",
                        (cutoff, enterprise_id, group_id),
                    ).fetchall()
                }
            elif enterprise_id is not None:
                proposed = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(created_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE created_at >= ? AND enterprise_id = ? GROUP BY day",
                        (cutoff, enterprise_id),
                    ).fetchall()
                }
                approved = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(reviewed_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE status = 'approved' AND reviewed_at >= ? "
                        "AND enterprise_id = ? GROUP BY day",
                        (cutoff, enterprise_id),
                    ).fetchall()
                }
                rejected = {
                    r[0]: r[1]
                    for r in conn.exec_driver_sql(
                        "SELECT date(reviewed_at) AS day, COUNT(*) FROM knowledge_units "
                        "WHERE status = 'rejected' AND reviewed_at >= ? "
                        "AND enterprise_id = ? GROUP BY day",
                        (cutoff, enterprise_id),
                    ).fetchall()
                }
            else:
                proposed = {
                    row[0]: row[1] for row in conn.execute(SELECT_PROPOSED_DAILY, {"cutoff": cutoff}).fetchall()
                }
                approved = {
                    row[0]: row[1] for row in conn.execute(SELECT_APPROVED_DAILY, {"cutoff": cutoff}).fetchall()
                }
                rejected = {
                    row[0]: row[1] for row in conn.execute(SELECT_REJECTED_DAILY, {"cutoff": cutoff}).fetchall()
                }
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

    def _domain_counts_sync(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, int]:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT d.domain, COUNT(*) "
                    "FROM knowledge_unit_domains d "
                    "JOIN knowledge_units ku ON ku.id = d.unit_id "
                    "WHERE ku.status = 'approved' "
                    "AND ku.enterprise_id = ? AND ku.group_id = ? "
                    "GROUP BY d.domain ORDER BY COUNT(*) DESC",
                    (enterprise_id, group_id),
                ).fetchall()
            elif enterprise_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT d.domain, COUNT(*) "
                    "FROM knowledge_unit_domains d "
                    "JOIN knowledge_units ku ON ku.id = d.unit_id "
                    "WHERE ku.status = 'approved' "
                    "AND ku.enterprise_id = ? "
                    "GROUP BY d.domain ORDER BY COUNT(*) DESC",
                    (enterprise_id,),
                ).fetchall()
            else:
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

    def _get_any_sync(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> KnowledgeUnit | None:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT data FROM knowledge_units WHERE id = ? AND enterprise_id = ? AND group_id = ?",
                    (unit_id, enterprise_id, group_id),
                ).fetchone()
            elif enterprise_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT data FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
            else:
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

    def _get_review_status_sync(
        self,
        unit_id: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, str | None] | None:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT status, reviewed_by, reviewed_at FROM knowledge_units "
                    "WHERE id = ? AND enterprise_id = ? AND group_id = ?",
                    (unit_id, enterprise_id, group_id),
                ).fetchone()
            elif enterprise_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT status, reviewed_by, reviewed_at FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
            else:
                row = conn.execute(SELECT_REVIEW_STATUS_BY_ID, {"id": unit_id}).fetchone()
        if row is None:
            return None
        return {"status": row[0], "reviewed_by": row[1], "reviewed_at": row[2]}

    def _get_sync(self, unit_id: str) -> KnowledgeUnit | None:
        with self._engine.connect() as conn:
            row = conn.execute(SELECT_APPROVED_BY_ID, {"id": unit_id}).fetchone()
        return KnowledgeUnit.model_validate_json(row[0]) if row is not None else None

    def _get_user_sync(self, username: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, username, password_hash, created_at, role, "
                    "enterprise_id, group_id FROM users WHERE username = :u"
                ),
                {"u": username},
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
            "role": row[4],
            "enterprise_id": row[5],
            "group_id": row[6],
        }

    def _insert_sync(
        self,
        unit: KnowledgeUnit,
        *,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        # embedding kwargs: persist via UPDATE after the INSERT.
        # Acceptance is silent here; the actual write happens via _set_embedding_sync.
        #
        # ``enterprise_id`` / ``group_id`` are the auth-claim values from
        # the propose handler (closes #89). When both are provided we
        # use ``INSERT_UNIT_WITH_TENANCY`` to write them explicitly; when
        # either is None the legacy ``INSERT_UNIT`` runs and the schema's
        # ``server_default`` (``default-enterprise`` / ``default-group``)
        # fills the column. The two-variant split keeps legacy fixture
        # callers (``store.sync.insert(unit)`` with no kwargs) green
        # without forcing every test to manufacture a tenant.
        from ._queries import INSERT_UNIT_WITH_TENANCY

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
                if enterprise_id is not None and group_id is not None:
                    conn.execute(
                        INSERT_UNIT_WITH_TENANCY,
                        {
                            "id": unit.id,
                            "data": unit.model_dump_json(),
                            "created_at": created_at,
                            "tier": unit.tier.value,
                            "enterprise_id": enterprise_id,
                            "group_id": group_id,
                        },
                    )
                else:
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
                if embedding is not None and embedding_model is not None:
                    conn.execute(
                        text("UPDATE knowledge_units SET embedding = :emb, embedding_model = :model WHERE id = :id"),
                        {"emb": embedding, "model": embedding_model, "id": unit.id},
                    )
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
        domain: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        status: str | None = None,
        limit: int = 100,
        enterprise_id: str | None = None,
        group_id: str | None = None,
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
            enterprise_id=enterprise_id,
            group_id=group_id,
        )
        params: dict[str, Any] = {}
        if normalized_domain is not None:
            params["domain"] = normalized_domain
        if normalized_status is not None:
            params["status"] = normalized_status
        if enterprise_id is not None:
            params["enterprise_id"] = enterprise_id
        if group_id is not None:
            params["group_id"] = group_id
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

    def _pending_count_sync(
        self,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> int:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                return int(
                    conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM knowledge_units "
                        "WHERE status = 'pending' AND enterprise_id = ? AND group_id = ?",
                        (enterprise_id, group_id),
                    ).scalar()
                    or 0
                )
            if enterprise_id is not None:
                return int(
                    conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM knowledge_units WHERE status = 'pending' AND enterprise_id = ?",
                        (enterprise_id,),
                    ).scalar()
                    or 0
                )
            return int(conn.execute(SELECT_PENDING_COUNT).scalar() or 0)

    def _pending_queue_sync(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT data, status, reviewed_by, reviewed_at FROM knowledge_units "
                    "WHERE status = 'pending' AND enterprise_id = ? AND group_id = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (enterprise_id, group_id, limit, offset),
                ).fetchall()
            elif enterprise_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT data, status, reviewed_by, reviewed_at FROM knowledge_units "
                    "WHERE status = 'pending' AND enterprise_id = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (enterprise_id, limit, offset),
                ).fetchall()
            else:
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
        languages: list[str] | None = None,
        frameworks: list[str] | None = None,
        pattern: str = "",
        limit: int = 5,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[KnowledgeUnit]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        normalized = normalize_domains(domains)
        if not normalized:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(SELECT_QUERY_UNITS, {"domains": normalized}).fetchall()
        if enterprise_id is not None:
            kus = [KnowledgeUnit.model_validate_json(r[0]) for r in rows]
            ids = [k.id for k in kus]
            scope_by_id: dict[str, tuple[str, str, int]] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                with self._engine.connect() as conn2:
                    sc_rows = conn2.exec_driver_sql(
                        "SELECT id, enterprise_id, group_id, cross_group_allowed "
                        f"FROM knowledge_units WHERE id IN ({placeholders})",
                        tuple(ids),
                    ).fetchall()
                scope_by_id = {sr[0]: (sr[1], sr[2], sr[3]) for sr in sc_rows}
            filtered: list[KnowledgeUnit] = []
            for k in kus:
                ent, grp, xgrp = scope_by_id.get(k.id, ("", "", 0))
                if ent != enterprise_id:
                    continue
                if group_id is not None and grp != group_id and not xgrp:
                    continue
                filtered.append(k)
            units = filtered
        else:
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
        # Tie-break: score desc, id desc on tie.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [u for _, _, u in scored[:limit]]

    def _recent_activity_sync(
        self,
        *,
        limit: int,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Over-fetch by 2x to give buffer; the SELECT already ORDER BYs
        # COALESCE(reviewed_at, created_at) DESC. Final slice trims to limit.
        with self._engine.connect() as conn:
            if enterprise_id is not None and group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT id, data, status, reviewed_by, reviewed_at "
                    "FROM knowledge_units "
                    "WHERE enterprise_id = ? AND group_id = ? "
                    "ORDER BY COALESCE(reviewed_at, created_at) DESC LIMIT ?",
                    (enterprise_id, group_id, limit * 2),
                ).fetchall()
            elif enterprise_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT id, data, status, reviewed_by, reviewed_at "
                    "FROM knowledge_units "
                    "WHERE enterprise_id = ? "
                    "ORDER BY COALESCE(reviewed_at, created_at) DESC LIMIT ?",
                    (enterprise_id, limit * 2),
                ).fetchall()
            else:
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

    def _set_review_status_sync(
        self,
        unit_id: str,
        status: str,
        reviewed_by: str,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
    ) -> bool:
        """Issue the guarded UPDATE; tell ``KeyError`` (no row) apart from race-loss.

        ``UPDATE_REVIEW_STATUS`` ANDs the WHERE clause on
        ``status NOT IN ('approved','rejected','dropped')``. Two paths
        produce ``rowcount == 0``:

        * The unit_id doesn't exist — raise ``KeyError`` (legacy
          behaviour, preserved for callers that 404 on missing).
        * The unit_id exists but is already terminal — return ``False``
          so the caller can 409 instead of overwriting the prior
          decision.

        We disambiguate with a follow-up SELECT inside the same
        transaction so the answer reflects the same snapshot the UPDATE
        saw. SQLite's default isolation is serialisable on a single
        write connection, so no extra locking is needed.

        Decision 27: when ``enterprise_id`` (and optionally ``group_id``)
        is provided, the UPDATE is also scoped by that tenant. The
        disambiguation SELECT honours the same scope so a cross-tenant
        id surfaces as ``KeyError`` (caller 404s), not ``False`` (409).
        """
        reviewed_at = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            if enterprise_id is not None and group_id is not None:
                cursor = conn.exec_driver_sql(
                    "UPDATE knowledge_units SET status = ?, reviewed_by = ?, reviewed_at = ? "
                    "WHERE id = ? AND enterprise_id = ? AND group_id = ? "
                    "AND status NOT IN ('approved', 'rejected', 'dropped')",
                    (status, reviewed_by, reviewed_at, unit_id, enterprise_id, group_id),
                )
            elif enterprise_id is not None:
                cursor = conn.exec_driver_sql(
                    "UPDATE knowledge_units SET status = ?, reviewed_by = ?, reviewed_at = ? "
                    "WHERE id = ? AND enterprise_id = ? "
                    "AND status NOT IN ('approved', 'rejected', 'dropped')",
                    (status, reviewed_by, reviewed_at, unit_id, enterprise_id),
                )
            else:
                cursor = conn.execute(
                    UPDATE_REVIEW_STATUS,
                    {"id": unit_id, "status": status, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at},
                )
            if cursor.rowcount > 0:
                return True
            if enterprise_id is not None and group_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT 1 FROM knowledge_units WHERE id = ? AND enterprise_id = ? AND group_id = ?",
                    (unit_id, enterprise_id, group_id),
                ).fetchone()
            elif enterprise_id is not None:
                row = conn.exec_driver_sql(
                    "SELECT 1 FROM knowledge_units WHERE id = ? AND enterprise_id = ?",
                    (unit_id, enterprise_id),
                ).fetchone()
            else:
                row = conn.exec_driver_sql(
                    "SELECT 1 FROM knowledge_units WHERE id = ?",
                    (unit_id,),
                ).fetchone()
            if row is None:
                raise KeyError(f"Knowledge unit not found: {unit_id}")
            return False

    # --- pending_review tier (#103) sync impls ---------------------------

    def _submit_pending_review_sync(
        self,
        unit: KnowledgeUnit,
        *,
        reason: str,
        expires_at: str,
        enterprise_id: str,
        group_id: str,
    ) -> None:
        """Insert a KU and immediately move it into pending_review state.

        Two writes in one transaction: the standard INSERT (re-uses the
        ``_insert_sync`` body for tier=private + tenancy + domain rows),
        then an UPDATE that flips status to pending_review and stamps
        the reason + expires_at columns.
        """
        # Re-use the canonical insert path so domain rows / quality
        # guards / tenancy fields all flow through the same sync method.
        self._insert_sync(unit, enterprise_id=enterprise_id, group_id=group_id)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE knowledge_units SET "
                    "status = 'pending_review', "
                    "pending_review_reason = :reason, "
                    "pending_review_expires_at = :expires_at "
                    "WHERE id = :id"
                ),
                {"reason": reason, "expires_at": expires_at, "id": unit.id},
            )

    def _list_pending_review_sync(
        self,
        *,
        enterprise_id: str,
        limit: int = 20,
        offset: int = 0,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List pending-review rows that have NOT yet hit their TTL.

        Read-time TTL filter (#121 finding 2): the sweeper transitions
        ``pending_review → dropped`` eventually, but until it fires
        every read could surface stale candidates whose
        ``pending_review_expires_at`` has already passed. We add an
        ``expires_at > now`` guard inline so the response can never
        contain rows the operator should no longer see, regardless of
        sweeper timing. Rows where ``expires_at IS NULL`` continue to
        appear (defensive: a NULL TTL means "never expires", same shape
        as ``expire_pending_reviews``'s ``IS NOT NULL`` filter).

        ``ORDER BY pending_review_expires_at ASC`` is preserved so the
        admin still sees closest-to-expiring rows first within the
        non-expired set.
        """
        now_iso = datetime.now(UTC).isoformat()
        with self._engine.connect() as conn:
            if group_id is not None:
                rows = conn.exec_driver_sql(
                    "SELECT data, status, reviewed_by, reviewed_at, "
                    "pending_review_reason, pending_review_expires_at "
                    "FROM knowledge_units "
                    "WHERE status = 'pending_review' AND enterprise_id = ? AND group_id = ? "
                    "AND (pending_review_expires_at IS NULL "
                    "     OR pending_review_expires_at > ?) "
                    "ORDER BY pending_review_expires_at ASC "
                    "LIMIT ? OFFSET ?",
                    (enterprise_id, group_id, now_iso, limit, offset),
                ).fetchall()
            else:
                rows = conn.exec_driver_sql(
                    "SELECT data, status, reviewed_by, reviewed_at, "
                    "pending_review_reason, pending_review_expires_at "
                    "FROM knowledge_units "
                    "WHERE status = 'pending_review' AND enterprise_id = ? "
                    "AND (pending_review_expires_at IS NULL "
                    "     OR pending_review_expires_at > ?) "
                    "ORDER BY pending_review_expires_at ASC "
                    "LIMIT ? OFFSET ?",
                    (enterprise_id, now_iso, limit, offset),
                ).fetchall()
        return [
            {
                "knowledge_unit": KnowledgeUnit.model_validate_json(row[0]),
                "status": row[1] or "pending_review",
                "reviewed_by": row[2],
                "reviewed_at": row[3],
                "pending_review_reason": row[4],
                "pending_review_expires_at": row[5],
            }
            for row in rows
        ]

    def _count_pending_review_sync(self, *, enterprise_id: str, group_id: str | None = None) -> int:
        """Count pending-review rows whose TTL has not yet passed.

        Mirror of ``_list_pending_review_sync``'s read-time filter
        (#121 finding 2) — count and list must agree on which rows
        are visible, otherwise dashboard pagination breaks ("total: 5"
        with only 3 items rendered).
        """
        now_iso = datetime.now(UTC).isoformat()
        with self._engine.connect() as conn:
            if group_id is not None:
                return int(
                    conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM knowledge_units "
                        "WHERE status = 'pending_review' AND enterprise_id = ? AND group_id = ? "
                        "AND (pending_review_expires_at IS NULL "
                        "     OR pending_review_expires_at > ?)",
                        (enterprise_id, group_id, now_iso),
                    ).scalar()
                    or 0
                )
            return int(
                conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM knowledge_units "
                    "WHERE status = 'pending_review' AND enterprise_id = ? "
                    "AND (pending_review_expires_at IS NULL "
                    "     OR pending_review_expires_at > ?)",
                    (enterprise_id, now_iso),
                ).scalar()
                or 0
            )

    def _expire_pending_reviews_sync(self, *, enterprise_id: str, now_iso: str) -> list[str]:
        """Two-step sweep: SELECT the row ids first, then UPDATE.

        We return the ids so the caller can emit one ``review_resolve``
        activity-log row per drop. Doing it as a single UPDATE…
        RETURNING would be cleaner on PostgreSQL but SQLite's RETURNING
        landed in 3.35; we run on whatever the host has, so split the
        round-trip explicitly.
        """
        with self._engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT id FROM knowledge_units "
                "WHERE status = 'pending_review' AND enterprise_id = ? "
                "AND pending_review_expires_at IS NOT NULL "
                "AND pending_review_expires_at < ?",
                (enterprise_id, now_iso),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                # Mark as dropped + stamp reviewed_by sentinel so the
                # admin UI can render "TTL-expired" in the audit history.
                reviewed_at = datetime.now(UTC).isoformat()
                conn.exec_driver_sql(
                    f"UPDATE knowledge_units SET "
                    f"status = 'dropped', "
                    f"reviewed_by = 'ttl_expired_sweeper', "
                    f"reviewed_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (reviewed_at, *ids),
                )
        return ids

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
    # SQLAlchemy text() + named-binding versions of the SQL the runtime executes.
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
        to_l2_endpoints_json: str = "[]",
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
        now_iso: str | None = None,
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
        enterprise_id: str | None = None,
        status: str | None = None,
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
        public_key_ed25519: str | None = None,
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

    def _semantic_query_with_scope_sync(
        self,
        *,
        query_vec: list[float],
        limit: int,
        status: str,
    ) -> list[dict[str, Any]]:
        import numpy as np

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT data, embedding, enterprise_id, group_id, "
                    "cross_group_allowed FROM knowledge_units "
                    "WHERE status = :status AND embedding IS NOT NULL"
                ),
                {"status": status},
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
            scored.append(
                (
                    sim,
                    unit,
                    ent or "default-enterprise",
                    grp or "default-group",
                    int(xgroup or 0),
                )
            )
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

    def _delete_sync(
        self,
        unit_id: str | None = None,
        *,
        enterprise_id: str | None = None,
        group_id: str | None = None,
        **kwargs: Any,
    ) -> bool:
        if unit_id is None:
            unit_id = kwargs.get("unit_id")
        with self._engine.begin() as conn:
            if enterprise_id is not None and group_id is not None:
                row = conn.execute(
                    text("SELECT 1 FROM knowledge_units WHERE id = :id AND enterprise_id = :eid AND group_id = :gid"),
                    {"id": unit_id, "eid": enterprise_id, "gid": group_id},
                ).fetchone()
                if row is None:
                    return False
            elif enterprise_id is not None:
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

    # --- activity_log (#108) sync impls ----------------------------------

    def _append_activity_sync(
        self,
        *,
        activity_id: str,
        ts: str,
        tenant_enterprise: str,
        tenant_group: str | None,
        persona: str | None,
        human: str | None,
        event_type: str,
        payload: dict[str, Any] | None,
        result_summary: dict[str, Any] | None,
        thread_or_chain_id: str | None,
    ) -> None:
        from ._queries import INSERT_ACTIVITY_LOG

        payload_json = json.dumps(payload if payload is not None else {})
        result_json = None if result_summary is None else json.dumps(result_summary)
        with self._engine.begin() as conn:
            conn.execute(
                INSERT_ACTIVITY_LOG,
                {
                    "id": activity_id,
                    "ts": ts,
                    "tenant_enterprise": tenant_enterprise,
                    "tenant_group": tenant_group,
                    "persona": persona,
                    "human": human,
                    "event_type": event_type,
                    "payload": payload_json,
                    "result_summary": result_json,
                    "thread_or_chain_id": thread_or_chain_id,
                },
            )

    def _get_activity_retention_days_sync(self, *, enterprise_id: str) -> int:
        from ..activity import DEFAULT_RETENTION_DAYS
        from ._queries import SELECT_ACTIVITY_RETENTION_DAYS

        with self._engine.connect() as conn:
            row = conn.execute(
                SELECT_ACTIVITY_RETENTION_DAYS,
                {"enterprise_id": enterprise_id},
            ).fetchone()
        if row is None:
            return DEFAULT_RETENTION_DAYS
        return int(row[0])

    def _set_activity_retention_days_sync(self, *, enterprise_id: str, retention_days: int) -> None:
        from ._queries import UPSERT_ACTIVITY_RETENTION_DAYS

        updated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._engine.begin() as conn:
            conn.execute(
                UPSERT_ACTIVITY_RETENTION_DAYS,
                {
                    "enterprise_id": enterprise_id,
                    "retention_days": retention_days,
                    "updated_at": updated_at,
                },
            )

    def _purge_activity_older_than_sync(self, *, tenant_enterprise: str, cutoff_iso: str) -> int:
        from ._queries import DELETE_ACTIVITY_OLDER_THAN

        with self._engine.begin() as conn:
            cursor = conn.execute(
                DELETE_ACTIVITY_OLDER_THAN,
                {
                    "tenant_enterprise": tenant_enterprise,
                    "cutoff_iso": cutoff_iso,
                },
            )
            return int(cursor.rowcount or 0)

    def _list_activity_sync(
        self,
        *,
        tenant_enterprise: str,
        tenant_group: str | None = None,
        persona: str | None,
        since_iso: str | None,
        until_iso: str | None,
        event_type: str | None,
        limit: int,
        cursor: tuple[str, str] | None,
    ) -> list[dict[str, Any]]:
        """Build the WHERE clause dynamically based on which filters are set.

        The base index is ``idx_activity_log_tenant_ts (tenant_enterprise,
        tenant_group, ts)``; the leading column pin makes every filter
        combination index-friendly. Persona filter switches over to
        ``idx_activity_log_persona_ts`` when set. Event-type filter
        switches over to ``idx_activity_log_event_type_ts``.

        Cursor: when set, restrict to rows strictly before ``(ts, id)``
        on the descending order — equivalent to "give me the next page
        beneath the last row of the previous page". The id tie-breaker
        is necessary because two rows can share a ts (sub-millisecond
        background-task scheduling).
        """
        clauses = ["tenant_enterprise = :tenant_enterprise"]
        params: dict[str, Any] = {"tenant_enterprise": tenant_enterprise, "limit": limit}
        if tenant_group is not None:
            # Decision 27: composite tenancy filter on the activity log
            # read path. The base index ``idx_activity_log_tenant_ts``
            # already covers ``(tenant_enterprise, tenant_group, ts)``
            # so this clause is index-friendly.
            clauses.append("tenant_group = :tenant_group")
            params["tenant_group"] = tenant_group
        if persona is not None:
            clauses.append("persona = :persona")
            params["persona"] = persona
        if since_iso is not None:
            clauses.append("ts >= :since_iso")
            params["since_iso"] = since_iso
        if until_iso is not None:
            clauses.append("ts < :until_iso")
            params["until_iso"] = until_iso
        if event_type is not None:
            clauses.append("event_type = :event_type")
            params["event_type"] = event_type
        if cursor is not None:
            cursor_ts, cursor_id = cursor
            # (ts, id) < (cursor_ts, cursor_id) tuple comparison — split
            # into the lexicographic SQL form so SQLite's planner can
            # use the indexes correctly.
            clauses.append("(ts < :cursor_ts OR (ts = :cursor_ts AND id < :cursor_id))")
            params["cursor_ts"] = cursor_ts
            params["cursor_id"] = cursor_id

        sql = (
            "SELECT id, ts, tenant_enterprise, tenant_group, persona, human, "
            "event_type, payload, result_summary, thread_or_chain_id "
            "FROM activity_log "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC, id DESC "
            "LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload_raw = row[7]
            result_raw = row[8]
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except (TypeError, ValueError):
                payload = {}
            try:
                result_summary = json.loads(result_raw) if result_raw is not None else None
            except (TypeError, ValueError):
                result_summary = None
            results.append(
                {
                    "id": row[0],
                    "ts": row[1],
                    "tenant_enterprise": row[2],
                    "tenant_group": row[3],
                    "persona": row[4],
                    "human": row[5],
                    "event_type": row[6],
                    "payload": payload,
                    "result_summary": result_summary,
                    "thread_or_chain_id": row[9],
                }
            )
        return results

    # --- crosstalk_threads + crosstalk_messages (#124) sync impls --------

    def _create_crosstalk_thread_sync(
        self,
        *,
        thread_id: str,
        subject: str,
        enterprise_id: str,
        group_id: str,
        created_at: str,
        created_by_username: str,
        participants: list[str],
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO crosstalk_threads "
                    "(id, subject, status, enterprise_id, group_id, created_at, "
                    " created_by_username, participants) "
                    "VALUES (:id, :subject, 'open', :enterprise_id, :group_id, "
                    "        :created_at, :created_by_username, :participants)"
                ),
                {
                    "id": thread_id,
                    "subject": subject,
                    "enterprise_id": enterprise_id,
                    "group_id": group_id,
                    "created_at": created_at,
                    "created_by_username": created_by_username,
                    "participants": json.dumps(participants),
                },
            )

    def _append_crosstalk_message_sync(
        self,
        *,
        message_id: str,
        thread_id: str,
        from_username: str,
        from_persona: str | None,
        to_username: str | None,
        content: str,
        sent_at: str,
        enterprise_id: str,
        group_id: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO crosstalk_messages "
                    "(id, thread_id, from_username, from_persona, to_username, "
                    " content, sent_at, enterprise_id, group_id) "
                    "VALUES (:id, :thread_id, :from_username, :from_persona, "
                    "        :to_username, :content, :sent_at, :enterprise_id, "
                    "        :group_id)"
                ),
                {
                    "id": message_id,
                    "thread_id": thread_id,
                    "from_username": from_username,
                    "from_persona": from_persona,
                    "to_username": to_username,
                    "content": content,
                    "sent_at": sent_at,
                    "enterprise_id": enterprise_id,
                    "group_id": group_id,
                },
            )

    def _get_crosstalk_thread_sync(
        self,
        *,
        thread_id: str,
        tenant_enterprise: str,
        tenant_group: str | None = None,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            if tenant_group is not None:
                row = conn.execute(
                    text(
                        "SELECT id, subject, status, closed_at, closed_by_username, "
                        "       closed_reason, enterprise_id, group_id, created_at, "
                        "       created_by_username, participants "
                        "FROM crosstalk_threads "
                        "WHERE id = :id AND enterprise_id = :tenant "
                        "  AND group_id = :tgroup"
                    ),
                    {"id": thread_id, "tenant": tenant_enterprise, "tgroup": tenant_group},
                ).fetchone()
            else:
                row = conn.execute(
                    text(
                        "SELECT id, subject, status, closed_at, closed_by_username, "
                        "       closed_reason, enterprise_id, group_id, created_at, "
                        "       created_by_username, participants "
                        "FROM crosstalk_threads "
                        "WHERE id = :id AND enterprise_id = :tenant"
                    ),
                    {"id": thread_id, "tenant": tenant_enterprise},
                ).fetchone()
        if row is None:
            return None
        try:
            participants = json.loads(row[10]) if row[10] else []
        except (TypeError, ValueError):
            participants = []
        return {
            "id": row[0],
            "subject": row[1],
            "status": row[2],
            "closed_at": row[3],
            "closed_by_username": row[4],
            "closed_reason": row[5],
            "enterprise_id": row[6],
            "group_id": row[7],
            "created_at": row[8],
            "created_by_username": row[9],
            "participants": participants,
        }

    def _list_crosstalk_messages_sync(
        self,
        *,
        thread_id: str,
        tenant_enterprise: str,
        limit: int,
        tenant_group: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            if tenant_group is not None:
                rows = conn.execute(
                    text(
                        "SELECT id, thread_id, from_username, from_persona, "
                        "       to_username, content, sent_at, read_at "
                        "FROM crosstalk_messages "
                        "WHERE thread_id = :thread AND enterprise_id = :tenant "
                        "  AND group_id = :tgroup "
                        "ORDER BY sent_at ASC, id ASC "
                        "LIMIT :limit"
                    ),
                    {
                        "thread": thread_id,
                        "tenant": tenant_enterprise,
                        "tgroup": tenant_group,
                        "limit": limit,
                    },
                ).fetchall()
            else:
                rows = conn.execute(
                    text(
                        "SELECT id, thread_id, from_username, from_persona, "
                        "       to_username, content, sent_at, read_at "
                        "FROM crosstalk_messages "
                        "WHERE thread_id = :thread AND enterprise_id = :tenant "
                        "ORDER BY sent_at ASC, id ASC "
                        "LIMIT :limit"
                    ),
                    {
                        "thread": thread_id,
                        "tenant": tenant_enterprise,
                        "limit": limit,
                    },
                ).fetchall()
        return [
            {
                "id": r[0],
                "thread_id": r[1],
                "from_username": r[2],
                "from_persona": r[3],
                "to_username": r[4],
                "content": r[5],
                "sent_at": r[6],
                "read_at": r[7],
            }
            for r in rows
        ]

    def _list_crosstalk_threads_for_user_sync(
        self,
        *,
        username: str,
        tenant_enterprise: str,
        is_admin: bool,
        limit: int,
        tenant_group: str | None = None,
    ) -> list[dict[str, Any]]:
        # Admin: all threads in tenant. Non-admin: threads where username
        # is in the participants JSON list. SQLite has no JSON_CONTAINS,
        # so we string-match the JSON-quoted form. The participants
        # column is bounded JSON ([]-list of usernames) and usernames
        # contain only safe characters; the false-positive surface is
        # acceptable for V1 and will tighten when we move to PostgreSQL
        # JSONB.
        #
        # Decision 27: when ``tenant_group`` is set, the WHERE clause
        # additionally pins ``group_id`` so two L2s under one Enterprise
        # see disjoint thread lists (admin role does NOT escape the
        # tenancy boundary — admin oversight is per-L2 by design).
        group_clause = " AND group_id = :tgroup" if tenant_group is not None else ""
        if is_admin:
            sql = (
                "SELECT id, subject, status, created_at, created_by_username, "
                "       participants "
                "FROM crosstalk_threads "
                f"WHERE enterprise_id = :tenant{group_clause} "
                "ORDER BY created_at DESC "
                "LIMIT :limit"
            )
            params: dict[str, Any] = {"tenant": tenant_enterprise, "limit": limit}
        else:
            sql = (
                "SELECT id, subject, status, created_at, created_by_username, "
                "       participants "
                "FROM crosstalk_threads "
                f"WHERE enterprise_id = :tenant{group_clause} "
                "  AND participants LIKE :ptn "
                "ORDER BY created_at DESC "
                "LIMIT :limit"
            )
            params = {
                "tenant": tenant_enterprise,
                "ptn": f'%"{username}"%',
                "limit": limit,
            }
        if tenant_group is not None:
            params["tgroup"] = tenant_group
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            try:
                participants = json.loads(r[5]) if r[5] else []
            except (TypeError, ValueError):
                participants = []
            results.append(
                {
                    "id": r[0],
                    "subject": r[1],
                    "status": r[2],
                    "created_at": r[3],
                    "created_by_username": r[4],
                    "participants": participants,
                }
            )
        return results

    def _close_crosstalk_thread_sync(
        self,
        *,
        thread_id: str,
        closed_by_username: str,
        closed_at: str,
        reason: str | None,
        tenant_enterprise: str,
        tenant_group: str | None = None,
    ) -> bool:
        # Optimistic concurrency: only flip if status is currently 'open'.
        # Pattern matches set_review_status (#121 finding 1) — second
        # closer races and gets False rather than overwriting fields.
        params = {
            "id": thread_id,
            "tenant": tenant_enterprise,
            "closed_at": closed_at,
            "closed_by": closed_by_username,
            "reason": reason,
        }
        if tenant_group is not None:
            sql = (
                "UPDATE crosstalk_threads "
                "SET status = 'closed', "
                "    closed_at = :closed_at, "
                "    closed_by_username = :closed_by, "
                "    closed_reason = :reason "
                "WHERE id = :id "
                "  AND enterprise_id = :tenant "
                "  AND group_id = :tgroup "
                "  AND status = 'open'"
            )
            params["tgroup"] = tenant_group
        else:
            sql = (
                "UPDATE crosstalk_threads "
                "SET status = 'closed', "
                "    closed_at = :closed_at, "
                "    closed_by_username = :closed_by, "
                "    closed_reason = :reason "
                "WHERE id = :id "
                "  AND enterprise_id = :tenant "
                "  AND status = 'open'"
            )
        with self._engine.begin() as conn:
            cursor = conn.execute(text(sql), params)
            return bool(cursor.rowcount)

    def _crosstalk_inbox_for_user_sync(
        self,
        *,
        username: str,
        tenant_enterprise: str,
        limit: int,
        mark_read: bool,
        read_at_iso: str | None,
        tenant_group: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._engine.begin() as conn:
            if tenant_group is not None:
                rows = conn.execute(
                    text(
                        "SELECT id, thread_id, from_username, from_persona, "
                        "       to_username, content, sent_at "
                        "FROM crosstalk_messages "
                        "WHERE to_username = :username "
                        "  AND enterprise_id = :tenant "
                        "  AND group_id = :tgroup "
                        "  AND read_at IS NULL "
                        "ORDER BY sent_at ASC "
                        "LIMIT :limit"
                    ),
                    {
                        "username": username,
                        "tenant": tenant_enterprise,
                        "tgroup": tenant_group,
                        "limit": limit,
                    },
                ).fetchall()
            else:
                rows = conn.execute(
                    text(
                        "SELECT id, thread_id, from_username, from_persona, "
                        "       to_username, content, sent_at "
                        "FROM crosstalk_messages "
                        "WHERE to_username = :username "
                        "  AND enterprise_id = :tenant "
                        "  AND read_at IS NULL "
                        "ORDER BY sent_at ASC "
                        "LIMIT :limit"
                    ),
                    {
                        "username": username,
                        "tenant": tenant_enterprise,
                        "limit": limit,
                    },
                ).fetchall()
            ids = [r[0] for r in rows]
            if mark_read and ids and read_at_iso is not None:
                conn.execute(
                    text(
                        "UPDATE crosstalk_messages "
                        "SET read_at = :read_at "
                        "WHERE id IN (" + ",".join(f":id{i}" for i in range(len(ids))) + ")"
                    ),
                    {"read_at": read_at_iso, **{f"id{i}": ids[i] for i in range(len(ids))}},
                )
        return [
            {
                "id": r[0],
                "thread_id": r[1],
                "from_username": r[2],
                "from_persona": r[3],
                "to_username": r[4],
                "content": r[5],
                "sent_at": r[6],
            }
            for r in rows
        ]

    # --- reflect_submissions (#67) sync impls ----------------------------

    _REFLECT_COLUMNS = (
        "id, session_id, user_id, enterprise_id, group_id, context_hash, "
        "state, anthropic_batch_id, model, input_tokens, output_tokens, "
        "candidates_proposed, candidates_confirmed, candidates_excluded, "
        "candidates_deduped, error, mode, max_candidates, since_ts, "
        "submitted_at, started_at, completed_at"
    )

    @staticmethod
    def _reflect_row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "user_id": row[2],
            "enterprise_id": row[3],
            "group_id": row[4],
            "context_hash": row[5],
            "state": row[6],
            "anthropic_batch_id": row[7],
            "model": row[8],
            "input_tokens": row[9],
            "output_tokens": row[10],
            "candidates_proposed": int(row[11]),
            "candidates_confirmed": int(row[12]),
            "candidates_excluded": int(row[13]),
            "candidates_deduped": int(row[14]),
            "error": row[15],
            "mode": row[16],
            "max_candidates": int(row[17]) if row[17] is not None else 10,
            "since_ts": row[18],
            "submitted_at": row[19],
            "started_at": row[20],
            "completed_at": row[21],
        }

    def _create_reflect_submission_sync(
        self,
        *,
        submission_id: str,
        session_id: str,
        user_id: int,
        enterprise_id: str,
        group_id: str | None,
        context_hash: str,
        mode: str,
        max_candidates: int,
        since_ts: str | None,
        submitted_at: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO reflect_submissions "
                    "(id, session_id, user_id, enterprise_id, group_id, "
                    " context_hash, state, mode, max_candidates, since_ts, "
                    " submitted_at, candidates_proposed, candidates_confirmed, "
                    " candidates_excluded, candidates_deduped) "
                    "VALUES (:id, :session_id, :user_id, :enterprise_id, :group_id, "
                    " :context_hash, 'queued', :mode, :max_candidates, :since_ts, "
                    " :submitted_at, 0, 0, 0, 0)"
                ),
                {
                    "id": submission_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "enterprise_id": enterprise_id,
                    "group_id": group_id,
                    "context_hash": context_hash,
                    "mode": mode,
                    "max_candidates": max_candidates,
                    "since_ts": since_ts,
                    "submitted_at": submitted_at,
                },
            )

    def _get_reflect_submission_sync(self, submission_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {self._REFLECT_COLUMNS} FROM reflect_submissions WHERE id = :id"),
                {"id": submission_id},
            ).fetchone()
        return None if row is None else self._reflect_row_to_dict(row)

    def _find_recent_reflect_dedup_sync(
        self,
        *,
        session_id: str,
        context_hash: str,
        window_start_iso: str,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {self._REFLECT_COLUMNS} FROM reflect_submissions "
                    "WHERE session_id = :sid AND context_hash = :ch "
                    "AND submitted_at >= :since "
                    "ORDER BY submitted_at DESC LIMIT 1"
                ),
                {"sid": session_id, "ch": context_hash, "since": window_start_iso},
            ).fetchone()
        return None if row is None else self._reflect_row_to_dict(row)

    def _count_recent_reflect_submissions_sync(
        self,
        *,
        session_id: str,
        window_start_iso: str,
    ) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM reflect_submissions WHERE session_id = :sid AND submitted_at >= :since"),
                {"sid": session_id, "since": window_start_iso},
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _get_oldest_reflect_in_window_sync(
        self,
        *,
        session_id: str,
        window_start_iso: str,
    ) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT submitted_at FROM reflect_submissions "
                    "WHERE session_id = :sid AND submitted_at >= :since "
                    "ORDER BY submitted_at ASC LIMIT 1"
                ),
                {"sid": session_id, "since": window_start_iso},
            ).fetchone()
        return row[0] if row is not None else None

    def _get_last_reflect_for_session_sync(
        self,
        *,
        session_id: str,
        enterprise_id: str,
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {self._REFLECT_COLUMNS} FROM reflect_submissions "
                    "WHERE session_id = :sid AND enterprise_id = :eid "
                    "ORDER BY submitted_at DESC LIMIT 1"
                ),
                {"sid": session_id, "eid": enterprise_id},
            ).fetchone()
        return None if row is None else self._reflect_row_to_dict(row)

    def _list_reflect_submissions_for_recovery_sync(self, *, lookback_iso: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {self._REFLECT_COLUMNS} FROM reflect_submissions "
                    "WHERE state IN ('queued','batching','polling') "
                    "AND anthropic_batch_id IS NOT NULL "
                    "AND submitted_at >= :since "
                    "ORDER BY submitted_at ASC"
                ),
                {"since": lookback_iso},
            ).fetchall()
        return [self._reflect_row_to_dict(r) for r in rows]

    # --- WebAuthn / passkey sync impls (FO-1a, #191) ----------------------

    def _insert_webauthn_credential_sync(
        self,
        *,
        user_id: int,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: str | None,
        aaguid: bytes | None,
        name: str | None,
    ) -> dict[str, Any]:
        created_at = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "INSERT INTO webauthn_credentials "
                    "(user_id, credential_id, public_key, sign_count, transports, aaguid, name, created_at) "
                    "VALUES (:uid, :cid, :pk, :sc, :tr, :ag, :nm, :ca)"
                ),
                {
                    "uid": user_id,
                    "cid": credential_id,
                    "pk": public_key,
                    "sc": sign_count,
                    "tr": transports,
                    "ag": aaguid,
                    "nm": name,
                    "ca": created_at,
                },
            )
            row_id = result.lastrowid
        return {
            "id": row_id,
            "user_id": user_id,
            "credential_id": credential_id,
            "public_key": public_key,
            "sign_count": sign_count,
            "transports": transports,
            "aaguid": aaguid,
            "name": name,
            "created_at": created_at,
            "last_used_at": None,
        }

    def _list_webauthn_credentials_for_user_sync(self, user_id: int) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, user_id, credential_id, public_key, sign_count, transports, "
                    "aaguid, name, created_at, last_used_at "
                    "FROM webauthn_credentials WHERE user_id = :uid ORDER BY id ASC"
                ),
                {"uid": user_id},
            ).fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "credential_id": bytes(r[2]),
                "public_key": bytes(r[3]),
                "sign_count": r[4],
                "transports": r[5],
                "aaguid": bytes(r[6]) if r[6] is not None else None,
                "name": r[7],
                "created_at": r[8],
                "last_used_at": r[9],
            }
            for r in rows
        ]

    def _get_webauthn_credential_by_id_sync(self, credential_id: bytes) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, user_id, credential_id, public_key, sign_count, transports, "
                    "aaguid, name, created_at, last_used_at "
                    "FROM webauthn_credentials WHERE credential_id = :cid"
                ),
                {"cid": credential_id},
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "credential_id": bytes(row[2]),
            "public_key": bytes(row[3]),
            "sign_count": row[4],
            "transports": row[5],
            "aaguid": bytes(row[6]) if row[6] is not None else None,
            "name": row[7],
            "created_at": row[8],
            "last_used_at": row[9],
        }

    def _update_webauthn_sign_count_sync(self, *, credential_id: bytes, new_sign_count: int, last_used_at: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE webauthn_credentials SET sign_count = :sc, last_used_at = :lu WHERE credential_id = :cid"),
                {"sc": new_sign_count, "lu": last_used_at, "cid": credential_id},
            )

    def _get_user_by_email_sync(self, email: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, username, password_hash, created_at, role, "
                    "enterprise_id, group_id, email FROM users WHERE email = :e"
                ),
                {"e": email},
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
            "role": row[4],
            "enterprise_id": row[5],
            "group_id": row[6],
            "email": row[7],
        }

    # ------------------------------------------------------------------
    # AS-1 (#200) — persona_assignments CRUD
    # ------------------------------------------------------------------

    async def list_persona_assignments(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """Return paginated persona assignments joined with user email."""
        return await asyncio.get_event_loop().run_in_executor(None, self._list_persona_assignments_sync, limit, offset)

    def _list_persona_assignments_sync(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with self._engine.connect() as conn:
            total_row = conn.execute(text("SELECT COUNT(*) FROM persona_assignments")).fetchone()
            total = total_row[0] if total_row else 0
            rows = conn.execute(
                text(
                    """
                    SELECT pa.username, u.email, pa.persona,
                           pa.assigned_at, pa.assigned_by, pa.disabled_at
                    FROM persona_assignments pa
                    LEFT JOIN users u ON pa.username = u.username
                    ORDER BY pa.assigned_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"limit": limit, "offset": offset},
            ).fetchall()
        items = [
            {
                "username": r[0],
                "email": r[1],
                "persona": r[2],
                "assigned_at": r[3],
                "assigned_by": r[4],
                "disabled_at": r[5],
            }
            for r in rows
        ]
        return items, total

    async def get_persona_assignment(self, username: str) -> dict | None:
        """Return the persona assignment for a user, or None."""
        return await asyncio.get_event_loop().run_in_executor(None, self._get_persona_assignment_sync, username)

    def _get_persona_assignment_sync(self, username: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT pa.username, u.email, pa.persona,
                           pa.assigned_at, pa.assigned_by, pa.disabled_at
                    FROM persona_assignments pa
                    LEFT JOIN users u ON pa.username = u.username
                    WHERE pa.username = :username
                    """
                ),
                {"username": username},
            ).fetchone()
        if row is None:
            return None
        return {
            "username": row[0],
            "email": row[1],
            "persona": row[2],
            "assigned_at": row[3],
            "assigned_by": row[4],
            "disabled_at": row[5],
        }

    async def upsert_persona_assignment(
        self,
        username: str,
        persona: str,
        assigned_at: str,
        assigned_by: str,
        *,
        audit_action: str | None = None,
        audit_old_persona: str | None = None,
    ) -> dict | None:
        """Create or update a persona assignment (clears disabled_at).

        When ``audit_action`` is provided (CREATED/CHANGED/ENABLED) the
        store writes a row into ``persona_assignment_audit`` in the same
        transaction. Callers that don't pass it get the legacy behaviour
        (no audit row) for backward compatibility with seed-style tests.
        """
        import functools

        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(
                self._upsert_persona_assignment_sync,
                username,
                persona,
                assigned_at,
                assigned_by,
                audit_action=audit_action,
                audit_old_persona=audit_old_persona,
            ),
        )

    def _upsert_persona_assignment_sync(
        self,
        username: str,
        persona: str,
        assigned_at: str,
        assigned_by: str,
        *,
        audit_action: str | None = None,
        audit_old_persona: str | None = None,
    ) -> dict | None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO persona_assignments
                        (username, persona, assigned_at, assigned_by, disabled_at)
                    VALUES (:u, :persona, :assigned_at, :assigned_by, NULL)
                    ON CONFLICT(username) DO UPDATE SET
                        persona = excluded.persona,
                        assigned_at = excluded.assigned_at,
                        assigned_by = excluded.assigned_by,
                        disabled_at = NULL
                    """
                ),
                {
                    "u": username,
                    "persona": persona,
                    "assigned_at": assigned_at,
                    "assigned_by": assigned_by,
                },
            )
            # AS-1 follow-up (H-2): atomic audit-row write.
            if audit_action is not None:
                conn.execute(
                    text(
                        """
                        INSERT INTO persona_assignment_audit
                            (username, old_persona, new_persona, changed_by, action)
                        VALUES (:u, :old, :new, :by, :action)
                        """
                    ),
                    {
                        "u": username,
                        "old": audit_old_persona,
                        "new": persona,
                        "by": assigned_by,
                        "action": audit_action,
                    },
                )
        return self._get_persona_assignment_sync(username)

    async def disable_persona_assignment(
        self,
        username: str,
        disabled_at: str,
        *,
        changed_by: str | None = None,
        old_persona: str | None = None,
    ) -> dict | None:
        """Soft-disable a persona assignment (set disabled_at timestamp).

        When ``changed_by`` is provided, also writes a DISABLED audit row.
        """
        import functools

        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(
                self._disable_persona_assignment_sync,
                username,
                disabled_at,
                changed_by=changed_by,
                old_persona=old_persona,
            ),
        )

    def _disable_persona_assignment_sync(
        self,
        username: str,
        disabled_at: str,
        *,
        changed_by: str | None = None,
        old_persona: str | None = None,
    ) -> dict | None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE persona_assignments
                    SET disabled_at = :disabled_at
                    WHERE username = :username
                    """
                ),
                {"username": username, "disabled_at": disabled_at},
            )
            if changed_by is not None:
                conn.execute(
                    text(
                        """
                        INSERT INTO persona_assignment_audit
                            (username, old_persona, new_persona, changed_by, action)
                        VALUES (:u, :old, NULL, :by, 'DISABLED')
                        """
                    ),
                    {"u": username, "old": old_persona, "by": changed_by},
                )
        return self._get_persona_assignment_sync(username)

    # ------------------------------------------------------------------
    # AS-1 follow-up (H-3, M-5) — admin guards + invite rate-limit
    # ------------------------------------------------------------------

    async def count_active_admins(self) -> int:
        """Return count of active admin persona assignments.

        Counts rows where persona='admin' and disabled_at IS NULL. Used
        by the last-admin guard on the disable endpoint.
        """
        return await asyncio.get_event_loop().run_in_executor(None, self._count_active_admins_sync)

    def _count_active_admins_sync(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM persona_assignments
                    WHERE persona = 'admin' AND disabled_at IS NULL
                    """
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def count_invites_by_admin(self, admin_username: str, since: str) -> int:
        """Count persona assignments minted by this admin since a timestamp.

        Used as a proxy for invite-rate-limit accounting on the persona
        create endpoint (M-5).
        """
        return await asyncio.get_event_loop().run_in_executor(
            None, self._count_invites_by_admin_sync, admin_username, since
        )

    def _count_invites_by_admin_sync(self, admin_username: str, since: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM persona_assignments
                    WHERE assigned_by = :admin AND assigned_at >= :since
                    """
                ),
                {"admin": admin_username, "since": since},
            ).fetchone()
        return int(row[0]) if row else 0

    async def list_persona_audit(self, username: str) -> list[dict]:
        """Return audit rows for a username, oldest-first (test helper)."""
        return await asyncio.get_event_loop().run_in_executor(None, self._list_persona_audit_sync, username)

    def _list_persona_audit_sync(self, username: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT username, old_persona, new_persona, changed_by,
                           changed_at, action
                    FROM persona_assignment_audit
                    WHERE username = :u
                    ORDER BY id ASC
                    """
                ),
                {"u": username},
            ).fetchall()
        return [
            {
                "username": r[0],
                "old_persona": r[1],
                "new_persona": r[2],
                "changed_by": r[3],
                "changed_at": r[4],
                "action": r[5],
            }
            for r in rows
        ]

    async def set_user_email(self, username: str, email: str) -> None:
        """Update the email on a users row (used when creating persona assignments)."""
        await asyncio.get_event_loop().run_in_executor(None, self._set_user_email_sync, username, email)

    def _set_user_email_sync(self, username: str, email: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET email = :email WHERE username = :username"),
                {"username": username, "email": email},
            )


class _SyncStoreProxy:
    """Sync method proxy over SqliteStore.

    Forwards each attribute access to the matching ``_<name>_sync``
    implementation on the wrapped store. ``close()`` and ``db_path``
    are surfaced explicitly because they don't follow the
    ``_method_sync`` naming convention.
    """

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        sync_attr = getattr(self._store, f"_{name}_sync", None)
        if sync_attr is not None:
            return sync_attr
        # Fall back to direct attribute (e.g. internal helpers).
        return getattr(self._store, name)

    @property
    def db_path(self) -> Path:
        return self._store._db_path

    def close(self) -> None:
        self._store.close_sync()
