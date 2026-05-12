"""Partial unique index on provisioning_jobs.enterprise_id (HIGH #4 fix).

8l-reviewer follow-up on PR #228: the original migration 0021 declared
``enterprise_id`` as ``unique=True`` (full UNIQUE constraint), which means
a FAILED job permanently locks that slug — the customer cannot retry their
own signup without operator DB intervention.

This migration:

1. Drops the column-level UNIQUE constraint via Alembic batch mode
   (SQLite doesn't support DROP CONSTRAINT directly; batch-mode rebuilds
   the table).
2. Drops the explicit unique index ``idx_provisioning_jobs_enterprise_id``.
3. Creates a partial unique index that only enforces uniqueness on
   non-terminal rows: ``WHERE status NOT IN ('FAILED', 'COMPLETED')``.

Net effect: a customer with a FAILED job can re-POST the same slug; an
in-flight job still gets the 409 SLUG_TAKEN via IntegrityError → 409.

Revision ID: 0021a_provisioning_partial_unique
Revises: 0021_provisioning_jobs
Created: 2026-05-12 (8l-reviewer re-review post-HIGH-fix cycle)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021a_provisioning_partial_unique"
down_revision: str | Sequence[str] | None = "0021_provisioning_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace full UNIQUE on enterprise_id with partial unique index."""
    # 1. Drop the explicit unique index that 0021 created.
    op.drop_index(
        "idx_provisioning_jobs_enterprise_id", table_name="provisioning_jobs"
    )

    # 2. Drop the column-level UNIQUE constraint via batch mode (SQLite).
    #    Batch mode rebuilds the table without the inline `unique=True`.
    with op.batch_alter_table("provisioning_jobs", recreate="always") as batch_op:
        batch_op.alter_column("enterprise_id", existing_type=sa.Text(), unique=False)

    # 3. Create the partial unique index. SQLite supports CREATE UNIQUE
    #    INDEX ... WHERE ... natively.
    op.execute(
        "CREATE UNIQUE INDEX idx_provisioning_jobs_active_slug "
        "ON provisioning_jobs (enterprise_id) "
        "WHERE status NOT IN ('FAILED', 'COMPLETED')"
    )


def downgrade() -> None:
    """Restore full UNIQUE on enterprise_id."""
    op.execute("DROP INDEX IF EXISTS idx_provisioning_jobs_active_slug")

    with op.batch_alter_table("provisioning_jobs", recreate="always") as batch_op:
        batch_op.alter_column("enterprise_id", existing_type=sa.Text(), unique=True)

    op.create_index(
        "idx_provisioning_jobs_enterprise_id",
        "provisioning_jobs",
        ["enterprise_id"],
        unique=True,
    )
