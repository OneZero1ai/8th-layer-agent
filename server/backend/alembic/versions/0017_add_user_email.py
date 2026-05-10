"""FO-1a: add ``email`` column to ``users`` + conditional unique index.

Revision ID: 0017_add_user_email
Revises: 0016_xgroup_consent
Create Date: 2026-05-10

Founder Onboarding (#191, phase 1a). Adds an additive ``email`` column
to ``users`` for passkey enrollment. The column is nullable — existing
non-passkey users carry NULL — and uniqueness is enforced by a
*conditional* unique index (``WHERE email IS NOT NULL``) so multiple
NULLs do not collide.

Why a partial index rather than a full UNIQUE constraint: the legacy
users-table seed path (CLI / SDK auth flows that pre-date the email
column) never wrote an email; making the column UNIQUE would either
require a non-NULL DEFAULT (which we don't want — placeholder emails
are worse than NULL) or accept that all existing rows collide on
empty-string. Partial-unique sidesteps both.

Idempotency mirrors 0015 — guard with ``_column_names`` for the column
add and an inspector check for the index. Re-run is a no-op.

# Downgrade

Drops the index and column. Note: the conditional-unique index
predicate (``WHERE email IS NOT NULL``) is part of the index DDL on
both SQLite and Postgres; ``op.drop_index`` removes it cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_add_user_email"
down_revision: str | Sequence[str] | None = "0016_xgroup_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _index_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    """Add ``users.email`` + conditional unique index."""
    bind = op.get_bind()

    if "email" not in _column_names(bind, "users"):
        op.add_column("users", sa.Column("email", sa.Text(), nullable=True))

    if "idx_users_email_unique" not in _index_names(bind, "users"):
        # Partial unique index: only enforces uniqueness for rows where
        # ``email IS NOT NULL``. Both SQLite and PostgreSQL support
        # ``CREATE UNIQUE INDEX ... WHERE`` with this exact syntax.
        op.create_index(
            "idx_users_email_unique",
            "users",
            ["email"],
            unique=True,
            sqlite_where=sa.text("email IS NOT NULL"),
            postgresql_where=sa.text("email IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop ``users.email`` + its conditional unique index."""
    bind = op.get_bind()

    if "idx_users_email_unique" in _index_names(bind, "users"):
        op.drop_index("idx_users_email_unique", table_name="users")

    if "email" in _column_names(bind, "users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("email")
