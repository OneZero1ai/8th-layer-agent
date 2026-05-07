"""Stage 1 of #108 — activity-log Alembic migration smoke-tests.

Mirrors the pattern in ``test_migration_0003_presence.py`` /
``test_migration_0002_xgroup_consent.py``: every migration in this
chain ships an upgrade-from-empty + upgrade-from-legacy + downgrade
cycle test, and a history-linearity sanity test.

Stage 1 is **schema only** — Stage 2 (instrumentation-engineer) wires
existing handlers to write rows, and ships ``GET /api/v1/activity``.
This file therefore covers:

* ``activity_log`` + ``activity_retention_config`` create cleanly on
  an empty DB and on a populated legacy DB.
* The CHECK constraint on ``event_type`` rejects unknown values.
* The ``DEFAULT 90`` and ``retention_days > 0`` constraints on
  ``activity_retention_config`` work as advertised.
* ``SqliteStore.append_activity`` round-trips a row through every
  nullable / non-nullable column.
* Retention config helpers fall back to 90 when no row exists, and
  upsert correctly.
* ``purge_activity_older_than`` deletes only rows older than the
  cutoff and only for the named tenant.
* The chain head moved to ``0011_activity_log`` (catches the easy
  miss of forgetting to bump ``HEAD_REVISION``).
* ``alembic history`` shows the new revision chained off
  ``0010_reflect_submissions``.

Tests intentionally do **not** assert on the route handlers (Stage 2)
or on any read endpoint — those land in a separate PR.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _run_alembic(
    db_path: Path, command: str, target: str
) -> subprocess.CompletedProcess[str]:
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


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


# --- 1. fresh DB upgrade/downgrade ----------------------------------------


class TestUpgradeDowngradeEmpty:
    def test_upgrade_creates_activity_log_with_indexes(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_empty.db"
        sqlite3.connect(str(db)).close()  # touch

        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, f"upgrade failed:\n{up.stderr}\n{up.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "activity_log")
            assert _table_exists(check, "activity_retention_config")

            # Column shape mirrors the #108 schema sketch verbatim.
            cols = _column_names(check, "activity_log")
            assert cols == [
                "id",
                "ts",
                "tenant_enterprise",
                "tenant_group",
                "persona",
                "human",
                "event_type",
                "payload",
                "result_summary",
                "thread_or_chain_id",
            ]

            idx = _index_names(check, "activity_log")
            assert "idx_activity_log_tenant_ts" in idx
            assert "idx_activity_log_persona_ts" in idx
            assert "idx_activity_log_event_type_ts" in idx
            assert "idx_activity_log_thread" in idx

            # Retention config has the 90-day default.
            row = check.execute(
                "SELECT dflt_value FROM pragma_table_info('activity_retention_config') "
                "WHERE name = 'retention_days'"
            ).fetchone()
            assert row is not None
            assert row[0] == "90"
        finally:
            check.close()

        down = _run_alembic(db, "downgrade", "0010_reflect_submissions")
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}\n{down.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert not _table_exists(check, "activity_log")
            assert not _table_exists(check, "activity_retention_config")
        finally:
            check.close()


# --- 2. legacy DB upgrade -------------------------------------------------


class TestUpgradeOnLegacyDb:
    """Pre-Alembic prod DB has knowledge_units but no alembic_version.

    The runtime path (run_migrations) stamps at baseline first; without
    the stamp, ``upgrade head`` errors trying to re-CREATE existing
    tables. This test exercises the full chain on a populated legacy
    fixture and asserts the activity-log tables land cleanly without
    touching pre-existing rows.
    """

    def test_upgrade_creates_tables_without_disturbing_legacy_rows(
        self, tmp_path: Path
    ) -> None:
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

        from cq_server.migrations import run_migrations
        run_migrations(f"sqlite:///{db}")

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "activity_log")
            assert _table_exists(check, "activity_retention_config")
            # Legacy row survived.
            row = check.execute(
                "SELECT id FROM knowledge_units WHERE id = 'legacy_ku'"
            ).fetchone()
            assert row is not None
            assert row[0] == "legacy_ku"
        finally:
            check.close()


# --- 3. CHECK constraints -------------------------------------------------


class TestCheckConstraints:
    """Direct SQL probes — covers the CHECK constraints declared in
    the migration without going through SqliteStore (which validates
    in Python *before* the constraint fires)."""

    def test_event_type_rejects_unknown_value(self, tmp_path: Path) -> None:
        db = tmp_path / "ck_event.db"
        sqlite3.connect(str(db)).close()
        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr

        conn = sqlite3.connect(str(db))
        # Foreign keys are off by default in raw sqlite3 connections;
        # CHECK constraints fire regardless. Confirm.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO activity_log "
                "(id, ts, tenant_enterprise, event_type, payload) "
                "VALUES ('act_x', '2026-05-06T00:00:00Z', 'ent', 'not_a_real_event', '{}')"
            )
        conn.close()

    def test_event_type_accepts_every_locked_enum_value(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "ck_event_ok.db"
        sqlite3.connect(str(db)).close()
        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr

        from cq_server.activity import EVENT_TYPES

        conn = sqlite3.connect(str(db))
        for i, et in enumerate(sorted(EVENT_TYPES)):
            conn.execute(
                "INSERT INTO activity_log "
                "(id, ts, tenant_enterprise, event_type, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"act_ok_{i:02d}", "2026-05-06T00:00:00Z", "ent", et, "{}"),
            )
        conn.commit()
        # Every event-type round-tripped.
        cnt = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        assert cnt == len(EVENT_TYPES)
        conn.close()

    def test_retention_days_must_be_positive(self, tmp_path: Path) -> None:
        db = tmp_path / "ck_retention.db"
        sqlite3.connect(str(db)).close()
        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr

        conn = sqlite3.connect(str(db))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO activity_retention_config "
                "(enterprise_id, retention_days, updated_at) "
                "VALUES ('ent', 0, '2026-05-06T00:00:00Z')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO activity_retention_config "
                "(enterprise_id, retention_days, updated_at) "
                "VALUES ('ent', -1, '2026-05-06T00:00:00Z')"
            )
        conn.close()


# --- 4. Store helper round-trip -------------------------------------------


class TestSqliteStoreActivityHelpers:
    """End-to-end through the SqliteStore wrapper. Stage-2 callers
    will go through the async surface; tests use the sync proxy for
    brevity, same as every other helper test in this suite."""

    async def test_append_activity_round_trip(self, tmp_path: Path) -> None:
        from cq_server.activity import generate_activity_id, now_iso_z
        from cq_server.store import SqliteStore

        db = tmp_path / "store.db"
        store = SqliteStore(db_path=db)
        try:
            aid = generate_activity_id()
            ts = now_iso_z()
            await store.append_activity(
                activity_id=aid,
                ts=ts,
                tenant_enterprise="ent-1",
                tenant_group="grp-A",
                persona="alice@persona",
                human="alice",
                event_type="query",
                payload={"domains": ["api"], "limit": 5},
                result_summary={"ku_ids": ["ku_x"], "cache_hit": False},
                thread_or_chain_id=None,
            )

            row = store.sync._engine.connect().execute(
                __import__("sqlalchemy").text(
                    "SELECT id, tenant_enterprise, tenant_group, persona, human, "
                    "event_type, payload, result_summary, thread_or_chain_id "
                    "FROM activity_log WHERE id = :id"
                ),
                {"id": aid},
            ).fetchone()
            assert row is not None
            assert row[0] == aid
            assert row[1] == "ent-1"
            assert row[2] == "grp-A"
            assert row[3] == "alice@persona"
            assert row[4] == "alice"
            assert row[5] == "query"
            # JSON round-trip preserves shape.
            import json as _json
            assert _json.loads(row[6]) == {"domains": ["api"], "limit": 5}
            assert _json.loads(row[7]) == {"ku_ids": ["ku_x"], "cache_hit": False}
            assert row[8] is None
        finally:
            store.sync.close()

    async def test_append_activity_rejects_unknown_event_type(
        self, tmp_path: Path
    ) -> None:
        from cq_server.activity import generate_activity_id, now_iso_z
        from cq_server.store import SqliteStore

        db = tmp_path / "store_bad.db"
        store = SqliteStore(db_path=db)
        try:
            with pytest.raises(ValueError, match="unknown activity event_type"):
                await store.append_activity(
                    activity_id=generate_activity_id(),
                    ts=now_iso_z(),
                    tenant_enterprise="ent",
                    tenant_group=None,
                    persona=None,
                    human=None,
                    event_type="bogus",
                )
        finally:
            store.sync.close()

    async def test_append_activity_allows_null_persona_and_group(
        self, tmp_path: Path
    ) -> None:
        """System events log without a persona / group / human."""
        from cq_server.activity import generate_activity_id, now_iso_z
        from cq_server.store import SqliteStore

        db = tmp_path / "store_null.db"
        store = SqliteStore(db_path=db)
        try:
            aid = generate_activity_id()
            await store.append_activity(
                activity_id=aid,
                ts=now_iso_z(),
                tenant_enterprise="ent-sys",
                tenant_group=None,
                persona=None,
                human=None,
                event_type="review_start",
                payload={"reason": "automatic_quality_gate"},
            )
            # Insert succeeded — assert by counting.
            cnt = (
                store.sync._engine.connect()
                .execute(
                    __import__("sqlalchemy").text(
                        "SELECT COUNT(*) FROM activity_log WHERE id = :id"
                    ),
                    {"id": aid},
                )
                .scalar()
            )
            assert cnt == 1
        finally:
            store.sync.close()


class TestRetentionConfigHelpers:
    async def test_default_is_90_when_no_row(self, tmp_path: Path) -> None:
        from cq_server.store import SqliteStore

        db = tmp_path / "ret_default.db"
        store = SqliteStore(db_path=db)
        try:
            assert (
                await store.get_activity_retention_days(enterprise_id="any-ent")
                == 90
            )
        finally:
            store.sync.close()

    async def test_set_then_get_round_trip(self, tmp_path: Path) -> None:
        from cq_server.store import SqliteStore

        db = tmp_path / "ret_set.db"
        store = SqliteStore(db_path=db)
        try:
            await store.set_activity_retention_days(
                enterprise_id="ent-1", retention_days=30
            )
            assert (
                await store.get_activity_retention_days(enterprise_id="ent-1")
                == 30
            )
            # Upsert overrides existing value.
            await store.set_activity_retention_days(
                enterprise_id="ent-1", retention_days=180
            )
            assert (
                await store.get_activity_retention_days(enterprise_id="ent-1")
                == 180
            )
            # Sibling enterprise still defaults.
            assert (
                await store.get_activity_retention_days(enterprise_id="ent-2")
                == 90
            )
        finally:
            store.sync.close()

    async def test_set_rejects_non_positive(self, tmp_path: Path) -> None:
        from cq_server.store import SqliteStore

        db = tmp_path / "ret_bad.db"
        store = SqliteStore(db_path=db)
        try:
            with pytest.raises(ValueError):
                await store.set_activity_retention_days(
                    enterprise_id="ent-1", retention_days=0
                )
            with pytest.raises(ValueError):
                await store.set_activity_retention_days(
                    enterprise_id="ent-1", retention_days=-7
                )
        finally:
            store.sync.close()


class TestPurgeOlderThan:
    async def test_purge_deletes_only_old_rows_for_named_tenant(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from cq_server.activity import generate_activity_id
        from cq_server.store import SqliteStore

        db = tmp_path / "purge.db"
        store = SqliteStore(db_path=db)
        try:
            now = datetime.now(UTC)
            old_ts = (now - timedelta(days=120)).isoformat().replace("+00:00", "Z")
            recent_ts = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
            cutoff_iso = (now - timedelta(days=90)).isoformat().replace("+00:00", "Z")

            # Three rows: one old + one recent for ent-1, one old for ent-2.
            await store.append_activity(
                activity_id=generate_activity_id(),
                ts=old_ts,
                tenant_enterprise="ent-1",
                tenant_group=None,
                persona=None,
                human=None,
                event_type="query",
            )
            await store.append_activity(
                activity_id=generate_activity_id(),
                ts=recent_ts,
                tenant_enterprise="ent-1",
                tenant_group=None,
                persona=None,
                human=None,
                event_type="query",
            )
            await store.append_activity(
                activity_id=generate_activity_id(),
                ts=old_ts,
                tenant_enterprise="ent-2",
                tenant_group=None,
                persona=None,
                human=None,
                event_type="query",
            )

            # Purge ent-1 only.
            deleted = await store.purge_activity_older_than(
                tenant_enterprise="ent-1", cutoff_iso=cutoff_iso
            )
            assert deleted == 1

            # ent-1 still has the recent row; ent-2 still has its old row.
            import sqlalchemy as _sa
            with store.sync._engine.connect() as conn:
                rows = conn.execute(
                    _sa.text(
                        "SELECT tenant_enterprise, ts FROM activity_log "
                        "ORDER BY tenant_enterprise, ts"
                    )
                ).fetchall()
            assert [(r[0], r[1]) for r in rows] == [
                ("ent-1", recent_ts),
                ("ent-2", old_ts),
            ]
        finally:
            store.sync.close()


# --- 5. chain head + history ---------------------------------------------


class TestHeadRevisionAndHistory:
    def test_head_revision_constant_was_bumped(self) -> None:
        """If a future migration lands without bumping HEAD_REVISION,
        ``test_migrations`` keeps asserting the old head, which the
        test suite would still pass — but ``run_migrations`` would
        silently miss the new revision on stamped DBs. The constant
        is the source of truth for ops scripts and stamp logic."""
        from cq_server.migrations import HEAD_REVISION

        assert HEAD_REVISION == "0011_activity_log"

    @pytest.mark.parametrize("invalid_dir", [None])
    def test_alembic_history_includes_0011(self, invalid_dir: object) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["uv", "run", "alembic", "history"],
            cwd=str(repo_root),
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "CQ_DB_PATH": "/tmp/cq-alembic-history-check-0011.db",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "0010_reflect_submissions" in result.stdout
        assert "0011_activity_log" in result.stdout
