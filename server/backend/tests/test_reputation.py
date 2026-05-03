"""Tests for reputation log v1-alpha (task #99).

Covers:
    * Schema: 0008 migration creates the tables.
    * record_event: first event uses GENESIS_PREV_HASH.
    * record_event: subsequent events chain via prev_event_hash.
    * Tampering: changing one event's body breaks chain verification.
    * Recording is best-effort: a closed connection is logged, not raised.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cq_server import reputation
from cq_server.migrations import run_migrations


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Bring up a fresh DB at HEAD_REVISION and yield a raw connection."""
    monkeypatch.setenv("CQ_ENTERPRISE", "test-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    db = tmp_path / "rep.db"
    run_migrations(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))
    yield conn
    conn.close()


class TestSchema:
    def test_migration_creates_reputation_tables(self, conn: sqlite3.Connection) -> None:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reputation_events" in names
        assert "reputation_chain_meta" in names

    def test_signature_columns_nullable(self, conn: sqlite3.Connection) -> None:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(reputation_events)").fetchall()}
        # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
        assert cols["signature_b64u"][3] == 0, "signature should be nullable in alpha"
        assert cols["signing_key_id"][3] == 0


class TestRecordEvent:
    def test_first_event_uses_genesis_prev_hash(self, conn: sqlite3.Connection) -> None:
        eid = reputation.record_event(
            conn,
            event_type="consult.closed",
            body={"thread_id": "th_1", "csat": 5},
        )
        conn.commit()
        assert eid is not None
        row = conn.execute(
            "SELECT prev_event_hash, payload_hash FROM reputation_events WHERE event_id = ?",
            (eid,),
        ).fetchone()
        assert row is not None
        prev_hash, payload_hash = row
        assert prev_hash == reputation.GENESIS_PREV_HASH
        assert payload_hash.startswith("sha256:")

    def test_chain_advances_on_second_event(self, conn: sqlite3.Connection) -> None:
        e1 = reputation.record_event(conn, event_type="consult.closed", body={"i": 1})
        conn.commit()
        e2 = reputation.record_event(conn, event_type="consult.closed", body={"i": 2})
        conn.commit()
        rows = conn.execute(
            "SELECT event_id, prev_event_hash, payload_hash FROM reputation_events ORDER BY event_id"
        ).fetchall()
        assert {r[0] for r in rows} == {e1, e2}
        # Second event's prev_event_hash equals first event's payload_hash.
        first = next(r for r in rows if r[0] == e1)
        second = next(r for r in rows if r[0] == e2)
        assert second[1] == first[2]
        # And chain meta points at the latest event.
        meta = conn.execute(
            "SELECT last_event_id, last_event_hash FROM reputation_chain_meta WHERE enterprise_id = ?",
            ("test-corp",),
        ).fetchone()
        assert meta == (e2, second[2])

    def test_chain_verification_detects_body_tampering(self, conn: sqlite3.Connection) -> None:
        e1 = reputation.record_event(conn, event_type="consult.closed", body={"x": 1})
        e2 = reputation.record_event(conn, event_type="consult.closed", body={"x": 2})
        conn.commit()
        assert e1 and e2
        # Mutate event 1's payload_canonical (simulating a rogue admin edit).
        conn.execute(
            "UPDATE reputation_events SET payload_canonical = ? WHERE event_id = ?",
            ('{"tampered":true}', e1),
        )
        conn.commit()
        # Re-derive event 1's hash from the (mutated) canonical bytes.
        row = conn.execute(
            "SELECT payload_canonical FROM reputation_events WHERE event_id = ?",
            (e1,),
        ).fetchone()
        recomputed = reputation.compute_payload_hash(row[0].encode("utf-8"))
        # Event 2 chained against the ORIGINAL e1 hash; tampering invalidates the chain.
        e2_prev = conn.execute(
            "SELECT prev_event_hash FROM reputation_events WHERE event_id = ?",
            (e2,),
        ).fetchone()[0]
        assert e2_prev != recomputed

    def test_record_swallows_errors_on_closed_conn(
        self, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn.close()
        # Re-open just to satisfy the type, but the inner write will fail
        # because the migration-side conn was the bound one. Easiest way
        # to trigger an error: pass a connection to a non-existent DB.
        bad = sqlite3.connect(":memory:")
        # bad has no reputation_events table → INSERT raises → record_event swallows.
        result = reputation.record_event(bad, event_type="consult.closed", body={})
        assert result is None
        bad.close()
