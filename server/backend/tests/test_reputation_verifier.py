"""Tests for the reputation verifier library (task #108 sub-task 7)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cq_server import forward_sign, reputation
from cq_server.daily_root import compute_root_for_day
from cq_server.migrations import run_migrations
from cq_server.reputation_verifier import (
    verify_chain,
    verify_event_payload_hashes,
    verify_event_signatures,
    verify_root,
)


@pytest.fixture()
def conn_with_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    """DB with an L2 signing key in place — events come out signed."""
    monkeypatch.setenv("CQ_ENTERPRISE", "test-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    key_path = tmp_path / "l2_key.bin"
    monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(key_path))
    forward_sign.reload_l2_privkey()

    db = tmp_path / "rep.db"
    run_migrations(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))
    yield conn
    conn.close()
    forward_sign.reload_l2_privkey()  # cleanup cache


def _read_events(conn: sqlite3.Connection, enterprise_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_id, event_type, enterprise_id, l2_id, ts,
               prev_event_hash, payload_canonical, payload_hash,
               signature_b64u, signing_key_id, created_at
        FROM reputation_events
        WHERE enterprise_id = ?
        ORDER BY ts ASC, event_id ASC
        """,
        (enterprise_id,),
    ).fetchall()
    cols = [
        "event_id",
        "event_type",
        "enterprise_id",
        "l2_id",
        "ts",
        "prev_event_hash",
        "payload_canonical",
        "payload_hash",
        "signature_b64u",
        "signing_key_id",
        "created_at",
    ]
    return [dict(zip(cols, r, strict=True)) for r in rows]


class TestChain:
    def test_empty_chain_verifies(self, conn_with_signing: sqlite3.Connection) -> None:
        result = verify_chain([])
        assert result["ok"] is True
        assert result["count"] == 0

    def test_intact_chain_verifies(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        for i in range(5):
            reputation.record_event(
                conn_with_signing,
                event_type="consult.closed",
                body={"i": i},
                ts=f"2026-01-01T0{i}:00:00Z",
            )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        assert len(events) == 5
        result = verify_chain(events)
        assert result["ok"] is True
        assert result["count"] == 5

    def test_broken_chain_detected(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        for i in range(3):
            reputation.record_event(
                conn_with_signing,
                event_type="consult.closed",
                body={"i": i},
                ts=f"2026-01-01T0{i}:00:00Z",
            )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        # Mutate event[1].prev_event_hash so the link breaks
        events[1]["prev_event_hash"] = "sha256:" + ("0" * 64)
        result = verify_chain(events)
        assert result["ok"] is False
        assert result["broken_at_index"] == 1


class TestPayloadHashes:
    def test_intact_payloads_verify(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        reputation.record_event(
            conn_with_signing, event_type="ku.event", body={"unit_id": "ku_x"}
        )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        assert verify_event_payload_hashes(events)["ok"] is True

    def test_tampered_canonical_detected(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        reputation.record_event(
            conn_with_signing, event_type="ku.event", body={"unit_id": "ku_x"}
        )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        events[0]["payload_canonical"] = '{"tampered":true}'
        result = verify_event_payload_hashes(events)
        assert result["ok"] is False
        assert events[0]["event_id"] in result["tampered_event_ids"]


class TestSignatures:
    def test_signed_events_verify(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        reputation.record_event(
            conn_with_signing, event_type="ku.event", body={"unit_id": "ku_x"}
        )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        result = verify_event_signatures(events)
        assert result["ok"] is True
        assert result["signed_count"] == 1
        assert result["unsigned_count"] == 0

    def test_tampered_signature_detected(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        reputation.record_event(
            conn_with_signing, event_type="ku.event", body={"unit_id": "ku_x"}
        )
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")
        # Flip a character in the signature
        original = events[0]["signature_b64u"]
        events[0]["signature_b64u"] = ("A" if original[0] != "A" else "B") + original[1:]
        result = verify_event_signatures(events)
        assert result["ok"] is False
        assert events[0]["event_id"] in result["bad_signature_event_ids"]


class TestRoot:
    def test_root_verifies_against_events(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        today = datetime.now(UTC).date().isoformat()
        for i in range(4):
            reputation.record_event(
                conn_with_signing,
                event_type="consult.closed",
                body={"i": i},
                ts=f"{today}T0{i}:00:00Z",
            )
        conn_with_signing.commit()

        root = compute_root_for_day(conn_with_signing, "test-corp", today)
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")

        result = verify_root(root, events)
        assert result["ok"] is True
        assert result["root_matches_events"] is True
        assert result["signature_valid"] is True

    def test_root_mismatch_detected_when_event_dropped(
        self, conn_with_signing: sqlite3.Connection
    ) -> None:
        today = datetime.now(UTC).date().isoformat()
        for i in range(3):
            reputation.record_event(
                conn_with_signing,
                event_type="consult.closed",
                body={"i": i},
                ts=f"{today}T0{i}:00:00Z",
            )
        conn_with_signing.commit()
        root = compute_root_for_day(conn_with_signing, "test-corp", today)
        conn_with_signing.commit()
        events = _read_events(conn_with_signing, "test-corp")

        # Drop one event from the verifier's input — root should mismatch.
        result = verify_root(root, events[:-1])
        assert result["root_matches_events"] is False
        assert result["ok"] is False
