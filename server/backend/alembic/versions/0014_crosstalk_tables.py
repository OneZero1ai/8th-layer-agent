"""Crosstalk tables — L2-mediated inter-session messaging (#124).

Revision ID: 0014_crosstalk_tables
Revises: 0013_backfill_default_enterprise_kus
Create Date: 2026-05-07

Re-scoped 2026-05-07: L2-mediated crosstalk is a TeamDW MVP requirement,
not a Phase B "ship after first cutover" item. Reasons (from Plan 19 v4):

- **Visibility/audit** — legacy claude-mux SQLite crosstalk is invisible
  to the L2; activity log captures KU events but not inter-session
  messaging. Acme-style multi-operator deployments need this.
- **Multi-tenant isolation** — laptop-local SQLite has no enterprise/
  group scoping; messages between sessions can cross persona boundaries
  trivially. L2 routing enforces tenancy.
- **Cross-account routing (eventual)** — cross-Enterprise consults
  already flow through L2 peering envelopes (Phase 0/1). Crosstalk
  needs the same shape eventually; the L2 endpoint is the foundation.

This migration creates the substrate. Routes ship in
``cq_server/crosstalk_routes.py`` (companion to this migration);
claude-mux's ``crosstalk/src/server.js`` swaps to L2-as-source via
dwinter3/claude-mux#113 (companion client-side change).

# Schema

Two tables:

* ``crosstalk_threads`` — conversation metadata. Carries tenancy on
  every row (enterprise_id + group_id) the same way ``knowledge_units``
  does. Status is ``open`` or ``closed`` with optional close metadata.
  ``participants`` is JSON-encoded list of usernames; multi-party threads
  (Phase 7+ feature, not MVP) extend this naturally.

* ``crosstalk_messages`` — append-only message records. Each carries
  tenancy + thread reference + from/to attribution. ``read_at`` is
  nullable; populated when the recipient calls ``/inbox`` (mark-read
  semantics; Phase 7+ may add a separate seen-but-not-acked state).
  CASCADE on thread delete simplifies test cleanup; production never
  deletes threads.

# Indexes (per the 2026-05-07 design doc, plan 22)

* ``idx_crosstalk_threads_tenancy`` — drives "list threads visible to
  caller" queries, scoped by tenancy
* ``idx_crosstalk_threads_created_at`` — chronological pagination
* ``idx_crosstalk_messages_thread`` — thread message-list (most-common
  read path)
* ``idx_crosstalk_messages_inbox`` — caller's unread queue
  (``WHERE to_username = ? AND read_at IS NULL``)
* ``idx_crosstalk_messages_tenancy`` — tenancy-scoped scans + retention

# Tenancy enforcement

Routes pin both ``enterprise_id`` + ``group_id`` from the authenticated
caller's user row (mirror of ``propose_unit`` / #89 fix). Cross-Enterprise
crosstalk, when added later, flows through bilateral peering envelopes,
not these tables; this migration is intra-Enterprise scope only.

# Activity-log integration

Existing ``activity_log`` already has ``crosstalk_send``, ``crosstalk_reply``,
``crosstalk_close`` in its enum (#108 0011 migration). Routes wire
instrumentation; this migration adds no activity log work.

# Idempotency

``_table_exists`` guard mirrors every migration in the chain. Re-run is
a no-op. Downgrade drops both tables — used by tests; not for prod.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_crosstalk_tables"
down_revision: str | Sequence[str] | None = "0013_backfill_default_enterprise_kus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``crosstalk_threads`` + ``crosstalk_messages`` tables."""
    bind = op.get_bind()

    if not _table_exists(bind, "crosstalk_threads"):
        op.create_table(
            "crosstalk_threads",
            sa.Column("id", sa.Text(), primary_key=True),  # thread_<hex>
            sa.Column("subject", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'open'"),
            ),
            sa.Column("closed_at", sa.Text(), nullable=True),
            sa.Column("closed_by_username", sa.Text(), nullable=True),
            sa.Column("closed_reason", sa.Text(), nullable=True),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("group_id", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("created_by_username", sa.Text(), nullable=False),
            sa.Column(
                "participants",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.CheckConstraint(
                "status IN ('open', 'closed')",
                name="ck_crosstalk_threads_status",
            ),
        )
        op.create_index(
            "idx_crosstalk_threads_tenancy",
            "crosstalk_threads",
            ["enterprise_id", "group_id"],
        )
        op.create_index(
            "idx_crosstalk_threads_created_at",
            "crosstalk_threads",
            ["created_at"],
        )

    if not _table_exists(bind, "crosstalk_messages"):
        op.create_table(
            "crosstalk_messages",
            sa.Column("id", sa.Text(), primary_key=True),  # msg_<hex>
            sa.Column(
                "thread_id",
                sa.Text(),
                sa.ForeignKey("crosstalk_threads.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("from_username", sa.Text(), nullable=False),
            sa.Column("from_persona", sa.Text(), nullable=True),
            sa.Column("to_username", sa.Text(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("sent_at", sa.Text(), nullable=False),
            sa.Column("read_at", sa.Text(), nullable=True),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("group_id", sa.Text(), nullable=False),
        )
        op.create_index(
            "idx_crosstalk_messages_thread",
            "crosstalk_messages",
            ["thread_id", "sent_at"],
        )
        op.create_index(
            "idx_crosstalk_messages_inbox",
            "crosstalk_messages",
            ["to_username", "read_at"],
        )
        op.create_index(
            "idx_crosstalk_messages_tenancy",
            "crosstalk_messages",
            ["enterprise_id", "group_id"],
        )


def downgrade() -> None:
    """Drop crosstalk tables. Used by tests; not for production."""
    bind = op.get_bind()
    if _table_exists(bind, "crosstalk_messages"):
        op.drop_index("idx_crosstalk_messages_tenancy", table_name="crosstalk_messages")
        op.drop_index("idx_crosstalk_messages_inbox", table_name="crosstalk_messages")
        op.drop_index("idx_crosstalk_messages_thread", table_name="crosstalk_messages")
        op.drop_table("crosstalk_messages")
    if _table_exists(bind, "crosstalk_threads"):
        op.drop_index("idx_crosstalk_threads_created_at", table_name="crosstalk_threads")
        op.drop_index("idx_crosstalk_threads_tenancy", table_name="crosstalk_threads")
        op.drop_table("crosstalk_threads")
