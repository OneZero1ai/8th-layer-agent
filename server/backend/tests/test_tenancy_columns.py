"""Phase 6 step 1: regression tests for additive tenancy columns.

These tests pin two invariants:

  1. New rows written through the propose-time path land in the
     ``default-enterprise`` / ``default-group`` scope.
  2. Pre-existing rows on a "legacy" DB (the shape that production looks
     like at https://8thlayer.onezero1.ai right now — no tenancy
     columns) get backfilled to the same defaults when the migration /
     the runtime ``ensure_tenancy_columns`` helper runs.

Read-path filtering is intentionally NOT tested here — that work lands
in a follow-up PR. This PR only ships the columns.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cq.models import Insight, KnowledgeUnit, create_knowledge_unit

from cq_server.store import SqliteStore
from cq_server.tables import (
    DEFAULT_ENTERPRISE_ID,
    DEFAULT_GROUP_ID,
)

# --- helpers ------------------------------------------------------------


def _make_unit(**overrides: Any) -> KnowledgeUnit:
    defaults = {
        "domains": ["test-fleet", "tenancy"],
        "insight": Insight(
            summary="Tenancy columns smoke",
            detail="Phase 6 step 1 regression fixture.",
            action="Assert default scope on new and legacy rows.",
        ),
    }
    return create_knowledge_unit(**{**defaults, **overrides})


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    from cq_server.migrations import run_migrations
    db = tmp_path / "tenancy.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _scope(conn: sqlite3.Connection, table: str, key_col: str, key: str) -> tuple[str, str]:
    row = conn.execute(
        f"SELECT enterprise_id, group_id FROM {table} WHERE {key_col} = ?",
        (key,),
    ).fetchone()
    assert row is not None, f"no row in {table} for {key_col}={key!r}"
    return row[0], row[1]


# --- new-row defaults ---------------------------------------------------


class TestNewRowDefaults:
    def test_inserted_ku_lands_in_default_scope(self, store: SqliteStore) -> None:
        unit = _make_unit()
        store.sync.insert(unit)
        ent, grp = _scope(store._conn, "knowledge_units", "id", unit.id)
        assert ent == DEFAULT_ENTERPRISE_ID
        assert grp == DEFAULT_GROUP_ID

    def test_created_user_lands_in_default_scope(self, store: SqliteStore) -> None:
        store.sync.create_user("alice", "pwhash")
        ent, grp = _scope(store._conn, "users", "username", "alice")
        assert ent == DEFAULT_ENTERPRISE_ID
        assert grp == DEFAULT_GROUP_ID

    def test_columns_are_not_null(self, store: SqliteStore) -> None:
        import sqlalchemy.exc

        # Prove the schema rejects an explicit NULL.
        with (
            pytest.raises((sqlite3.IntegrityError, sqlalchemy.exc.IntegrityError)),
            store._engine.begin() as conn,
        ):
            conn.exec_driver_sql(
                "INSERT INTO knowledge_units (id, data, enterprise_id, group_id) "
                "VALUES (?, ?, ?, ?)",
                ("ku_null", "{}", None, "default-group"),
            )


# --- legacy-row backfill ------------------------------------------------


# PR-C: TestLegacyBackfill class deleted. ``ensure_tenancy_columns`` was
# the runtime tenancy backfill helper; Alembic migration
# ``0001_phase6_step1`` now owns that path, and ``TestAlembicMigration``
# below covers the upgrade/downgrade matrix end-to-end.


# --- alembic upgrade / downgrade ---------------------------------------


class TestAlembicMigration:
    """End-to-end: run the Alembic migration on an empty DB and on a
    DB that already has rows. Both upgrade and downgrade must complete
    cleanly. This is the migration smoke-test the PR description calls
    for.
    """

    def _run_alembic(self, db_path: Path, command: str) -> subprocess.CompletedProcess[str]:
        repo_root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            ["uv", "run", "alembic", command, "head" if command == "upgrade" else "base"],
            cwd=str(repo_root),
            env={
                "PATH": _path_env(),
                "CQ_DB_PATH": str(db_path),
                "HOME": str(Path.home()),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_upgrade_then_downgrade_clean_on_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_empty.db"
        # Touch the DB so the file exists but has no schema.
        sqlite3.connect(str(db)).close()

        up = self._run_alembic(db, "upgrade")
        assert up.returncode == 0, f"upgrade failed: {up.stderr}\n{up.stdout}"

        down = self._run_alembic(db, "downgrade")
        assert down.returncode == 0, f"downgrade failed: {down.stderr}\n{down.stdout}"

    def test_upgrade_on_legacy_db_backfills_rows(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_legacy.db"
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

        # Use the python runtime path so the legacy DB is stamped at
        # baseline before the upgrade walks the chain.
        from cq_server.migrations import run_migrations
        run_migrations(f"sqlite:///{db}")

        # Inspect the row scope post-migration.
        check = sqlite3.connect(str(db))
        ent_ku, grp_ku = _scope(check, "knowledge_units", "id", "legacy_ku")
        assert (ent_ku, grp_ku) == (DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID)
        ent_u, grp_u = _scope(check, "users", "username", "legacy_user")
        assert (ent_u, grp_u) == (DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID)
        check.close()


def _path_env() -> str:
    """Pass through the test runner's PATH so subprocess can find ``uv``."""
    import os

    return os.environ.get("PATH", "")
