"""Phase 2 — port directory_peerings fork-delta table to Alembic.

Revision ID: 0006_directory_peerings
Revises: 0005_aigrp_peers
Create Date: 2026-05-02

Brings the local mirror of directory peering records (sprint 3) under
Alembic ownership. Mirrors
``cq_server.tables.ensure_directory_peerings_schema`` — including the
sprint-4 additive ``to_l2_endpoints_json`` column for cross-Enterprise
consult forward routing.

Each row carries BOTH signed envelopes (offer + accept) so any local
consumer can re-verify offline.

Idempotent: prod DBs that already have this table skip the CREATE;
the column-existence guard skips the additive ALTER.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_directory_peerings"
down_revision: str | Sequence[str] | None = "0005_aigrp_peers"
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
    """Create ``aigrp_directory_peerings`` + ensure ``to_l2_endpoints_json``."""
    bind = op.get_bind()

    if not _table_exists(bind, "aigrp_directory_peerings"):
        op.create_table(
            "aigrp_directory_peerings",
            sa.Column("offer_id", sa.Text(), primary_key=True),
            sa.Column("from_enterprise", sa.Text(), nullable=False),
            sa.Column("to_enterprise", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("content_policy", sa.Text(), nullable=False),
            sa.Column("consult_logging_policy", sa.Text(), nullable=False),
            sa.Column(
                "topic_filters_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("active_from", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("offer_payload_canonical", sa.Text(), nullable=False),
            sa.Column("offer_signature_b64u", sa.Text(), nullable=False),
            sa.Column("offer_signing_key_id", sa.Text(), nullable=False),
            sa.Column("accept_payload_canonical", sa.Text(), nullable=False),
            sa.Column("accept_signature_b64u", sa.Text(), nullable=False),
            sa.Column("accept_signing_key_id", sa.Text(), nullable=False),
            sa.Column("last_synced_at", sa.Text(), nullable=False),
            sa.Column(
                "to_l2_endpoints_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )
        op.create_index(
            "idx_directory_peerings_from",
            "aigrp_directory_peerings",
            ["from_enterprise", "status"],
        )
        op.create_index(
            "idx_directory_peerings_to",
            "aigrp_directory_peerings",
            ["to_enterprise", "status"],
        )
    else:
        existing = _column_names(bind, "aigrp_directory_peerings")
        if "to_l2_endpoints_json" not in existing:
            op.add_column(
                "aigrp_directory_peerings",
                sa.Column(
                    "to_l2_endpoints_json",
                    sa.Text(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                ),
            )


def downgrade() -> None:
    """Drop the aigrp_directory_peerings table."""
    bind = op.get_bind()
    if _table_exists(bind, "aigrp_directory_peerings"):
        op.drop_index(
            "idx_directory_peerings_to",
            table_name="aigrp_directory_peerings",
        )
        op.drop_index(
            "idx_directory_peerings_from",
            table_name="aigrp_directory_peerings",
        )
        op.drop_table("aigrp_directory_peerings")
