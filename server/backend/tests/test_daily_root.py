"""Tests for daily Merkle root computation (task #108 sub-task 3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cq_server import reputation
from cq_server.daily_root import compute_root_for_day
from cq_server.merkle import EMPTY_DAY_ROOT, merkle_root
from cq_server.migrations import run_migrations


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("CQ_ENTERPRISE", "test-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    db = tmp_path / "rep.db"
    run_migrations(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))
    yield conn
    conn.close()


class TestComputeRoot:
    def test_empty_day_returns_zero_event_root(
        self, conn: sqlite3.Connection
    ) -> None:
        # No events for the day — root should be the empty-day constant.
        result = compute_root_for_day(conn, "test-corp", "2026-01-01")
        conn.commit()
        assert result["event_count"] == 0
        assert result["merkle_root_hash"] == EMPTY_DAY_ROOT
        assert result["first_event_id"] is None
        assert result["last_event_id"] is None

    def test_root_matches_merkle_over_payload_hashes(
        self, conn: sqlite3.Connection
    ) -> None:
        # Write 3 events at known times (today's UTC date by default).
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        e1 = reputation.record_event(
            conn,
            event_type="consult.closed",
            body={"i": 1},
            ts=f"{today}T01:00:00Z",
        )
        e2 = reputation.record_event(
            conn,
            event_type="ku.event",
            body={"i": 2},
            ts=f"{today}T02:00:00Z",
        )
        e3 = reputation.record_event(
            conn,
            event_type="peer.heartbeat",
            body={"i": 3},
            ts=f"{today}T03:00:00Z",
        )
        conn.commit()
        assert e1 and e2 and e3

        # Read the payload hashes in the same ts-ASC order
        rows = conn.execute(
            "SELECT payload_hash FROM reputation_events "
            "WHERE enterprise_id = ? ORDER BY ts ASC, event_id ASC",
            ("test-corp",),
        ).fetchall()
        leaf_hashes = [r[0] for r in rows]
        expected_root = merkle_root(leaf_hashes)

        result = compute_root_for_day(conn, "test-corp", today)
        conn.commit()
        assert result["merkle_root_hash"] == expected_root
        assert result["event_count"] == 3
        assert result["first_event_id"] == e1
        assert result["last_event_id"] == e3

    def test_idempotent_recompute_returns_existing(
        self, conn: sqlite3.Connection
    ) -> None:
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        reputation.record_event(
            conn,
            event_type="consult.closed",
            body={"i": 1},
            ts=f"{today}T01:00:00Z",
        )
        conn.commit()

        first = compute_root_for_day(conn, "test-corp", today)
        conn.commit()
        second = compute_root_for_day(conn, "test-corp", today)
        conn.commit()
        assert first["computed_at"] == second["computed_at"]  # not recomputed
        assert first["merkle_root_hash"] == second["merkle_root_hash"]

        # Only one row should exist for that (enterprise, day).
        n = conn.execute(
            "SELECT COUNT(*) FROM reputation_roots "
            "WHERE enterprise_id = ? AND root_date = ?",
            ("test-corp", today),
        ).fetchone()[0]
        assert n == 1

    def test_chain_meta_advances_after_compute(
        self, conn: sqlite3.Connection
    ) -> None:
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        reputation.record_event(
            conn, event_type="consult.closed", body={"i": 1},
        )
        conn.commit()

        compute_root_for_day(conn, "test-corp", today)
        conn.commit()

        last_day = conn.execute(
            "SELECT last_root_published_day FROM reputation_chain_meta "
            "WHERE enterprise_id = ?",
            ("test-corp",),
        ).fetchone()[0]
        assert last_day == today


class TestSignedRoot:
    def test_root_signature_when_key_available(
        self,
        conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the L2 forward-sign key is available, the daily root row
        carries a non-NULL signature over its canonical envelope."""
        from cq_server import forward_sign
        from cq_server.crypto import verify_raw
        from cq_server.reputation import canonical_payload_bytes

        key_path = tmp_path / "l2_key.bin"
        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(key_path))
        forward_sign.reload_l2_privkey()

        try:
            from datetime import UTC, datetime

            today = datetime.now(UTC).date().isoformat()
            reputation.record_event(
                conn, event_type="consult.closed", body={"i": 1}
            )
            conn.commit()

            result = compute_root_for_day(conn, "test-corp", today)
            conn.commit()
            assert result["signature_b64u"] is not None
            assert result["signing_key_id"] is not None

            # Reconstruct canonical envelope + verify signature
            canonical = canonical_payload_bytes(
                {
                    "enterprise_id": result["enterprise_id"],
                    "root_date": result["root_date"],
                    "event_count": result["event_count"],
                    "merkle_root_hash": result["merkle_root_hash"],
                    "first_event_id": result["first_event_id"],
                    "last_event_id": result["last_event_id"],
                }
            )
            ok = verify_raw(
                result["signing_key_id"], canonical, result["signature_b64u"]
            )
            assert ok is True
        finally:
            forward_sign.reload_l2_privkey()
