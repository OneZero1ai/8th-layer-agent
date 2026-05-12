"""AS-1 follow-up: persona_assignment_audit — append-only history table.

Revision ID: 0023_persona_assignment_audit
Revises: 0022_persona_assignments
Create Date: 2026-05-12

The base 0022 table overwrites a single row per Human, which destroys the
``assigned_by`` history every time an admin changes a persona. The audit
table records every CREATED/CHANGED/DISABLED/ENABLED transition so the
operator surface can answer "who set Carol to admin and when?"

# Chain note

After this migration lands, HEAD_REVISION in migrations.py must be
``0023_persona_assignment_audit``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_persona_assignment_audit"
down_revision: str | Sequence[str] | None = "0022_persona_assignments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``persona_assignment_audit`` table."""
    bind = op.get_bind()

    if _table_exists(bind, "persona_assignment_audit"):
        return

    op.create_table(
        "persona_assignment_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False),
        # old_persona is NULL on the CREATED row (no prior state).
        sa.Column("old_persona", sa.Text(), nullable=True),
        # new_persona is NULL on the DISABLED row (no live persona after).
        sa.Column("new_persona", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "action IN ('CREATED', 'CHANGED', 'DISABLED', 'ENABLED')",
            name="ck_audit_action",
        ),
    )
    op.create_index(
        "idx_audit_username",
        "persona_assignment_audit",
        ["username", sa.text("changed_at DESC")],
    )


def downgrade() -> None:
    """Drop the audit table."""
    bind = op.get_bind()
    if not _table_exists(bind, "persona_assignment_audit"):
        return
    op.drop_index("idx_audit_username", table_name="persona_assignment_audit")
    op.drop_table("persona_assignment_audit")
