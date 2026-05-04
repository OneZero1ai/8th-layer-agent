"""Tenancy default-scope constants.

Post-PR-C (#105): the runtime ensure_* helpers and inline CREATE TABLE
strings that lived here are gone. Schema is owned by Alembic — see
``cq_server/migrations.py`` and ``cq_server/migrations/versions/*``.

Only the default-scope constants stay here because both runtime code (KU
insert) and Alembic migrations (legacy-row backfill) need to converge on
the same values.
"""

DEFAULT_ENTERPRISE_ID = "default-enterprise"
DEFAULT_GROUP_ID = "default-group"
