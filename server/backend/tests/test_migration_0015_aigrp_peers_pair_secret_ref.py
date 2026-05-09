"""Phase 1.0c — ``aigrp_peers.pair_secret_ref`` migration smoke-tests.

Mirrors the structure of ``test_migration_0011_activity_log.py``:

* Fresh-DB upgrade creates the column + index, downgrade removes both.
* Legacy-DB upgrade (table pre-populated with rows that have
  ``l2_id`` but no ``pair_secret_ref``) backfills the canonical
  pair-name for every existing row.
* History-linearity sanity check — chain head moved to 0015.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _run_alembic(
    db_path: Path,
    command: str,
    target: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CQ_DB_PATH": str(db_path),
        "HOME": str(Path.home()),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["uv", "run", "alembic", command, target],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


# --- 1. fresh DB upgrade/downgrade ----------------------------------------


class TestUpgradeDowngradeFresh:
    def test_upgrade_creates_pair_secret_ref_column_and_index(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_fresh.db"
        sqlite3.connect(str(db)).close()  # touch

        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, f"upgrade failed:\n{up.stderr}\n{up.stdout}"

        check = sqlite3.connect(str(db))
        try:
            assert _table_exists(check, "aigrp_peers")
            cols = _column_names(check, "aigrp_peers")
            assert "pair_secret_ref" in cols
            # Column shape sanity — text + NOT NULL.
            info = check.execute(
                "SELECT name, type, [notnull] FROM pragma_table_info('aigrp_peers') WHERE name = 'pair_secret_ref'"
            ).fetchone()
            assert info is not None
            assert info[0] == "pair_secret_ref"
            assert info[1].upper() == "TEXT"
            assert info[2] == 1  # NOT NULL

            idx = _index_names(check, "aigrp_peers")
            assert "idx_aigrp_peers_pair_secret_ref" in idx
        finally:
            check.close()

        down = _run_alembic(db, "downgrade", "0014_crosstalk_tables")
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}\n{down.stdout}"

        check = sqlite3.connect(str(db))
        try:
            cols = _column_names(check, "aigrp_peers")
            assert "pair_secret_ref" not in cols
            idx = _index_names(check, "aigrp_peers")
            assert "idx_aigrp_peers_pair_secret_ref" not in idx
        finally:
            check.close()


# --- 2. legacy DB upgrade with backfill -----------------------------------


class TestUpgradeBackfillsExistingRows:
    """Pre-populate ``aigrp_peers`` at revision 0014 (no
    ``pair_secret_ref``), then upgrade to 0015 and verify backfill."""

    def test_backfill_derives_canonical_pair_name_for_each_row(self, tmp_path: Path) -> None:
        db = tmp_path / "alembic_backfill.db"
        sqlite3.connect(str(db)).close()

        # Stop at the previous head to populate without the new column.
        up0 = _run_alembic(db, "upgrade", "0014_crosstalk_tables")
        assert up0.returncode == 0, f"pre-upgrade failed:\n{up0.stderr}\n{up0.stdout}"

        # Insert a few peer rows. The migration will derive
        # canonical_pair_name(self_l2, peer_l2_id). With env vars
        # ``CQ_ENTERPRISE=acme`` + ``CQ_GROUP=engineering`` the self id
        # is ``acme/engineering``.
        seed = sqlite3.connect(str(db))
        try:
            seed.executemany(
                """
                INSERT INTO aigrp_peers (
                    l2_id, enterprise, "group", endpoint_url,
                    embedding_centroid, domain_bloom, ku_count, domain_count,
                    embedding_model, first_seen_at, last_seen_at,
                    last_signature_at, public_key_ed25519
                ) VALUES (
                    ?, ?, ?, ?, NULL, NULL, 0, 0, NULL,
                    '2026-05-08T00:00:00+00:00',
                    '2026-05-08T00:00:00+00:00',
                    NULL, NULL
                )
                """,
                [
                    ("acme/sga", "acme", "sga", "https://sga.acme.example"),
                    ("acme/finance", "acme", "finance", "https://fin.acme.example"),
                    # A peer that lex-sorts BEFORE the self_l2 — exercises
                    # the canonical-min branch.
                    ("acme/aardvark", "acme", "aardvark", "https://a.acme.example"),
                ],
            )
            seed.commit()
        finally:
            seed.close()

        up1 = _run_alembic(
            db,
            "upgrade",
            "head",
            extra_env={"CQ_ENTERPRISE": "acme", "CQ_GROUP": "engineering"},
        )
        assert up1.returncode == 0, f"upgrade failed:\n{up1.stderr}\n{up1.stdout}"

        check = sqlite3.connect(str(db))
        try:
            rows = check.execute("SELECT l2_id, pair_secret_ref FROM aigrp_peers ORDER BY l2_id").fetchall()
            # Every row populated.
            assert len(rows) == 3
            for _l2_id, ref in rows:
                assert ref is not None
                assert ref.startswith("aigrp-pair:")

            row_map = dict(rows)

            # Lex-min canonicalization: ``acme/engineering`` (self) sorts
            # AFTER ``acme/aardvark`` but BEFORE ``acme/finance`` and
            # ``acme/sga``. So:
            assert row_map["acme/aardvark"] == "aigrp-pair:acme/aardvark:acme/engineering"
            assert row_map["acme/finance"] == "aigrp-pair:acme/engineering:acme/finance"
            assert row_map["acme/sga"] == "aigrp-pair:acme/engineering:acme/sga"
        finally:
            check.close()

    def test_backfill_no_rows_is_noop(self, tmp_path: Path) -> None:
        """Empty ``aigrp_peers`` is the common case for fresh deploys —
        upgrade must succeed without env vars set, and produce a
        column with the NOT NULL constraint visible."""
        db = tmp_path / "alembic_empty_peers.db"
        sqlite3.connect(str(db)).close()

        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, f"upgrade failed:\n{up.stderr}\n{up.stdout}"

        check = sqlite3.connect(str(db))
        try:
            cnt = check.execute("SELECT COUNT(*) FROM aigrp_peers").fetchone()[0]
            assert cnt == 0
            cols = _column_names(check, "aigrp_peers")
            assert "pair_secret_ref" in cols
        finally:
            check.close()


# --- 3. NOT NULL constraint shape ----------------------------------------


class TestNotNullShape:
    """Column is NOT NULL with ``server_default=''``. Two consequences
    we want to lock in:

    * Explicit ``NULL`` is rejected (NOT NULL fires).
    * Omitting the column lets ``server_default`` populate the empty
      sentinel — preserves backwards-compat with INSERTs from app
      code that pre-dates the Phase 1.0b update."""

    def test_explicit_null_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "ck_explicit_null.db"
        sqlite3.connect(str(db)).close()
        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr

        conn = sqlite3.connect(str(db))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO aigrp_peers (
                        l2_id, enterprise, "group", endpoint_url,
                        first_seen_at, last_seen_at, pair_secret_ref
                    ) VALUES (
                        'acme/x', 'acme', 'x', 'https://x.example',
                        '2026-05-09T00:00:00+00:00',
                        '2026-05-09T00:00:00+00:00',
                        NULL
                    )
                    """
                )
        finally:
            conn.close()

    def test_omitted_column_falls_back_to_empty_sentinel(self, tmp_path: Path) -> None:
        db = tmp_path / "ck_omitted.db"
        sqlite3.connect(str(db)).close()
        up = _run_alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr

        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                """
                INSERT INTO aigrp_peers (
                    l2_id, enterprise, "group", endpoint_url,
                    first_seen_at, last_seen_at
                ) VALUES (
                    'acme/x', 'acme', 'x', 'https://x.example',
                    '2026-05-09T00:00:00+00:00',
                    '2026-05-09T00:00:00+00:00'
                )
                """
            )
            conn.commit()
            row = conn.execute("SELECT pair_secret_ref FROM aigrp_peers WHERE l2_id = 'acme/x'").fetchone()
            assert row is not None
            assert row[0] == ""
        finally:
            conn.close()


# --- 4. chain head + history ---------------------------------------------


class TestHeadRevisionAndHistory:
    def test_head_revision_constant_was_bumped(self) -> None:
        from cq_server.migrations import HEAD_REVISION

        assert HEAD_REVISION == "0015_phase_1_0c_aigrp_peers_pair_secret_ref"

    def test_alembic_history_includes_0015(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["uv", "run", "alembic", "history"],
            cwd=str(repo_root),
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "CQ_DB_PATH": "/tmp/cq-alembic-history-check-0015.db",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "0014_crosstalk_tables" in result.stdout
        assert "0015_phase_1_0c_aigrp_peers_pair_secret_ref" in result.stdout
