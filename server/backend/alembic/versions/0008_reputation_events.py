"""Reputation log v1 — append-only hash chain (task #99).

Revision ID: 0008_reputation
Revises: 0007_embedding
Create Date: 2026-05-02

Per [decision 13](crosstalk-enterprise/docs/decisions/13) and
[reputation-v1 spec](crosstalk-enterprise/docs/specs/reputation-v1.md),
the L2 maintains a per-Enterprise append-only hash chain of
reputation-relevant events:

  - ``consult.closed``  — when an L3 consult thread closes
  - ``ku.event``        — KU lifecycle (proposed/approved/rejected/confirmed/flagged)
  - ``peer.heartbeat``  — AIGRP peer-poll convergence

Each row commits to the hash of the previous row via ``prev_event_hash``
so any tampering breaks chain verification. ``signature_b64u`` and
``signing_key_id`` are nullable in v1-alpha (this migration);
Ed25519 signing lands in the v1 follow-up that ships the daily Merkle
root publish path. Schema is forward-compatible — no further migration
needed once signing turns on.

``reputation_chain_meta`` is a single-row coordination table holding
the ``last_event_hash`` so a writer doesn't have to scan the chain
on every insert. v1 single-leader / single-L2; multi-L2 leadership
handover (decision 13 §"sibling L2s share one logical chain") lands
later via the AIGRP lease.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_reputation"
down_revision: str | Sequence[str] | None = "0007_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the reputation event log + chain-meta table."""
    bind = op.get_bind()

    if not _table_exists(bind, "reputation_events"):
        op.create_table(
            "reputation_events",
            sa.Column("event_id", sa.Text(), primary_key=True),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("l2_id", sa.Text(), nullable=False),
            sa.Column("ts", sa.Text(), nullable=False),
            sa.Column("prev_event_hash", sa.Text(), nullable=False),
            sa.Column("payload_canonical", sa.Text(), nullable=False),
            sa.Column("payload_hash", sa.Text(), nullable=False),
            sa.Column("signature_b64u", sa.Text(), nullable=True),
            sa.Column("signing_key_id", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.Text(),
                nullable=False,
            ),
        )
        op.create_index(
            "idx_reputation_events_enterprise_ts",
            "reputation_events",
            ["enterprise_id", "ts"],
        )
        op.create_index(
            "idx_reputation_events_type_ts",
            "reputation_events",
            ["event_type", "ts"],
        )

    if not _table_exists(bind, "reputation_chain_meta"):
        op.create_table(
            "reputation_chain_meta",
            sa.Column("enterprise_id", sa.Text(), primary_key=True),
            sa.Column("last_event_id", sa.Text(), nullable=True),
            sa.Column("last_event_hash", sa.Text(), nullable=False),
            sa.Column(
                "last_root_published_day",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.Text(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    """Drop reputation tables. Used by tests; not for production."""
    bind = op.get_bind()
    if _table_exists(bind, "reputation_chain_meta"):
        op.drop_table("reputation_chain_meta")
    if _table_exists(bind, "reputation_events"):
        op.drop_index(
            "idx_reputation_events_type_ts", table_name="reputation_events"
        )
        op.drop_index(
            "idx_reputation_events_enterprise_ts", table_name="reputation_events"
        )
        op.drop_table("reputation_events")
