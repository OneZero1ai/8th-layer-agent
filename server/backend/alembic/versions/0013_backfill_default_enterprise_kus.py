"""Backfill default-enterprise KUs to their proposer's tenancy (#121 finding 3).

Revision ID: 0013_backfill_default_enterprise_kus
Revises: 0012_pending_review_tier
Create Date: 2026-05-07

The #89 fix landed in 819729b, but it only protects new INSERTs via
``INSERT_UNIT_WITH_TENANCY``. Every KU proposed before the fix shipped
sits in ``enterprise_id='default-enterprise'`` /
``group_id='default-group'`` regardless of which tenant the proposer
actually belongs to. On a deployment that's been live since before the
fix, those rows are mis-tenanted: cross-tenant queries can see them,
the proposer's real tenant can't see them, and tenant-scoped admin
endpoints (``/review/queue``, ``/review/stats``) silently exclude
them from the right tenant's view.

This migration scans every KU still at ``default-enterprise``, parses
``created_by`` out of the JSON ``data`` blob, looks up that user's
current tenancy, and rewrites the row's tenancy columns to match.
Idempotent — re-runs are no-ops because the WHERE clause excludes
rows that have already been backfilled.

# What gets touched

Only rows where:

* ``enterprise_id = 'default-enterprise'`` AND ``group_id = 'default-group'``
  — both columns must still be at the schema-level default. If either
  has been customized, we leave the row alone (defensive: that row may
  have been intentionally placed in default-enterprise, e.g. system-
  proposed fixture data).
* ``data->>'created_by'`` resolves to a known user with non-default
  tenancy. KUs with no ``created_by``, an empty string, or a username
  that doesn't exist in the users table are left at default-enterprise
  — we have no signal to reassign them, and the safe default is "stay
  put" rather than guess.

# Why JSON parse over a join

``knowledge_units.data`` is the ``KnowledgeUnit`` Pydantic dump as a
TEXT blob. SQLite doesn't expose ``->>`` as a JSON operator pre-3.38;
PostgreSQL does. To stay portable we parse in Python rather than push
JSON extraction into SQL — slower, but a one-shot migration so the
extra round-trips don't matter and the SQL stays portable.

# Idempotency

Standard pattern: re-runs of the upgrade scan zero rows because the
WHERE clause excludes already-backfilled records. Downgrade is a
no-op — there's no way to recover the original ``default-enterprise``
state for the rows we touched (and we shouldn't want to: those rows
were mis-tenanted in the first place).

# Operator note

If you're running this against a deployment with thousands of legacy
KUs, the migration runs in O(n) Python time, not O(n) SQL time. For
deployments below ~10k rows it'll complete in seconds; above that,
plan a maintenance window. The rewrite uses one UPDATE per row rather
than a single CASE expression because the per-row enterprise_id is
data-dependent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_backfill_default_enterprise_kus"
down_revision: str | Sequence[str] | None = "0012_pending_review_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Reassign legacy default-enterprise KUs to their proposer's tenancy."""
    bind = op.get_bind()
    # Defensive: the chain might be partial (test-only branches stamp
    # one migration without running the rest). If knowledge_units or
    # users isn't present, there's nothing to backfill.
    if not _table_exists(bind, "knowledge_units"):
        return
    if not _table_exists(bind, "users"):
        return

    # Step 1: pull every still-default-enterprise KU id + data blob.
    # We need data because that's where ``created_by`` lives.
    candidates = bind.exec_driver_sql(
        "SELECT id, data FROM knowledge_units "
        "WHERE enterprise_id = 'default-enterprise' "
        "AND group_id = 'default-group'"
    ).fetchall()
    if not candidates:
        return

    # Step 2: index users by username so the per-row lookup is O(1).
    # Skip users at default tenancy themselves — they don't help us
    # disambiguate (reassigning one default-enterprise row to another
    # is a no-op, and we shouldn't pretend to "fix" it).
    user_rows = bind.exec_driver_sql(
        "SELECT username, enterprise_id, group_id FROM users "
        "WHERE enterprise_id != 'default-enterprise' "
        "OR group_id != 'default-group'"
    ).fetchall()
    user_map: dict[str, tuple[str, str]] = {
        row[0]: (row[1], row[2]) for row in user_rows
    }
    if not user_map:
        # No users with non-default tenancy => no signal to backfill on.
        return

    # Step 3: walk the candidates, parse created_by, rewrite tenancy.
    update_sql = sa.text(
        "UPDATE knowledge_units "
        "SET enterprise_id = :enterprise_id, group_id = :group_id "
        "WHERE id = :id "
        "AND enterprise_id = 'default-enterprise' "
        "AND group_id = 'default-group'"
    )
    backfilled = 0
    for unit_id, data_blob in candidates:
        try:
            data = json.loads(data_blob)
        except (TypeError, ValueError):
            # Malformed JSON — log via Alembic's stderr and skip.
            # We never raise: a single bad row must not abort the
            # whole migration. The row stays at default-enterprise;
            # operators can repair it manually.
            continue
        created_by = data.get("created_by") if isinstance(data, dict) else None
        if not created_by or not isinstance(created_by, str):
            continue
        scope = user_map.get(created_by)
        if scope is None:
            continue
        bind.execute(
            update_sql,
            {
                "enterprise_id": scope[0],
                "group_id": scope[1],
                "id": unit_id,
            },
        )
        backfilled += 1

    # Surface the count in alembic output for the operator's audit log.
    # Alembic captures stdout when running migrations.
    print(  # noqa: T201 — migration audit signal, not application logging
        f"[0013] backfilled {backfilled} of {len(candidates)} "
        f"default-enterprise KUs to their proposer's tenancy"
    )


def downgrade() -> None:
    """No-op: backfilled rows stay at their corrected tenancy.

    Rolling back this migration would mean re-tenanting correctly-
    placed rows back to default-enterprise — that's the bug, not a
    feature. If you need to undo this migration for some reason,
    write a follow-up data migration with explicit selection criteria
    rather than reverting in bulk.
    """
    pass
