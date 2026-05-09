r"""Reflect submissions — server-side batch-reflect contract surface (#67).

Revision ID: 0010_reflect_submissions
Revises: 0009_reputation_roots
Create Date: 2026-05-04

Implements the persistence layer behind the frozen batch-reflect contract
at ``crosstalk-enterprise/docs/specs/batch-reflect-contract.md`` v1
(2026-04-30). One row per ``POST /api/v1/reflect/submit`` call. The
batch worker reads/writes the same table to drive Anthropic Batch
dispatch + result ingest; this migration ships the schema only.

State machine column values (locked enum, mirrors contract §"State machine"):

    queued -> batching -> polling -> complete
       \         |          |
        +-> failed <- failed <-+

Indexes:

  - ``(session_id, submitted_at DESC)`` — drives ``GET /reflect/last``
    and the per-session-key rate-limit count.
  - ``(session_id, context_hash, submitted_at)`` — drives the 30-min
    dedup lookup on submit.
  - ``(state, anthropic_batch_id)`` — drives the worker's R6 startup
    recovery scan: non-terminal rows with a populated batch id.

The schema is the union of contract §"Implementation notes" and the
issue #67 acceptance criteria. Cost columns are intentionally NOT
present — cost is computed off ``input_tokens`` server-internally and
not exposed on the wire (see contract §"Telemetry exposure").
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_reflect_submissions"
down_revision: str | Sequence[str] | None = "0009_reputation_roots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the reflect_submissions table + supporting indexes."""
    bind = op.get_bind()

    if not _table_exists(bind, "reflect_submissions"):
        op.create_table(
            "reflect_submissions",
            sa.Column("id", sa.Text(), primary_key=True),  # sub_<ULID>
            sa.Column("session_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("group_id", sa.Text(), nullable=True),
            sa.Column("context_hash", sa.Text(), nullable=False),  # sha256(context)[:16]
            sa.Column("state", sa.Text(), nullable=False),  # queued|batching|polling|complete|failed
            sa.Column("anthropic_batch_id", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("candidates_proposed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_confirmed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_excluded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_deduped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),  # locked-enum string when state=failed
            sa.Column("mode", sa.Text(), nullable=False, server_default="nightly"),
            sa.Column("max_candidates", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("since_ts", sa.Text(), nullable=True),
            sa.Column("submitted_at", sa.Text(), nullable=False),
            sa.Column("started_at", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.Text(), nullable=True),
        )
        op.create_index(
            "idx_reflect_submissions_session_submitted",
            "reflect_submissions",
            ["session_id", "submitted_at"],
        )
        op.create_index(
            "idx_reflect_submissions_dedup",
            "reflect_submissions",
            ["session_id", "context_hash", "submitted_at"],
        )
        op.create_index(
            "idx_reflect_submissions_state_batch",
            "reflect_submissions",
            ["state", "anthropic_batch_id"],
        )


def downgrade() -> None:
    """Drop the reflect_submissions table. Used by tests; not for production."""
    bind = op.get_bind()
    if _table_exists(bind, "reflect_submissions"):
        op.drop_index("idx_reflect_submissions_state_batch", table_name="reflect_submissions")
        op.drop_index("idx_reflect_submissions_dedup", table_name="reflect_submissions")
        op.drop_index(
            "idx_reflect_submissions_session_submitted",
            table_name="reflect_submissions",
        )
        op.drop_table("reflect_submissions")
