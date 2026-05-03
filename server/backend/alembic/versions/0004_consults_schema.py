"""Phase 2 — port consults_schema fork-delta tables to Alembic.

Revision ID: 0004_consults
Revises: 0003_phase6_step3
Create Date: 2026-05-02

Brings the L3 consult tables (sprint 2, issue #20) under Alembic
ownership. Mirrors ``cq_server.tables.ensure_consults_schema`` so an
Alembic-first DB and a legacy-runtime DB end up indistinguishable.

Two tables:
  - ``consults``: one row per agent-to-agent thread.
  - ``consult_messages``: append-only message log per thread.

Idempotent: runs against a prod DB where these tables already exist
(via ``ensure_consults_schema`` at startup) — the table-existence
guard makes the migration a no-op in that case.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_consults"
down_revision: str | Sequence[str] | None = "0003_phase6_step3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create ``consults`` and ``consult_messages`` tables if missing."""
    bind = op.get_bind()

    if not _table_exists(bind, "consults"):
        op.create_table(
            "consults",
            sa.Column("thread_id", sa.Text(), primary_key=True),
            sa.Column("from_l2_id", sa.Text(), nullable=False),
            sa.Column("from_persona", sa.Text(), nullable=False),
            sa.Column("to_l2_id", sa.Text(), nullable=False),
            sa.Column("to_persona", sa.Text(), nullable=False),
            sa.Column("subject", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'open'"),
            ),
            sa.Column("claimed_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("closed_at", sa.Text(), nullable=True),
            sa.Column("resolution_summary", sa.Text(), nullable=True),
        )
        op.create_index(
            "idx_consults_to_l2_persona",
            "consults",
            ["to_l2_id", "to_persona", "status"],
        )
        op.create_index(
            "idx_consults_from_l2_persona",
            "consults",
            ["from_l2_id", "from_persona"],
        )
        op.create_index("idx_consults_created", "consults", ["created_at"])

    if not _table_exists(bind, "consult_messages"):
        op.create_table(
            "consult_messages",
            sa.Column("message_id", sa.Text(), primary_key=True),
            sa.Column("thread_id", sa.Text(), nullable=False),
            sa.Column("from_l2_id", sa.Text(), nullable=False),
            sa.Column("from_persona", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["thread_id"], ["consults.thread_id"]),
        )
        op.create_index(
            "idx_consult_messages_thread",
            "consult_messages",
            ["thread_id", "created_at"],
        )


def downgrade() -> None:
    """Drop the consult tables.

    Used by migration tests; production rollbacks should leave the
    tables in place — consult history is corporate IP.
    """
    bind = op.get_bind()
    if _table_exists(bind, "consult_messages"):
        op.drop_index("idx_consult_messages_thread", table_name="consult_messages")
        op.drop_table("consult_messages")
    if _table_exists(bind, "consults"):
        op.drop_index("idx_consults_created", table_name="consults")
        op.drop_index("idx_consults_from_l2_persona", table_name="consults")
        op.drop_index("idx_consults_to_l2_persona", table_name="consults")
        op.drop_table("consults")
