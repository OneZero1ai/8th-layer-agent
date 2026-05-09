"""Phase 6 step 1: additive tenancy columns on knowledge_units and users.

Revision ID: 0001_phase6_step1
Revises:
Create Date: 2026-04-30

Adds ``enterprise_id`` and ``group_id`` to ``knowledge_units`` and
``users``. Both default to ``default-enterprise`` / ``default-group`` and
are marked NOT NULL after a backfill of any pre-existing rows.

This is the first Alembic migration in the project. The runtime store
still creates its schema directly via ``_ensure_schema`` (see
``cq_server/store/__init__.py``), so this migration does **not** define a
full baseline of every existing table — it ALTERs the two tables that
gain columns in this step. Both legacy runtime-created DBs and any DB
that gets a baseline migration in a future step will end up with the
same ``enterprise_id``/``group_id`` shape.

What this migration does NOT do (deferred):

  - Read-path filtering by enterprise_id / group_id.
  - JWT or API-key claim-based scope assignment.
  - New ``tenants`` / ``enterprises`` / ``groups`` / ``humans`` /
    ``personas`` / ``teams`` tables.

See ``docs/plans/06-gap-analysis-deployed-vs-target.md`` (Section A and
step 2 of the Recommended build order) for the full plan.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_phase6_step1"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEFAULT_ENTERPRISE_ID = "default-enterprise"
DEFAULT_GROUP_ID = "default-group"


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _add_tenancy_columns(table_name: str) -> None:
    """Add enterprise_id and group_id to ``table_name`` if missing.

    Add as nullable, backfill, then enforce NOT NULL. SQLite needs the
    batch context to do the NOT NULL ALTER (it rebuilds the table).
    Postgres can do it in place; ``render_as_batch=True`` in env.py
    handles both.
    """
    bind = op.get_bind()
    if not _table_exists(bind, table_name):
        # The runtime store creates each table lazily on first use.
        # If the migration runs against a DB where this table doesn't
        # exist yet, there's nothing to alter — the runtime will create
        # it with the new columns already in place via _SCHEMA_SQL /
        # USERS_TABLE_SQL.
        return

    existing = _column_names(bind, table_name)

    if "enterprise_id" not in existing:
        op.add_column(
            table_name,
            sa.Column(
                "enterprise_id",
                sa.Text(),
                nullable=True,
                server_default=DEFAULT_ENTERPRISE_ID,
            ),
        )
    if "group_id" not in existing:
        op.add_column(
            table_name,
            sa.Column(
                "group_id",
                sa.Text(),
                nullable=True,
                server_default=DEFAULT_GROUP_ID,
            ),
        )

    # Inline backfill — required for SQLite, where ALTER TABLE ADD COLUMN
    # ... DEFAULT only stamps the default on subsequent inserts and
    # leaves pre-existing rows NULL. Postgres backfills automatically
    # but the UPDATE is a no-op in that case.
    op.execute(
        sa.text(f"UPDATE {table_name} SET enterprise_id = :ent WHERE enterprise_id IS NULL").bindparams(
            ent=DEFAULT_ENTERPRISE_ID
        )
    )
    op.execute(
        sa.text(f"UPDATE {table_name} SET group_id = :grp WHERE group_id IS NULL").bindparams(grp=DEFAULT_GROUP_ID)
    )

    # Promote to NOT NULL now that every row has a value.
    with op.batch_alter_table(table_name) as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.Text(),
            nullable=False,
            existing_server_default=DEFAULT_ENTERPRISE_ID,
        )
        batch.alter_column(
            "group_id",
            existing_type=sa.Text(),
            nullable=False,
            existing_server_default=DEFAULT_GROUP_ID,
        )


def _drop_tenancy_columns(table_name: str) -> None:
    bind = op.get_bind()
    if not _table_exists(bind, table_name):
        return
    existing = _column_names(bind, table_name)
    with op.batch_alter_table(table_name) as batch:
        if "group_id" in existing:
            batch.drop_column("group_id")
        if "enterprise_id" in existing:
            batch.drop_column("enterprise_id")


def upgrade() -> None:
    """Add additive tenancy columns + backfill defaults."""
    _add_tenancy_columns("knowledge_units")
    _add_tenancy_columns("users")


def downgrade() -> None:
    """Drop tenancy columns.

    Used for migration tests; production rollbacks should prefer leaving
    the columns in place since they are additive.
    """
    _drop_tenancy_columns("users")
    _drop_tenancy_columns("knowledge_units")
