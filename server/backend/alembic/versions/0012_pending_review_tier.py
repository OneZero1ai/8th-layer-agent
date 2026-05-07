"""Pending-review tier columns on knowledge_units (#103).

Revision ID: 0012_pending_review_tier
Revises: 0011_activity_log
Create Date: 2026-05-06

Adds the substrate for the L2-queued hard-finding review surface from
#103. ``/cq:reflect`` produces VIBE√-flagged candidates that need human
judgment before tier-promotion (sanitization concerns: credentials in
summary, PII in detail, etc.). Pre-fix, those candidates were either
silently dropped or pushed to private — both losing data and bypassing
the security review the VIBE√ classifier flagged. This migration ships
the columns and the state-machine values; ``cq_server.review`` and a
new ``GET /review/pending-review`` route layer use them.

State model (#103):

* Hard findings are submitted with ``status='pending_review'`` plus a
  reason string and a per-tenant TTL on ``pending_review_expires_at``.
* ``POST /review/{id}/approve`` transitions ``pending_review →
  approved`` (existing approve handler — same shape as the normal
  pending-queue approve).
* ``POST /review/{id}/reject`` transitions ``pending_review →
  dropped`` — the dropped status is *terminal* and distinct from
  ``rejected`` so we can distinguish "operator saw it and said no"
  from "lost data" if a downstream sweeper ever needs to.
* TTL sweeper transitions ``pending_review → dropped`` when
  ``pending_review_expires_at < now`` and no human has reviewed.

Why ``status``, not a new ``tier``: the cq SDK's ``Tier`` enum is
pinned from PyPI (cq-sdk~=0.9.1). Adding a new tier value would
require an SDK release; the ``status`` column is already L2-server-
local and was designed for exactly this kind of lifecycle marker.
KUs proposed via the hard-finding queue still land at ``tier=private``
on the storage axis; the lifecycle axis is ``status``.

# Schema additions

* ``pending_review_reason TEXT NULL`` — free-form reason from the
  reflect classifier ("credential-shaped substring", "PII detected",
  etc.). NULL on rows that aren't pending-review.
* ``pending_review_expires_at TEXT NULL`` — ISO-8601 with Z suffix.
  When ``status='pending_review'`` AND ``expires_at < now``, the TTL
  sweeper drops the row.

No CHECK constraint on the status column — adding one would need
batch-recreate, and the existing review code paths already use string
comparison rather than enum membership. Documentation and the
``/review/*`` handlers gate the value space.

# Idempotency

Standard column-existence guard pattern. Re-runs are no-ops. Downgrade
drops both columns; only used by tests.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_pending_review_tier"
down_revision: str | Sequence[str] | None = "0011_activity_log"
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
    """Add pending_review_reason + pending_review_expires_at to knowledge_units."""
    bind = op.get_bind()
    if not _table_exists(bind, "knowledge_units"):
        # The table is created by 0001_baseline; this migration runs on
        # a stamped DB that already has it. Defensive: skip rather than
        # error if the chain is somehow incomplete (matches the
        # idempotency guard in 0001_phase6_step1).
        return

    existing = _column_names(bind, "knowledge_units")
    with op.batch_alter_table("knowledge_units") as batch:
        if "pending_review_reason" not in existing:
            batch.add_column(
                sa.Column("pending_review_reason", sa.Text(), nullable=True)
            )
        if "pending_review_expires_at" not in existing:
            batch.add_column(
                sa.Column("pending_review_expires_at", sa.Text(), nullable=True)
            )


def downgrade() -> None:
    """Drop pending_review columns. Used by tests; not for production."""
    bind = op.get_bind()
    if not _table_exists(bind, "knowledge_units"):
        return

    existing = _column_names(bind, "knowledge_units")
    with op.batch_alter_table("knowledge_units") as batch:
        if "pending_review_expires_at" in existing:
            batch.drop_column("pending_review_expires_at")
        if "pending_review_reason" in existing:
            batch.drop_column("pending_review_reason")
