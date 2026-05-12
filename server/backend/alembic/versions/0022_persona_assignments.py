"""AS-1: persona_assignments table — per-Human persona in the L2 admin shell.

Revision ID: 0022_persona_assignments
Revises: 0020_l2_brand
Create Date: 2026-05-12

Design choice: one active persona per Human per L2 for v1 (many-to-one).
The four hardcoded personas are: admin, viewer, agent, external-collaborator.

# Soft-disable

Humans are never hard-deleted from the persona surface — the ``disabled_at``
column on this table is a soft flag. The underlying ``users`` row is unchanged;
only the persona assignment is disabled.

# Chain note

Chains from 0021_provisioning_jobs (FO-2-backend, closed #224).
After this migration lands, HEAD_REVISION in migrations.py must be bumped to
``0022_persona_assignments``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_persona_assignments"
down_revision: str | Sequence[str] | None = "0021_provisioning_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``persona_assignments`` table."""
    bind = op.get_bind()

    if _table_exists(bind, "persona_assignments"):
        return

    op.create_table(
        "persona_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Foreign key to users.username (string PK on the users side).
        # We store username rather than the integer id so the join is
        # readable in audit queries without an extra lookup.
        sa.Column(
            "username",
            sa.Text(),
            sa.ForeignKey("users.username"),
            nullable=False,
            unique=True,  # one active persona per Human per L2 in v1
        ),
        # Persona ENUM stored as TEXT with a CHECK constraint.
        # Four hardcoded values for v1; no custom personas.
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        # Soft-disable flag.  NULL = active; ISO-8601 timestamp = disabled.
        sa.Column("disabled_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "persona IN ('admin', 'viewer', 'agent', 'external-collaborator')",
            name="ck_persona_assignments_persona_enum",
        ),
    )
    # Partial index so the uniqueness constraint on username is efficient.
    op.create_index(
        "ix_persona_assignments_username",
        "persona_assignments",
        ["username"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the table."""
    bind = op.get_bind()
    if not _table_exists(bind, "persona_assignments"):
        return
    op.drop_index("ix_persona_assignments_username", table_name="persona_assignments")
    op.drop_table("persona_assignments")
