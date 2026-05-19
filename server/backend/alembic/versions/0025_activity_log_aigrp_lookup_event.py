r"""Extend the activity_log event-type enum with ``aigrp_lookup`` (agent#284).

Revision ID: 0025_activity_log_aigrp_lookup_event
Revises: 0024_user_tour_state
Create Date: 2026-05-19

# Why

The L2 ``activity_log`` records *write* events (``propose``,
``review_resolve``, ``crosstalk_send`` …) but no *read* events. The
ambient-query path — ``POST /api/v1/aigrp/lookup``, fired by the harness
on every ``UserPromptSubmit`` — is the highest-volume read in the fleet
and is entirely unlogged today. Instrumenting it (agent#284) means the
L2 becomes a record of what agents *consult*, not only what they
*contribute* — the other half of the "L2 is the activity log of record"
positioning, and a hard requirement of the MVP acceptance gate (R4).

The KU-query endpoint (``GET /query`` / cross-L2 ``aigrp/forward-query``)
already reuses the existing ``query`` event value — no enum change
needed there. Only the AIGRP ambient-lookup needs a *new* value so it
can be filtered apart from explicit KU queries on the read endpoint.

# What this migration does

``activity_log.event_type`` is gated by the ``ck_activity_log_event_type``
CHECK constraint created in ``0011_activity_log``. SQLite cannot
``ALTER TABLE ... DROP CONSTRAINT``; the locked-enum docstring in 0011
explicitly says adding a value "requires a new migration that uses
Alembic batch recreate to swap the CHECK constraint". This migration
does exactly that: ``batch_alter_table(recreate="always")`` rebuilds the
table with the new constraint clause, copying every existing row.

The new enum (must stay in sync with ``cq_server.activity.EVENT_TYPES``):

    query, propose, confirm, flag,
    review_start, review_resolve,
    crosstalk_send, crosstalk_reply, crosstalk_close,
    consult_open, consult_reply, consult_close,
    aigrp_lookup            <-- added here

# Index re-creation

``batch_alter_table`` with ``recreate="always"`` drops and rebuilds the
table; Alembic's batch context reflects and re-applies the four indexes
(``idx_activity_log_tenant_ts`` …) automatically as part of the copy, so
no explicit index DDL is needed here.

# Idempotency

``_constraint_allows`` inspects the live CHECK clause; if ``aigrp_lookup``
is already permitted (re-run, or a DB stamped past this revision) the
upgrade is a no-op. Downgrade rebuilds the table with the original
12-value clause — only used by tests; never run in production against a
table that already holds ``aigrp_lookup`` rows (the copy would violate
the narrowed constraint).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_activity_log_aigrp_lookup_event"
down_revision: str | Sequence[str] | None = "0024_user_tour_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Original 12-value enum from 0011_activity_log.
_EVENT_TYPES_BASE = (
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

# agent#284 adds the read-path ambient-lookup event.
_EVENT_TYPES_WITH_LOOKUP = (*_EVENT_TYPES_BASE, "aigrp_lookup")


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _check_clause(event_types: Sequence[str]) -> str:
    quoted = ", ".join(f"'{e}'" for e in event_types)
    return f"event_type IN ({quoted})"


def _constraint_allows(bind: sa.engine.Connection, value: str) -> bool:
    """Return True if the live activity_log CHECK clause permits ``value``.

    SQLite stores the CHECK clause verbatim in ``sqlite_master.sql``;
    a substring test on the quoted value is sufficient and avoids a
    fragile clause re-parse.
    """
    row = bind.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='activity_log'").fetchone()
    if row is None or row[0] is None:
        return False
    return f"'{value}'" in row[0]


def _swap_constraint(event_types: Sequence[str]) -> None:
    """Recreate ``activity_log`` with a fresh ``event_type`` CHECK clause.

    On ``recreate="always"`` Alembic's batch context *reflects* the
    table — including the existing ``ck_activity_log_event_type`` CHECK.
    Passing a new ``CheckConstraint`` via ``table_args`` would *add* a
    second constraint of the same name rather than replace it (SQLite
    permits duplicate constraint names), and the row copy would then
    have to satisfy both — the old, narrower clause would reject any new
    value. So the swap is two explicit ops inside the batch block:
    drop the reflected constraint, then create the new one. Both run
    against the temp table before the row copy.
    """
    with op.batch_alter_table("activity_log", recreate="always") as batch:
        batch.drop_constraint("ck_activity_log_event_type", type_="check")
        batch.create_check_constraint(
            "ck_activity_log_event_type",
            _check_clause(event_types),
        )


def upgrade() -> None:
    """Recreate activity_log with ``aigrp_lookup`` added to the CHECK enum."""
    bind = op.get_bind()
    if not _table_exists(bind, "activity_log"):
        # activity_log is created by 0011; this migration runs on a
        # stamped DB that already has it. Skip rather than error if the
        # chain is somehow incomplete (mirrors 0012's defensive guard).
        return
    if _constraint_allows(bind, "aigrp_lookup"):
        # Already extended (re-run / DB stamped past this revision).
        return
    _swap_constraint(_EVENT_TYPES_WITH_LOOKUP)


def downgrade() -> None:
    """Recreate activity_log with the original 12-value CHECK enum.

    Used by tests only. Never run in production against a table that
    already holds ``aigrp_lookup`` rows — the row copy would violate the
    narrowed constraint.
    """
    bind = op.get_bind()
    if not _table_exists(bind, "activity_log"):
        return
    if not _constraint_allows(bind, "aigrp_lookup"):
        # Already at the base enum.
        return
    _swap_constraint(_EVENT_TYPES_BASE)
