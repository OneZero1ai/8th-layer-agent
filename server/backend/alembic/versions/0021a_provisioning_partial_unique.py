"""Partial unique index on provisioning_jobs.enterprise_id (HIGH #4 fix).

8l-reviewer follow-up on PR #228 (third pass 2026-05-12):

The original 0021 migration declared ``enterprise_id`` ``unique=True``
(full UNIQUE constraint) — a FAILED job permanently locks that slug, so
a customer cannot retry their own signup without operator DB intervention.

This migration replaces the full UNIQUE with a **partial unique index**
that only enforces uniqueness on non-terminal rows:

    CREATE UNIQUE INDEX idx_provisioning_jobs_active_slug
    ON provisioning_jobs (enterprise_id)
    WHERE status NOT IN ('FAILED', 'COMPLETED')

# Why raw SQL table swap

The first attempt at this migration used Alembic batch mode:

    with op.batch_alter_table("provisioning_jobs", recreate="always") as bop:
        bop.alter_column("enterprise_id", existing_type=sa.Text(), unique=False)

The 8l-reviewer empirically verified (third pass, SQLAlchemy 2.0.49 +
Alembic 1.18.4) that the post-migration DDL still contained
``UNIQUE (enterprise_id)`` at the table level. Root cause: when batch
mode reflects an existing SQLite table, unnamed inline UNIQUE constraints
become reflected ``UniqueConstraint`` objects that ``alter_column(...,
unique=False)`` does NOT remove. Alembic's docs treat ``unique=`` on
``alter_column`` as advisory hint, not a constraint drop.

So this migration takes the explicit-table-swap path: rename the old
table out of the way, ``CREATE TABLE`` afresh with the columns we want
(no inline UNIQUE), copy rows back, drop the old table. SQLite supports
this pattern natively; it's the standard "alter table" workaround the
SQLite docs recommend.

Revision ID: 0021a_provisioning_partial_unique
Revises: 0021_provisioning_jobs
Created: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021a_provisioning_partial_unique"
down_revision: str | Sequence[str] | None = "0021_provisioning_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The target shape of ``provisioning_jobs`` after this migration — same
# columns as 0021 but WITHOUT the inline ``unique=True`` on enterprise_id.
def _create_new_table_sql() -> str:
    return """
    CREATE TABLE provisioning_jobs (
        job_id TEXT PRIMARY KEY,
        enterprise_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PROVISIONING',
        phase INTEGER,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        error TEXT,
        result_json TEXT,
        ip_hash TEXT NOT NULL DEFAULT '',
        assume_role_external_id TEXT NOT NULL DEFAULT '',
        job_params_json TEXT
    )
    """


def upgrade() -> None:
    """Replace full UNIQUE on enterprise_id with partial unique index."""
    bind = op.get_bind()

    # 1. Drop the explicit unique index that 0021 created. (Idempotent.)
    inspector = sa.inspect(bind)
    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("provisioning_jobs")
    }
    if "idx_provisioning_jobs_enterprise_id" in existing_indexes:
        op.drop_index(
            "idx_provisioning_jobs_enterprise_id",
            table_name="provisioning_jobs",
        )

    # 2. Rename old table → recreate without UNIQUE → copy data → drop old.
    #    All in a single transaction so a partial failure leaves us
    #    either fully on the old schema or fully on the new.
    op.execute("ALTER TABLE provisioning_jobs RENAME TO _provisioning_jobs_pre_21a")
    op.execute(_create_new_table_sql())
    op.execute(
        "INSERT INTO provisioning_jobs ("
        "job_id, enterprise_id, status, phase, started_at, completed_at, "
        "error, result_json, ip_hash, assume_role_external_id, job_params_json"
        ") SELECT "
        "job_id, enterprise_id, status, phase, started_at, completed_at, "
        "error, result_json, ip_hash, assume_role_external_id, job_params_json "
        "FROM _provisioning_jobs_pre_21a"
    )
    op.execute("DROP TABLE _provisioning_jobs_pre_21a")

    # 3. Recreate the rate-limit index that 0021 set up (it's named, so
    #    the rename above preserved nothing — we need to redeclare).
    op.create_index(
        "idx_provisioning_jobs_ip_hash_started_at",
        "provisioning_jobs",
        ["ip_hash", "started_at"],
    )

    # 4. Create the partial unique index — the actual point of this
    #    migration. SQLite supports CREATE UNIQUE INDEX ... WHERE ...
    #    natively (3.8.0+).
    op.execute(
        "CREATE UNIQUE INDEX idx_provisioning_jobs_active_slug "
        "ON provisioning_jobs (enterprise_id) "
        "WHERE status NOT IN ('FAILED', 'COMPLETED')"
    )


def downgrade() -> None:
    """Restore full UNIQUE on enterprise_id (0021 shape)."""
    op.execute("DROP INDEX IF EXISTS idx_provisioning_jobs_active_slug")
    op.execute(
        "DROP INDEX IF EXISTS idx_provisioning_jobs_ip_hash_started_at"
    )

    op.execute(
        "ALTER TABLE provisioning_jobs RENAME TO _provisioning_jobs_pre_21a"
    )
    op.execute(
        """
        CREATE TABLE provisioning_jobs (
            job_id TEXT PRIMARY KEY,
            enterprise_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'PROVISIONING',
            phase INTEGER,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            result_json TEXT,
            ip_hash TEXT NOT NULL DEFAULT '',
            assume_role_external_id TEXT NOT NULL DEFAULT '',
            job_params_json TEXT
        )
        """
    )
    op.execute(
        "INSERT INTO provisioning_jobs ("
        "job_id, enterprise_id, status, phase, started_at, completed_at, "
        "error, result_json, ip_hash, assume_role_external_id, job_params_json"
        ") SELECT "
        "job_id, enterprise_id, status, phase, started_at, completed_at, "
        "error, result_json, ip_hash, assume_role_external_id, job_params_json "
        "FROM _provisioning_jobs_pre_21a"
    )
    op.execute("DROP TABLE _provisioning_jobs_pre_21a")

    op.create_index(
        "idx_provisioning_jobs_ip_hash_started_at",
        "provisioning_jobs",
        ["ip_hash", "started_at"],
    )
    op.create_index(
        "idx_provisioning_jobs_enterprise_id",
        "provisioning_jobs",
        ["enterprise_id"],
        unique=True,
    )
