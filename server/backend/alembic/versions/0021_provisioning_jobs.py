"""FO-2-backend: ``provisioning_jobs`` table.

Revision ID: 0021_provisioning_jobs
Revises: 0020_l2_brand
Create Date: 2026-05-12

Enterprise Provisioning Service (FO-2, Decision 31). Stores the async
provisioning job state machine rows — one row per POST /api/v1/enterprises
call.

# Schema notes

``job_id`` is a ``prov_<26-char-ULID>`` primary key (TEXT). ULIDs are
lexicographically sortable on insert order and unguessable — meets
Decision 31 §Authentication which requires "anonymous but unguessable"
job IDs. No integer autoincrement because job IDs are generated in the
application layer.

``enterprise_id`` is the enterprise slug string supplied by the caller.
Checked for uniqueness by the application before inserting (no DB
UNIQUE here because we want the application to return SLUG_TAKEN 409
rather than a constraint error — cleaner UI message).

``status`` mirrors the Decision 31 state machine strings:
  PROVISIONING → KEY_MINT_IN_PROGRESS → DIRECTORY_REGISTER_IN_PROGRESS
  → DNS_PROVISION_IN_PROGRESS → L2_STANDUP_IN_PROGRESS
  → ADMIN_INVITE_SENT → COMPLETED | FAILED

``phase`` is the integer phase number (1–6) for progress_pct mapping.
NULL until the background task advances to phase 1.

``ip_hash`` is sha256(client_ip) — never the raw IP, stored only for
rate-limit lookups. It is NOT personally identifiable at rest.

``result_json`` is a nullable TEXT JSON blob; populated only on
COMPLETED. Consumers parse it as a dict.

# Idempotency

Standard ``_table_exists`` guard mirrors every migration in the chain.
Re-run is a no-op. Downgrade drops the table.

# Chain note

Sits on top of FO-1d's ``0020_l2_brand``. Head is ``0021_provisioning_jobs``
after this migration; ``cq_server.migrations.HEAD_REVISION`` is updated
in the same PR.

Parallel work note: AS-1 takes migration 0022. No conflict expected
because they branch from the same 0020_l2_brand head.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_provisioning_jobs"
down_revision: str | Sequence[str] | None = "0020_l2_brand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``provisioning_jobs`` table."""
    bind = op.get_bind()

    if _table_exists(bind, "provisioning_jobs"):
        return

    op.create_table(
        "provisioning_jobs",
        # ``prov_<26-char-ULID>`` — application-generated, unguessable.
        sa.Column("job_id", sa.Text(), primary_key=True),
        # Enterprise slug — set at job creation; the slug the caller
        # requested. Application checks uniqueness before insert.
        sa.Column("enterprise_id", sa.Text(), nullable=False),
        # State-machine status string (Decision 31 §Phases).
        sa.Column("status", sa.Text(), nullable=False, default="PROVISIONING"),
        # Phase number (1–6); NULL until background task first advances.
        sa.Column("phase", sa.Integer(), nullable=True),
        # ISO-8601 UTC timestamps matching the convention in users / invites.
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        # Error message on FAILED; NULL otherwise.
        sa.Column("error", sa.Text(), nullable=True),
        # Result JSON blob on COMPLETED; NULL otherwise.
        sa.Column("result_json", sa.Text(), nullable=True),
        # sha256(client_ip) — rate-limit key only; never raw IP.
        sa.Column("ip_hash", sa.Text(), nullable=False, default=""),
    )

    # Index for rate-limit lookups: WHERE ip_hash = ? AND started_at >= ?
    op.create_index(
        "idx_provisioning_jobs_ip_hash_started_at",
        "provisioning_jobs",
        ["ip_hash", "started_at"],
    )

    # Index for enterprise uniqueness checks: WHERE enterprise_id = ?
    op.create_index(
        "idx_provisioning_jobs_enterprise_id",
        "provisioning_jobs",
        ["enterprise_id"],
    )


def downgrade() -> None:
    """Drop the table and its indexes."""
    bind = op.get_bind()
    if not _table_exists(bind, "provisioning_jobs"):
        return
    op.drop_index("idx_provisioning_jobs_ip_hash_started_at", "provisioning_jobs")
    op.drop_index("idx_provisioning_jobs_enterprise_id", "provisioning_jobs")
    op.drop_table("provisioning_jobs")
