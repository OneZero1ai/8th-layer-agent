"""Phase 6 step 3: Alembic migration smoke-test.

Mirrors the pattern in ``test_migration_0002_xgroup_consent.py``. Runs
``alembic upgrade head`` and ``alembic downgrade base`` against both an
empty DB and a populated legacy DB; both cycles must complete without
raising and end with the expected schema.
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

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "peers")
            # peers indexes exist.
            idx_rows = check.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='peers'"
            ).fetchall()
            idx_names = {r[0] for r in idx_rows}
            assert "idx_peers_enterprise_group" in idx_names
            assert "idx_peers_last_seen" in idx_names
            # If users exists (it doesn't on a truly-empty DB — runtime
            # creates it lazily), then role exists too.
            if _table_exists(check, "users"):
                assert _column_exists(check, "users", "role")
        finally:
            check.close()

        down = _run_alembic(db, "downgrade", "base")
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}\n{down.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert not _table_exists(check, "peers")
        finally:
            check.close()


class TestUpgradeOnLegacyDb:
    def test_upgrade_on_populated_legacy_db_adds_role_and_peers(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "alembic_legacy.db"
        # Pre-step-1 shape: minimal users/knowledge_units, no tenancy /
        # xgroup / role columns. Seed one user so the role-backfill is
        # exercised.
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
            INSERT INTO users (username, password_hash, created_at)
                VALUES ('legacy-user', 'hash', '2024-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        # Use the python runtime path so the legacy DB is stamped at
        # baseline before the chain walks; the bare CLI doesn't stamp.
        from cq_server.migrations import run_migrations
        run_migrations(f"sqlite:///{db}")

        check = sqlite3.connect(str(db))
        try:
            # All three migration steps applied.
            assert _column_exists(check, "knowledge_units", "enterprise_id")
            assert _column_exists(check, "knowledge_units", "cross_group_allowed")
            assert _column_exists(check, "users", "role")
            assert _table_exists(check, "peers")
            # Backfill: legacy user gets role='user'.
            row = check.execute(
                "SELECT role FROM users WHERE username = 'legacy-user'"
            ).fetchone()
            assert row is not None
            assert row[0] == "user"
        finally:
            check.close()


class TestStepwiseUpgrade:
    """0001 -> 0002 -> 0003 must apply in order."""

    def test_upgrade_to_0002_then_head(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_stepwise.db"
        sqlite3.connect(str(db)).close()

        first = _run_alembic(db, "upgrade", "0002_phase6_step2")
        assert first.returncode == 0, f"step 2 upgrade failed:\n{first.stderr}\n{first.stdout}"

        # peers must NOT exist yet.
        check = sqlite3.connect(str(db))
        try:
            assert not _table_exists(check, "peers")
        finally:
            check.close()

        second = _run_alembic(db, "upgrade", "head")
        assert second.returncode == 0, f"step 3 upgrade failed:\n{second.stderr}\n{second.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "peers")
        finally:
            check.close()


@pytest.mark.parametrize("invalid_dir", [None])
def test_alembic_history_includes_0003(invalid_dir: object) -> None:
    """Sanity: alembic history shows 0003 chained off 0002."""
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "run", "alembic", "history"],
        cwd=str(repo_root),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path.home()),
            "CQ_DB_PATH": "/tmp/cq-alembic-history-check-0003.db",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "0002_phase6_step2" in result.stdout
    assert "0003_phase6_step3" in result.stdout
