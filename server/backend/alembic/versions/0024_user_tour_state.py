"""Founder-tour: ``users.tour_state`` JSON-text column.

Revision ID: 0024_user_tour_state
Revises: 0023_persona_assignment_audit
Create Date: 2026-05-14

Per-user tour-completion state for the in-app onboarding walkthrough.
Stored as a TEXT column holding JSON of the shape:

    {
      "completed_at": "2026-05-14T11:55:00Z" | null,
      "current_step": 3,
      "dismissed_at": "2026-05-14T11:53:00Z" | null
    }

NULL row means "the tour has never been shown" — frontend auto-fires.
``completed_at`` set means the user finished it. ``dismissed_at`` set
means they X'd out before finishing — still don't auto-fire again, but
the `?` button can replay.

# Why a single JSON column (not a separate table)

The data is tiny, never queried across users, never joined. A relation
would be over-engineering. JSON in TEXT matches the convention used
elsewhere (``invites.target_l2_id`` etc.) — sqlite stores it as TEXT
and the Python side type-coerces via Pydantic on read.

# Idempotency

Standard ``_column_names`` guard mirrors every other additive migration
in the chain. Re-run is a no-op; downgrade drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_user_tour_state"
down_revision: str | Sequence[str] | None = "0023_persona_assignment_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Add ``users.tour_state`` (TEXT, nullable)."""
    bind = op.get_bind()
    if "tour_state" not in _column_names(bind, "users"):
        op.add_column("users", sa.Column("tour_state", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop ``users.tour_state``. Sqlite needs batch-mode for DROP COLUMN."""
    with op.batch_alter_table("users") as batch:
        batch.drop_column("tour_state")
