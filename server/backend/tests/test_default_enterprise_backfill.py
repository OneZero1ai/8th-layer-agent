"""Backfill migration 0013 — legacy default-enterprise KUs (#121 finding 3).

The #89 tenancy fix only protects new INSERTs. KUs proposed before
that fix shipped sit in ``enterprise_id='default-enterprise'`` /
``group_id='default-group'`` regardless of which tenant the proposer
actually belongs to. Migration 0013 reassigns those rows by parsing
``created_by`` out of the JSON ``data`` blob and looking up the
proposer's tenancy in ``users``.

These tests pin the migration's behaviour:

1. Eligible legacy rows (default-enterprise + known-non-default
   proposer) get reassigned to the proposer's actual tenancy.
2. Rows with no ``created_by``, an empty ``created_by``, or a
   ``created_by`` that doesn't resolve to a user — left alone.
3. Rows whose proposer is also at default tenancy — left alone (no
   useful signal to use for reassignment).
4. Rows already at non-default tenancy (i.e. proposed *after* the
   #89 fix) — left alone. The migration is restricted to the bug's
   blast radius.
5. Re-running the upgrade is a no-op (idempotent).

Tests use the public ``run_migrations`` entrypoint plus direct
sqlite3 inspection — same shape as the other 001x migration tests in
this directory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cq_server.migrations import HEAD_REVISION, run_migrations


def _seed_legacy_ku(
    conn: sqlite3.Connection,
    *,
    unit_id: str,
    created_by: str,
    enterprise_id: str = "default-enterprise",
    group_id: str = "default-group",
) -> None:
    """Insert a KU directly into knowledge_units, bypassing the store.

    Mimics the pre-#89 shape: tenancy columns at default, a populated
    ``data`` JSON with the proposer's username under ``created_by``.
    """
    data = {
        "id": unit_id,
        "domains": ["test-fleet"],
        "insight": {
            "summary": "legacy KU for backfill probe",
            "detail": "Inserted with default tenancy to simulate pre-#89.",
            "action": "Migration 0013 should reassign based on created_by.",
        },
        "tier": "private",
        "created_by": created_by,
        "evidence": {"confirmations": 0, "flags": []},
    }
    conn.execute(
        "INSERT INTO knowledge_units "
        "(id, data, created_at, tier, status, enterprise_id, group_id) "
        "VALUES (?, ?, ?, 'private', 'pending', ?, ?)",
        (
            unit_id,
            json.dumps(data),
            datetime.now(UTC).isoformat(),
            enterprise_id,
            group_id,
        ),
    )


def _seed_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    enterprise_id: str,
    group_id: str,
) -> None:
    conn.execute(
        "INSERT INTO users "
        "(username, password_hash, created_at, enterprise_id, group_id) "
        "VALUES (?, '$2b$12$placeholder', ?, ?, ?)",
        (username, datetime.now(UTC).isoformat(), enterprise_id, group_id),
    )


def _read_tenancy(conn: sqlite3.Connection, unit_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT enterprise_id, group_id FROM knowledge_units WHERE id = ?",
        (unit_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """Bring a DB up through 0012 (one short of the backfill migration).

    All tests then seed legacy + user rows and step the chain forward
    one revision so we can observe the backfill in isolation.
    """
    path = tmp_path / "backfill.db"
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(Path.home()),
        "CQ_DB_PATH": str(path),
    }
    up = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "0012_pending_review_tier"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert up.returncode == 0, f"upgrade-to-0012 failed: {up.stderr}\n{up.stdout}"
    return path


def _step_to_0013(db_path: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(Path.home()),
        "CQ_DB_PATH": str(db_path),
    }
    return subprocess.run(
        ["uv", "run", "alembic", "upgrade", "0013_backfill_default_enterprise_kus"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Headline behaviour: eligible row reassigned to proposer tenancy.
# ---------------------------------------------------------------------------


class TestLegacyRowReassignment:
    def test_default_enterprise_row_with_known_proposer_gets_reassigned(self, db: Path) -> None:
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="bran",
                enterprise_id="moscowmul3",
                group_id="engineering",
            )
            _seed_legacy_ku(conn, unit_id="legacy-1", created_by="bran")
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            ent, grp = _read_tenancy(conn, "legacy-1")
        finally:
            conn.close()
        assert ent == "moscowmul3"
        assert grp == "engineering"

    def test_two_proposers_two_enterprises_each_row_lands_correctly(self, db: Path) -> None:
        """Pin per-row resolution. Two legacy KUs from two proposers
        in two enterprises — each row gets its own proposer's tenancy,
        not the wrong one (no thread-local cache, no cross-row leak).
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="alice",
                enterprise_id="acme",
                group_id="solutions",
            )
            _seed_user(
                conn,
                username="bob",
                enterprise_id="moscowmul3",
                group_id="engineering",
            )
            _seed_legacy_ku(conn, unit_id="ku-alice", created_by="alice")
            _seed_legacy_ku(conn, unit_id="ku-bob", created_by="bob")
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "ku-alice") == ("acme", "solutions")
            assert _read_tenancy(conn, "ku-bob") == ("moscowmul3", "engineering")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Skip conditions — rows the migration must leave untouched.
# ---------------------------------------------------------------------------


class TestRowsLeftAlone:
    def test_row_with_unknown_proposer_stays_default(self, db: Path) -> None:
        """``created_by`` doesn't resolve to any user — no signal to
        reassign on. Row stays at default-enterprise.
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_legacy_ku(conn, unit_id="ghost", created_by="never-existed")
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "ghost") == (
                "default-enterprise",
                "default-group",
            )
        finally:
            conn.close()

    def test_row_with_empty_created_by_stays_default(self, db: Path) -> None:
        conn = sqlite3.connect(str(db))
        try:
            _seed_legacy_ku(conn, unit_id="empty-attr", created_by="")
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "empty-attr") == (
                "default-enterprise",
                "default-group",
            )
        finally:
            conn.close()

    def test_row_whose_proposer_is_also_default_stays_default(self, db: Path) -> None:
        """If the proposer is themselves at default tenancy, there's
        no useful signal — the migration shouldn't pretend to "fix"
        the row by re-tenanting it to default-enterprise (no-op).
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="default_user",
                enterprise_id="default-enterprise",
                group_id="default-group",
            )
            _seed_legacy_ku(conn, unit_id="dual-default", created_by="default_user")
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "dual-default") == (
                "default-enterprise",
                "default-group",
            )
        finally:
            conn.close()

    def test_row_already_at_non_default_tenancy_is_not_overwritten(self, db: Path) -> None:
        """A row proposed *after* the #89 fix has the right tenancy
        already — the migration must not touch it (the WHERE clause's
        ``enterprise_id = 'default-enterprise'`` guard).
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="alice",
                enterprise_id="acme",
                group_id="solutions",
            )
            # Already correctly-tenanted row — don't touch it.
            _seed_legacy_ku(
                conn,
                unit_id="post-fix",
                created_by="alice",
                enterprise_id="acme",
                group_id="solutions",
            )
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "post-fix") == ("acme", "solutions")
        finally:
            conn.close()

    def test_malformed_data_blob_is_skipped_not_aborted(self, db: Path) -> None:
        """A single bad-JSON row must not abort the whole migration
        — the row stays put, every other eligible row still backfills.
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="alice",
                enterprise_id="acme",
                group_id="solutions",
            )
            # Good row — must still backfill.
            _seed_legacy_ku(conn, unit_id="good", created_by="alice")
            # Bad row — invalid JSON blob.
            conn.execute(
                "INSERT INTO knowledge_units "
                "(id, data, created_at, tier, status, enterprise_id, group_id) "
                "VALUES ('bad', 'not-json{', ?, 'private', 'pending', "
                "'default-enterprise', 'default-group')",
                (datetime.now(UTC).isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

        result = _step_to_0013(db)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "good") == ("acme", "solutions")
            # Bad row stays put — no crash, no migration abort.
            assert _read_tenancy(conn, "bad") == (
                "default-enterprise",
                "default-group",
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Idempotency + chain integration.
# ---------------------------------------------------------------------------


class TestIdempotencyAndChain:
    def test_rerun_via_run_migrations_is_noop(self, db: Path) -> None:
        """Running the chain twice through ``run_migrations`` (the ops
        path, not the alembic CLI) leaves the data unchanged the
        second time. Pre-fix this would re-process every row; post-
        fix the WHERE clause sees zero candidates.
        """
        conn = sqlite3.connect(str(db))
        try:
            _seed_user(
                conn,
                username="alice",
                enterprise_id="acme",
                group_id="solutions",
            )
            _seed_legacy_ku(conn, unit_id="rerun", created_by="alice")
            conn.commit()
        finally:
            conn.close()

        # Step forward via the ops entrypoint — this is what runs at
        # server startup, not the alembic CLI.
        run_migrations(f"sqlite:///{db}")
        run_migrations(f"sqlite:///{db}")  # second call must be no-op.

        conn = sqlite3.connect(str(db))
        try:
            assert _read_tenancy(conn, "rerun") == ("acme", "solutions")
        finally:
            conn.close()

    def test_head_revision_is_current(self) -> None:
        """If a future migration lands without bumping HEAD_REVISION,
        ``run_migrations`` silently misses the new revision on stamped
        DBs. Pin the constant so the chain head is the source of truth.
        """
        # Bumped to 0016_xgroup_consent (Phase 1.0b — Decision 28).
        # Chains after 0015_phase_1_0c_aigrp_peers_pair_secret_ref.
        assert HEAD_REVISION == "0016_xgroup_consent"
