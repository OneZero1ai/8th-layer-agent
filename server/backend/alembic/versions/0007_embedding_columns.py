"""Phase 2 — port embedding columns on knowledge_units to Alembic.

Revision ID: 0007_embedding
Revises: 0006_directory_peerings
Create Date: 2026-05-02

Closes the last fork-delta gap on ``knowledge_units``: the
``embedding`` (BLOB) and ``embedding_model`` (TEXT) columns added by
the runtime ``ensure_embedding_columns`` path were never carried into
Alembic. After this migration the legacy ``RemoteStore._ensure_schema``
and the ``alembic upgrade head`` paths produce the same column set.

Idempotent: column-existence guard skips the ALTER on prod DBs that
already grew these columns at startup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_embedding"
down_revision: str | Sequence[str] | None = "0006_directory_peerings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Add ``embedding`` + ``embedding_model`` columns if missing."""
    bind = op.get_bind()
    existing = _column_names(bind, "knowledge_units")
    if "embedding" not in existing:
        op.add_column(
            "knowledge_units",
            sa.Column("embedding", sa.LargeBinary(), nullable=True),
        )
    if "embedding_model" not in existing:
        op.add_column(
            "knowledge_units",
            sa.Column("embedding_model", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    """Drop embedding columns. Used by tests; not for production."""
    bind = op.get_bind()
    existing = _column_names(bind, "knowledge_units")
    with op.batch_alter_table("knowledge_units") as batch:
        if "embedding_model" in existing:
            batch.drop_column("embedding_model")
        if "embedding" in existing:
            batch.drop_column("embedding")
