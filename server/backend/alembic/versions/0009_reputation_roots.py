"""Reputation log v1-real — daily Merkle roots (task #108 sub-task 3).

Revision ID: 0009_reputation_roots
Revises: 0008_reputation
Create Date: 2026-05-03

Per [decision 13](crosstalk-enterprise/docs/decisions/13) §"daily root publish":
the L2 computes a SHA-256 Merkle tree over each UTC day's
reputation_events, signs the root with this L2's Ed25519 forward-sign
key, and persists to ``reputation_roots``. The same root is later
POST'd to the directory's ``/api/v1/directory/reputation/root`` endpoint
(sub-task 4) for cross-Enterprise verification.

Schema invariants:
- ``(enterprise_id, root_date)`` is unique — one root per Enterprise per
  day. Recomputing requires DELETE first.
- ``event_count`` is informational; the root is the hash, not the count.
- ``signature_b64u`` and ``signing_key_id`` mirror the columns in
  ``reputation_events`` — same key (the L2 forward-sign key per
  decision 21), same self-describing format.
- Empty days (no events): we still write a row with the zero-event
  Merkle root constant, so day-over-day roots form a continuous chain
  and the directory can detect a gap if a day's row is missing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_reputation_roots"
down_revision: str | Sequence[str] | None = "0008_reputation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the daily-Merkle-root table."""
    bind = op.get_bind()

    if not _table_exists(bind, "reputation_roots"):
        op.create_table(
            "reputation_roots",
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("root_date", sa.Text(), nullable=False),  # YYYY-MM-DD UTC
            sa.Column("event_count", sa.Integer(), nullable=False),
            sa.Column("merkle_root_hash", sa.Text(), nullable=False),  # sha256:<hex>
            sa.Column("first_event_id", sa.Text(), nullable=True),
            sa.Column("last_event_id", sa.Text(), nullable=True),
            sa.Column("signature_b64u", sa.Text(), nullable=True),
            sa.Column("signing_key_id", sa.Text(), nullable=True),
            sa.Column("computed_at", sa.Text(), nullable=False),
            sa.Column("published_to_directory_at", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("enterprise_id", "root_date"),
        )
        op.create_index(
            "idx_reputation_roots_published",
            "reputation_roots",
            ["published_to_directory_at"],
        )


def downgrade() -> None:
    """Drop the roots table. Used by tests; not for production."""
    bind = op.get_bind()
    if _table_exists(bind, "reputation_roots"):
        op.drop_index(
            "idx_reputation_roots_published", table_name="reputation_roots"
        )
        op.drop_table("reputation_roots")
