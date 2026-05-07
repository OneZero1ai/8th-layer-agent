r"""Activity log of record — corporate IP capture for the L2 (#108).

Revision ID: 0011_activity_log
Revises: 0010_reflect_submissions
Create Date: 2026-05-06

Phase 1 of #108: schema only. Stage 2 (instrumentation-engineer) wires
existing handlers to write rows; Stage 2 also ships the read endpoint at
``GET /api/v1/activity``. This migration creates the substrate.

The L2 is the **activity log of record** for a team's agents — every
``cq.query`` call, every KU lifecycle event, every crosstalk message,
every cross-Enterprise consult. The table is append-only and tenanted:

* All rows carry ``tenant_enterprise`` (NOT NULL). ``tenant_group`` is
  nullable because some system events (e.g. cross-tenant peering scans)
  don't have a single owning group.
* ``persona`` and ``human`` are nullable so background/system events
  (cron sweeps, automated reflect runs) can still log without faking
  an actor.
* ``payload`` and ``result_summary`` are JSON-as-text — same convention
  as every other store column (SQLite has no JSON type; PostgreSQL gets
  upgraded to ``jsonb`` in a future migration when the runtime moves
  off SQLite).
* ``event_type`` is constrained to the locked enum below by a CHECK
  constraint — same shape as ``reflect_submissions.state``. Adding a
  new event type requires a follow-up migration that drops + recreates
  the table on SQLite (``ALTER TABLE ... DROP CHECK`` is not supported)
  via Alembic's batch-recreate mode.

Locked event-type enum (#108 Schema sketch):

    query, propose, confirm, flag,
    review_start, review_resolve,
    crosstalk_send, crosstalk_reply, crosstalk_close,
    consult_open, consult_reply, consult_close

Indexes (per #108 acceptance):

* ``idx_activity_log_tenant_ts`` — ``(tenant_enterprise, tenant_group,
  ts)``: drives the dashboard "what is our team doing" queries and the
  retention sweeper's per-tenant scan.
* ``idx_activity_log_persona_ts`` — ``(persona, ts)``: drives the
  per-persona drill-down ("activity by human").
* ``idx_activity_log_event_type_ts`` — ``(event_type, ts)``: drives
  filtering by event class on the read endpoint.
* ``idx_activity_log_thread`` — ``(thread_or_chain_id)``: correlates
  events into workflows (consult thread, crosstalk chain, review
  cycle).

Indexes are declared with columns in ascending order for portability.
SQLite uses the same b-tree for ``ORDER BY ts DESC`` queries; the read
endpoint sets ``ORDER BY ts DESC`` and the planner picks up the index
for backward iteration.

# Retention

Default retention is **90 days**, configurable per Enterprise via
``activity_retention_config(enterprise_id, retention_days, updated_at)``.
Absence of a row means "use default 90 days" — operators only need to
write a row to override.

Cleanup runs out-of-band:

* In-server: a periodic asyncio task in ``app.py`` startup wakes daily
  and calls ``store.purge_activity_older_than(...)`` per Enterprise
  (Stage 2 of #108 wires this — schema only here).
* Lambda alternative: an EventBridge cron hits an admin endpoint that
  invokes the same store helper. Same SQL, different scheduler.

Cleanup is intentionally **not** triggered by the migration. Schema
changes never delete rows.

# Append-only invariant

Application code never UPDATEs or DELETEs except via the retention
sweeper, which deletes rows older than the per-Enterprise window. The
schema does not enforce this — it's a code-level invariant, validated
in ``test_activity_log.py``. PostgreSQL will eventually grow a
trigger to forbid UPDATEs; SQLite has no equivalent so the invariant
stays code-only.

# Idempotency

The ``_table_exists`` guard mirrors every other migration in this
chain (#305 baseline, #67 reflect, #99 reputation). A re-run is a
no-op. Downgrade drops both tables — used by tests; not for prod.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_activity_log"
down_revision: str | Sequence[str] | None = "0010_reflect_submissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Locked enum — must stay in sync with cq_server.activity.EVENT_TYPES.
# Adding a new value requires a new migration that uses Alembic batch
# recreate to swap the CHECK constraint.
_EVENT_TYPES = (
    "query",
    "propose",
    "confirm",
    "flag",
    "review_start",
    "review_resolve",
    "crosstalk_send",
    "crosstalk_reply",
    "crosstalk_close",
    "consult_open",
    "consult_reply",
    "consult_close",
)


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _check_clause() -> str:
    quoted = ", ".join(f"'{e}'" for e in _EVENT_TYPES)
    return f"event_type IN ({quoted})"


def upgrade() -> None:
    """Create the ``activity_log`` + ``activity_retention_config`` tables."""
    bind = op.get_bind()

    if not _table_exists(bind, "activity_log"):
        op.create_table(
            "activity_log",
            sa.Column("id", sa.Text(), primary_key=True),  # act_<26-char-ULID>
            sa.Column("ts", sa.Text(), nullable=False),  # ISO-8601 with Z suffix
            sa.Column("tenant_enterprise", sa.Text(), nullable=False),
            sa.Column("tenant_group", sa.Text(), nullable=True),
            sa.Column("persona", sa.Text(), nullable=True),
            sa.Column("human", sa.Text(), nullable=True),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("result_summary", sa.Text(), nullable=True),
            sa.Column("thread_or_chain_id", sa.Text(), nullable=True),
            sa.CheckConstraint(_check_clause(), name="ck_activity_log_event_type"),
        )
        op.create_index(
            "idx_activity_log_tenant_ts",
            "activity_log",
            ["tenant_enterprise", "tenant_group", "ts"],
        )
        op.create_index(
            "idx_activity_log_persona_ts",
            "activity_log",
            ["persona", "ts"],
        )
        op.create_index(
            "idx_activity_log_event_type_ts",
            "activity_log",
            ["event_type", "ts"],
        )
        op.create_index(
            "idx_activity_log_thread",
            "activity_log",
            ["thread_or_chain_id"],
        )

    if not _table_exists(bind, "activity_retention_config"):
        op.create_table(
            "activity_retention_config",
            sa.Column("enterprise_id", sa.Text(), primary_key=True),
            sa.Column(
                "retention_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("90"),
            ),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "retention_days > 0",
                name="ck_activity_retention_days_positive",
            ),
        )


def downgrade() -> None:
    """Drop activity-log tables. Used by tests; not for production."""
    bind = op.get_bind()
    if _table_exists(bind, "activity_retention_config"):
        op.drop_table("activity_retention_config")
    if _table_exists(bind, "activity_log"):
        op.drop_index("idx_activity_log_thread", table_name="activity_log")
        op.drop_index("idx_activity_log_event_type_ts", table_name="activity_log")
        op.drop_index("idx_activity_log_persona_ts", table_name="activity_log")
        op.drop_index("idx_activity_log_tenant_ts", table_name="activity_log")
        op.drop_table("activity_log")
