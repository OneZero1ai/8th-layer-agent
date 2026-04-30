"""Phase 6 step 2: cross-group flag + cross-Enterprise consent + audit.

Revision ID: 0002_phase6_step2
Revises: 0001_phase6_step1
Create Date: 2026-04-30

Adds the schema delta required by /aigrp/forward-query:

  - ``knowledge_units.cross_group_allowed`` — per-KU opt-in flag for
    sharing across sibling Groups inside the same Enterprise. Defaults
    to 0 so no existing row implicitly opens up.
  - ``cross_enterprise_consents`` table — admin-signed records that
    permit a foreign Enterprise's L2 to receive forward-query results
    from this L2 under a stated policy (``summary_only`` in v1).
  - ``cross_l2_audit`` table — append-only log of every
    /aigrp/forward-query call, including denied probes.

Mirrors the runtime ``ensure_xgroup_consent_schema`` helper so the
Alembic-first DB and the legacy-runtime DB converge on the same shape.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_phase6_step2"
down_revision: str | Sequence[str] | None = "0001_phase6_step1"
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
    """Add cross_group_allowed column + create consent and audit tables."""
    bind = op.get_bind()

    # 1. cross_group_allowed on knowledge_units (idempotent).
    if _table_exists(bind, "knowledge_units"):
        existing = _column_names(bind, "knowledge_units")
        if "cross_group_allowed" not in existing:
            op.add_column(
                "knowledge_units",
                sa.Column(
                    "cross_group_allowed",
                    sa.Integer(),
                    nullable=True,
                    server_default=sa.text("0"),
                ),
            )
            # Backfill — same SQLite ALTER quirk as Phase 6 step 1.
            op.execute(
                sa.text(
                    "UPDATE knowledge_units SET cross_group_allowed = 0 "
                    "WHERE cross_group_allowed IS NULL"
                )
            )
            with op.batch_alter_table("knowledge_units") as batch:
                batch.alter_column(
                    "cross_group_allowed",
                    existing_type=sa.Integer(),
                    nullable=False,
                    existing_server_default=sa.text("0"),
                )

    # 2. cross_enterprise_consents — admin-signed sharing records.
    if not _table_exists(bind, "cross_enterprise_consents"):
        op.create_table(
            "cross_enterprise_consents",
            sa.Column("consent_id", sa.Text(), primary_key=True),
            sa.Column("requester_enterprise", sa.Text(), nullable=False),
            sa.Column("responder_enterprise", sa.Text(), nullable=False),
            sa.Column("requester_group", sa.Text(), nullable=True),
            sa.Column("responder_group", sa.Text(), nullable=True),
            sa.Column("policy", sa.Text(), nullable=False),
            sa.Column("signed_by_admin", sa.Text(), nullable=False),
            sa.Column("signed_at", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=True),
            sa.Column("audit_log_id", sa.Text(), nullable=False, unique=True),
        )
        op.create_index(
            "idx_xent_consents_pair",
            "cross_enterprise_consents",
            ["requester_enterprise", "responder_enterprise"],
        )

    # 3. cross_l2_audit — append-only audit log.
    if not _table_exists(bind, "cross_l2_audit"):
        op.create_table(
            "cross_l2_audit",
            sa.Column("audit_id", sa.Text(), primary_key=True),
            sa.Column("ts", sa.Text(), nullable=False),
            sa.Column("requester_l2_id", sa.Text(), nullable=True),
            sa.Column("requester_enterprise", sa.Text(), nullable=True),
            sa.Column("requester_group", sa.Text(), nullable=True),
            sa.Column("requester_persona", sa.Text(), nullable=True),
            sa.Column("responder_l2_id", sa.Text(), nullable=True),
            sa.Column("responder_enterprise", sa.Text(), nullable=True),
            sa.Column("responder_group", sa.Text(), nullable=True),
            sa.Column("policy_applied", sa.Text(), nullable=True),
            sa.Column("result_count", sa.Integer(), nullable=True),
            sa.Column("consent_id", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    """Drop the new tables and column.

    Used by migration tests; production rollbacks should prefer leaving
    the additive tables in place.
    """
    bind = op.get_bind()
    if _table_exists(bind, "cross_l2_audit"):
        op.drop_table("cross_l2_audit")
    if _table_exists(bind, "cross_enterprise_consents"):
        op.drop_index(
            "idx_xent_consents_pair",
            table_name="cross_enterprise_consents",
        )
        op.drop_table("cross_enterprise_consents")
    if _table_exists(bind, "knowledge_units"):
        existing = _column_names(bind, "knowledge_units")
        if "cross_group_allowed" in existing:
            with op.batch_alter_table("knowledge_units") as batch:
                batch.drop_column("cross_group_allowed")
