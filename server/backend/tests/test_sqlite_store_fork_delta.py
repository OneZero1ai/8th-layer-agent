"""Tests for fork-delta methods ported to async SqliteStore (#105 PR-A).

Covers the first slice: directory peerings + AIGRP peers. The full
collapse (cutover of ``app.state.store`` from SqliteStore → SqliteStore,
remaining method ports, ensure_* deletion, ruff cleanup) lands across
PR-B/C/D per the plan in
``crosstalk-enterprise/docs/plans/14-alembic-phase-2b-remotestore-collapse.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from cq_server.migrations import run_migrations
from cq_server.store._sqlite import SqliteStore


@pytest_asyncio.fixture()
async def store(tmp_path: Path):
    db = tmp_path / "test.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    await s.close()


class TestDirectoryPeerings:
    @pytest.mark.asyncio
    async def test_upsert_then_find_active(self, store: SqliteStore) -> None:
        store.sync.upsert_directory_peering(
            offer_id="ofr_1",
            from_enterprise="acme",
            to_enterprise="globex",
            status="active",
            content_policy="summary_only",
            consult_logging_policy="mutual_log_required",
            topic_filters_json="[]",
            active_from="2026-01-01T00:00:00Z",
            expires_at="2027-01-01T00:00:00Z",
            offer_payload_canonical='{"offer":1}',
            offer_signature_b64u="OFFER_SIG",
            offer_signing_key_id="ACME_KEY",
            accept_payload_canonical='{"accept":1}',
            accept_signature_b64u="ACCEPT_SIG",
            accept_signing_key_id="GLOBEX_KEY",
            last_synced_at="2026-05-03T00:00:00Z",
        )
        # Bidirectional lookup
        a_to_b = store.sync.find_active_directory_peering(from_enterprise="acme", to_enterprise="globex")
        b_to_a = store.sync.find_active_directory_peering(from_enterprise="globex", to_enterprise="acme")
        assert a_to_b is not None
        assert b_to_a is not None
        assert a_to_b["offer_id"] == "ofr_1"

    @pytest.mark.asyncio
    async def test_expired_peering_not_returned(self, store: SqliteStore) -> None:
        store.sync.upsert_directory_peering(
            offer_id="ofr_old",
            from_enterprise="acme",
            to_enterprise="globex",
            status="active",
            content_policy="summary_only",
            consult_logging_policy="mutual_log_required",
            topic_filters_json="[]",
            active_from="2020-01-01T00:00:00Z",
            expires_at="2021-01-01T00:00:00Z",  # already expired
            offer_payload_canonical="{}",
            offer_signature_b64u="X",
            offer_signing_key_id="X",
            accept_payload_canonical="{}",
            accept_signature_b64u="X",
            accept_signing_key_id="X",
            last_synced_at="2026-05-03T00:00:00Z",
        )
        result = store.sync.find_active_directory_peering(from_enterprise="acme", to_enterprise="globex")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_with_filters(self, store: SqliteStore) -> None:
        for offer_id, status in [("a", "active"), ("p", "pending"), ("a2", "active")]:
            store.sync.upsert_directory_peering(
                offer_id=offer_id,
                from_enterprise="acme",
                to_enterprise="globex",
                status=status,
                content_policy="summary_only",
                consult_logging_policy="mutual_log_required",
                topic_filters_json="[]",
                active_from="2026-01-01T00:00:00Z",
                expires_at="2027-01-01T00:00:00Z",
                offer_payload_canonical="{}",
                offer_signature_b64u="X",
                offer_signing_key_id="X",
                accept_payload_canonical="{}",
                accept_signature_b64u="X",
                accept_signing_key_id="X",
                last_synced_at="2026-05-03T00:00:00Z",
            )
        active = store.sync.list_directory_peerings(status="active")
        assert len(active) == 2
        all_acme = store.sync.list_directory_peerings(enterprise_id="acme")
        assert len(all_acme) == 3


class TestAigrpPeers:
    @pytest.mark.asyncio
    async def test_upsert_first_signature(self, store: SqliteStore) -> None:
        store.sync.upsert_aigrp_peer(
            l2_id="acme/eng",
            enterprise="acme",
            group="engineering",
            endpoint_url="https://acme.example/eng",
            embedding_centroid=b"\x00" * 32,
            domain_bloom=b"\x00" * 16,
            ku_count=10,
            domain_count=3,
            embedding_model="test",
            signature_received=True,
            public_key_ed25519="ACME_PK",
        )
        peers = store.sync.list_aigrp_peers("acme")
        assert len(peers) == 1
        peer = peers[0]
        assert peer["l2_id"] == "acme/eng"
        assert peer["public_key_ed25519"] == "ACME_PK"
        assert peer["last_signature_at"] is not None  # signature_received=True

    @pytest.mark.asyncio
    async def test_unsigned_upsert_keeps_existing_signature(self, store: SqliteStore) -> None:
        # First upsert with signature
        store.sync.upsert_aigrp_peer(
            l2_id="acme/eng",
            enterprise="acme",
            group="engineering",
            endpoint_url="https://acme.example/eng",
            embedding_centroid=b"\xff" * 32,
            domain_bloom=b"\xff" * 16,
            ku_count=10,
            domain_count=3,
            embedding_model="test",
            signature_received=True,
            public_key_ed25519="ACME_PK",
        )
        first = (store.sync.list_aigrp_peers("acme"))[0]

        # Second upsert without signature (just /aigrp/hello refresh) — last_signature_at stays
        store.sync.upsert_aigrp_peer(
            l2_id="acme/eng",
            enterprise="acme",
            group="engineering",
            endpoint_url="https://acme.example/eng-v2",
            embedding_centroid=None,
            domain_bloom=None,
            ku_count=0,
            domain_count=0,
            embedding_model=None,
            signature_received=False,
        )
        second = (store.sync.list_aigrp_peers("acme"))[0]
        assert second["last_signature_at"] == first["last_signature_at"]
        # endpoint_url did update + last_seen advanced
        assert second["endpoint_url"] == "https://acme.example/eng-v2"

    @pytest.mark.asyncio
    async def test_get_pubkey_returns_none_for_unknown(self, store: SqliteStore) -> None:
        assert store.sync.get_aigrp_peer_pubkey("ghost/eng") is None
