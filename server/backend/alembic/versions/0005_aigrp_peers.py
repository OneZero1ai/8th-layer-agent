"""Phase 2 — port aigrp_peers fork-delta table to Alembic.

Revision ID: 0005_aigrp_peers
Revises: 0004_consults
Create Date: 2026-05-02

Brings the AIGRP peer roster (sprint 4 forward-id binding, issue #44)
under Alembic ownership. Mirrors
``cq_server.tables.ensure_aigrp_peers_table`` — including the additive
``public_key_ed25519`` column added post-merge.

Idempotent: prod DBs that already have this table (via the
runtime ``_ensure_schema`` path) skip the CREATE; the column-existence
guard skips the ALTER.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_aigrp_peers"
down_revision: str | Sequence[str] | None = "0004_consults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Create ``aigrp_peers`` table + ensure ``public_key_ed25519`` column."""
    bind = op.get_bind()

    if not _table_exists(bind, "aigrp_peers"):
        op.create_table(
            "aigrp_peers",
            sa.Column("l2_id", sa.Text(), primary_key=True),
            sa.Column("enterprise", sa.Text(), nullable=False),
            sa.Column("group", sa.Text(), nullable=False),
            sa.Column("endpoint_url", sa.Text(), nullable=False),
            sa.Column("embedding_centroid", sa.LargeBinary(), nullable=True),
            sa.Column("domain_bloom", sa.LargeBinary(), nullable=True),
            sa.Column(
                "ku_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "domain_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("embedding_model", sa.Text(), nullable=True),
            sa.Column("first_seen_at", sa.Text(), nullable=False),
            sa.Column("last_seen_at", sa.Text(), nullable=False),
            sa.Column("last_signature_at", sa.Text(), nullable=True),
            sa.Column("public_key_ed25519", sa.Text(), nullable=True),
        )
        op.create_index(
            "idx_aigrp_peers_enterprise",
            "aigrp_peers",
            ["enterprise"],
        )
    else:
        existing = _column_names(bind, "aigrp_peers")
        if "public_key_ed25519" not in existing:
            op.add_column(
                "aigrp_peers",
                sa.Column("public_key_ed25519", sa.Text(), nullable=True),
            )


def downgrade() -> None:
    """Drop the aigrp_peers table."""
    bind = op.get_bind()
    if _table_exists(bind, "aigrp_peers"):
        op.drop_index("idx_aigrp_peers_enterprise", table_name="aigrp_peers")
        op.drop_table("aigrp_peers")
