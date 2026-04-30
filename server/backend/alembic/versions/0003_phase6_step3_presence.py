"""Phase 6 step 3: presence registry + admin role column.

Revision ID: 0003_phase6_step3
Revises: 0002_phase6_step2
Create Date: 2026-04-30

Adds the schema delta required by Lanes C and D of the live-network-demo
plan:

  - ``users.role`` — TEXT NOT NULL DEFAULT 'user'. Lets admin-only routes
    (POST /consents/sign, DELETE /consents/{id}) gate access without
    introducing a separate roles table; v1 is global admin scope, per-
    Enterprise admin is deferred.
  - ``peers`` table — live presence registry keyed by ``persona``. Holds
    the heartbeat timestamp + opt-in metadata an L2 advertises. Indexed
    on (enterprise_id, group_id) for the active-peers listing and on
    last_seen_at for window-bounded queries.

Mirrors the runtime ``ensure_user_role_column`` and ``ensure_peers_schema``
helpers so the Alembic-first DB and the legacy-runtime DB end up
indistinguishable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_phase6_step3"
down_revision: str | Sequence[str] | None = "0002_phase6_step2"
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
    """Add ``users.role`` + create the ``peers`` table."""
    bind = op.get_bind()

    # 1. users.role — additive column, default 'user', NOT NULL after backfill.
    if _table_exists(bind, "users"):
        existing = _column_names(bind, "users")
        if "role" not in existing:
            op.add_column(
                "users",
                sa.Column(
                    "role",
                    sa.Text(),
                    nullable=True,
                    server_default=sa.text("'user'"),
                ),
            )
            op.execute(
                sa.text("UPDATE users SET role = 'user' WHERE role IS NULL")
            )
            with op.batch_alter_table("users") as batch:
                batch.alter_column(
                    "role",
                    existing_type=sa.Text(),
                    nullable=False,
                    existing_server_default=sa.text("'user'"),
                )

    # 2. peers — presence registry.
    if not _table_exists(bind, "peers"):
        op.create_table(
            "peers",
            sa.Column("persona", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("group_id", sa.Text(), nullable=False),
            sa.Column("last_seen_at", sa.Text(), nullable=False),
            sa.Column("expertise_vector", sa.LargeBinary(), nullable=True),
            sa.Column("expertise_domains", sa.Text(), nullable=True),
            sa.Column(
                "discoverable",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("working_dir_hint", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
        )
        op.create_index(
            "idx_peers_enterprise_group",
            "peers",
            ["enterprise_id", "group_id"],
        )
        op.create_index(
            "idx_peers_last_seen",
            "peers",
            ["last_seen_at"],
        )


def downgrade() -> None:
    """Drop the ``peers`` table + remove ``users.role``.

    Used by migration tests; production rollbacks should prefer leaving
    the additive column in place since downgrading after data has been
    written can lose role assignments.
    """
    bind = op.get_bind()
    if _table_exists(bind, "peers"):
        op.drop_index("idx_peers_last_seen", table_name="peers")
        op.drop_index("idx_peers_enterprise_group", table_name="peers")
        op.drop_table("peers")
    if _table_exists(bind, "users"):
        existing = _column_names(bind, "users")
        if "role" in existing:
            with op.batch_alter_table("users") as batch:
                batch.drop_column("role")
