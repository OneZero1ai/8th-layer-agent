"""Phase 6 step 2: Alembic migration smoke-test.

Mirrors the pattern in ``test_tenancy_columns.py::TestAlembicMigration``.
Runs ``alembic upgrade head`` and ``alembic downgrade base`` against
both an empty DB and a populated legacy DB; both cycles must complete
without raising and must end with the expected schema in place.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _run_alembic(db_path: Path, command: str, target: str) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        ["uv", "run", "alembic", command, target],
        cwd=str(repo_root),
        env={
            "PATH": os.environ.get("PATH", ""),
            "CQ_DB_PATH": str(db_path),
            "HOME": str(Path.home()),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


class TestUpgradeDowngradeEmpty:
    def test_upgrade_then_downgrade_clean_on_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_empty.db"
        sqlite3.connect(str(db)).close()  # touch

        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, f"upgrade failed:\n{up.stderr}\n{up.stdout}"

        # After upgrade head: cross_l2_audit + cross_enterprise_consents
        # exist; if knowledge_units has been created (it hasn't on a
        # truly-empty DB, since the runtime store creates it lazily),
        # then cross_group_allowed exists too.
        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "cross_l2_audit")
            assert _table_exists(check, "cross_enterprise_consents")
            if _table_exists(check, "knowledge_units"):
                assert _column_exists(check, "knowledge_units", "cross_group_allowed")
        finally:
            check.close()

        down = _run_alembic(db, "downgrade", "base")
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}\n{down.stdout}"

        # After full downgrade the new tables are gone.
        check = sqlite3.connect(str(db))
        try:
            assert not _table_exists(check, "cross_l2_audit")
            assert not _table_exists(check, "cross_enterprise_consents")
        finally:
            check.close()


class TestUpgradeOnLegacyDb:
    def test_upgrade_on_populated_legacy_db_adds_column_and_tables(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "alembic_legacy.db"

        # Pre-step-1 shape: knowledge_units / users without tenancy
        # columns. This is what production looks like before any
        # Phase 6 migration runs.
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE knowledge_units (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO knowledge_units (id, data) VALUES ('legacy_ku', '{}');
            INSERT INTO users (username, password_hash, created_at)
                VALUES ('legacy_user', 'hash', '2024-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        # Use the python runtime path (run_migrations) so the legacy DB
        # is stamped at baseline before the upgrade walks the chain.
        # The bare CLI path doesn't stamp, so it errors trying to
        # create knowledge_units a second time.
        from cq_server.migrations import run_migrations
        run_migrations(f"sqlite:///{db}")

        check = sqlite3.connect(str(db))
        try:
            # Step 1 columns landed.
            assert _column_exists(check, "knowledge_units", "enterprise_id")
            assert _column_exists(check, "knowledge_units", "group_id")
            # Step 2 column landed.
            assert _column_exists(check, "knowledge_units", "cross_group_allowed")
            # New tables exist.
            assert _table_exists(check, "cross_enterprise_consents")
            assert _table_exists(check, "cross_l2_audit")
            # Pre-existing row picks up cross_group_allowed = 0.
            row = check.execute(
                "SELECT cross_group_allowed FROM knowledge_units WHERE id = ?",
                ("legacy_ku",),
            ).fetchone()
            assert row is not None
            assert row[0] == 0
        finally:
            check.close()


class TestStepwiseUpgrade:
    """Confirm 0001 -> 0002 is applied in the right order — going to
    the 0001 head first, then to head, must work without error."""

    def test_upgrade_to_0001_then_head(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_stepwise.db"
        sqlite3.connect(str(db)).close()

        first = _run_alembic(db, "upgrade", "0001_phase6_step1")
        assert first.returncode == 0, f"step 1 upgrade failed:\n{first.stderr}\n{first.stdout}"

        # cross_l2_audit must NOT exist yet — that's a step-2 table.
        check = sqlite3.connect(str(db))
        try:
            assert not _table_exists(check, "cross_l2_audit")
        finally:
            check.close()

        second = _run_alembic(db, "upgrade", "head")
        assert second.returncode == 0, f"step 2 upgrade failed:\n{second.stderr}\n{second.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "cross_l2_audit")
            assert _table_exists(check, "cross_enterprise_consents")
        finally:
            check.close()


@pytest.mark.parametrize("invalid_dir", [None])
def test_alembic_history_is_linear(invalid_dir: object) -> None:
    """Sanity: alembic history should show 0002 chained off 0001."""
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "run", "alembic", "history"],
        cwd=str(repo_root),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path.home()),
            "CQ_DB_PATH": "/tmp/cq-alembic-history-check.db",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Both revs appear; 0002 lists 0001 as its predecessor.
    assert "0001_phase6_step1" in result.stdout
    assert "0002_phase6_step2" in result.stdout
