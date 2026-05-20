"""Decision 34 — ``transactional_suppression`` table (central mail service).

Revision ID: 0026_transactional_suppression
Revises: 0025_activity_log_aigrp_lookup_event
Create Date: 2026-05-20

Central transactional-mail service (agent#348 / Decision 34). The
control-plane cq-server runs an SES dispatcher for every L2; bounces
and complaints fan back via SNS → suppression writer → this table. The
send path checks the table before SES dispatch and returns 409 on a
hit.

Schema:

* ``address`` — recipient email, lowercased. PRIMARY KEY so a re-hit
  on the same address is idempotent. Suppression is cross-category by
  design: once you bounce on any send, no further sends go out from
  any L2.
* ``reason`` — short tag (e.g. ``hard_bounce_2026-05-15``,
  ``complaint_2026-05-15``). Free-form; the writer composes it from
  the SNS event type + date.
* ``suppressed_at`` — ISO 8601 timestamp, matches the convention used
  by the rest of the schema (``users.created_at``, ``invites.*``, …).
* ``source_event_id`` — the SNS ``MessageId`` of the bounce /
  complaint event that drove the insert. Lets us reconcile against
  the SES → SNS audit trail when investigating.

# Idempotency

PRIMARY KEY on ``address`` means a second insert for the same address
is a no-op when the writer uses ``INSERT … ON CONFLICT DO NOTHING``.
The first reason wins; subsequent bounces don't overwrite (the address
is already blocked, so the diagnostic value of an updated reason is
low).

# Why not soft-delete

Unsuppression is a deliberate operator action (e.g. customer-reported
false-positive after their mailbox rebound). A ``suppressed_at``
column with no ``unsuppressed_at`` partner keeps the table append-only
in the happy path; un-suppressing is ``DELETE FROM
transactional_suppression WHERE address = ?`` and an audit-log entry,
not a tombstone. Mass un-suppression (e.g. after a domain-wide bounce
storm) is rare enough that the runbook delta isn't worth a schema
column.

# Chain note

Sits on top of 0025; no upstream dependency on FO-* migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_transactional_suppression"
down_revision: str | Sequence[str] | None = "0025_activity_log_aigrp_lookup_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``transactional_suppression`` table."""
    bind = op.get_bind()

    if _table_exists(bind, "transactional_suppression"):
        return

    op.create_table(
        "transactional_suppression",
        sa.Column("address", sa.Text(), primary_key=True, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("suppressed_at", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=True),
    )
    # Index on suppressed_at — operator queries "what bounced in the
    # last 24h?" are common enough to warrant the index. address is
    # the PK so equality lookups (send-path check) are already fast.
    op.create_index(
        "idx_transactional_suppression_suppressed_at",
        "transactional_suppression",
        ["suppressed_at"],
    )


def downgrade() -> None:
    """Drop the index and table."""
    bind = op.get_bind()
    if not _table_exists(bind, "transactional_suppression"):
        return

    inspector = sa.inspect(bind)
    idx_names = {idx["name"] for idx in inspector.get_indexes("transactional_suppression")}
    if "idx_transactional_suppression_suppressed_at" in idx_names:
        op.drop_index(
            "idx_transactional_suppression_suppressed_at",
            table_name="transactional_suppression",
        )
    op.drop_table("transactional_suppression")
