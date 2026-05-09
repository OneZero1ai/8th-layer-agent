"""Phase 1.0c — add ``pair_secret_ref`` column to ``aigrp_peers``.

Revision ID: 0015_phase_1_0c_aigrp_peers_pair_secret_ref
Revises: 0014_crosstalk_tables
Create Date: 2026-05-09

Decision 27 (per-L2 isolation) audit identified ``aigrp_peers`` as the
lone schema gap for the Phase 1.0c key-protocol substrate: every other
table is already correctly composite-scoped. Decision 28 §1.1 fixes the
canonical pair-name shape used as the SSM-key suffix and HKDF
``info``-string component:

    canonical_pair_name(a, b) = "aigrp-pair:" + min(a, b) + ":" + max(a, b)

This migration adds one column — ``pair_secret_ref`` — that records the
canonical pair-name for the (self_l2, peer_l2) pair represented by the
row. The column does NOT carry the secret value (the symmetric secret
lives in SSM, derived on-demand via HKDF from the per-Enterprise root
per Decision 28 §1.1/§1.2). It carries the *reference* — the lex-min
pair name — that callers use to look up / derive the pair-secret.

Why a column rather than computing on read: callers need a stable
identifier to log, cross-reference activity-log rows, surface in the
admin UI, and match against the SSM-key suffix during forensic review.
Storing the canonical name once at peer-registration time is cheaper
than re-canonicalizing on every read path and gives a hard audit
anchor that survives ``self_l2_id`` env-var drift.

# Backfill

The pre-existing rows have ``l2_id`` (peer L2 identity, e.g.
``acme/sga``) but no ``self_l2_id`` column — that identity is
implicitly the L2 owning the DB. We read it from the standard
``CQ_ENTERPRISE`` / ``CQ_GROUP`` env vars (the same source
``cq_server.aigrp.self_l2_id()`` uses at runtime), then compute the
canonical pair-name for each row.

If those env vars are unset at migration time (CI / fresh-empty-DB
runs), there are no peer rows to backfill — the table is empty —
so the missing identity is harmless. The migration only requires the
self-id when rows exist and the column is being added; in that
case unset env vars produce a deterministic placeholder
``unknown-self/unknown-self`` that the operator can later repair.
The column is NOT NULL so the placeholder lets the migration succeed
on legacy DBs where the env happens to be missing; runtime peer
upserts immediately overwrite with the correct value.

# Idempotency

Standard ``_column_names`` guard mirrors every migration in the chain.
Re-run is a no-op. Downgrade drops the column via SQLite batch-mode
table-recreate (``aigrp_peers`` has no FK constraints from other
tables, so the recreate is safe).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_phase_1_0c_aigrp_peers_pair_secret_ref"
down_revision: str | Sequence[str] | None = "0014_crosstalk_tables"
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


def _self_l2_id_from_env() -> str:
    """Mirror of ``cq_server.aigrp.self_l2_id()``.

    Kept inline to avoid importing the runtime module at migration
    time (Alembic env bootstrapping should not depend on application
    packages).
    """
    enterprise = os.environ.get("CQ_ENTERPRISE", "default-enterprise")
    group = os.environ.get("CQ_GROUP", "default")
    return f"{enterprise}/{group}"


def _canonical_pair_name(a: str, b: str) -> str:
    """Decision 28 §1.1 — lex-min canonical pair name.

    Both peers compute the same value without coordination because the
    sort is deterministic. ``a == b`` (self-loop) is structurally
    impossible for AIGRP peers and not handled here; the runtime
    rejects that case at peer-registration time.
    """
    lo, hi = sorted([a, b])
    return f"aigrp-pair:{lo}:{hi}"


def upgrade() -> None:
    """Add ``pair_secret_ref`` to ``aigrp_peers`` + backfill existing rows."""
    bind = op.get_bind()

    if not _table_exists(bind, "aigrp_peers"):
        # Table missing entirely — earlier migration was skipped or
        # downgraded past 0005. Nothing to do; runtime will create it
        # via the runtime _ensure_schema path with the new column shape
        # once the chain is re-applied.
        return

    existing = _column_names(bind, "aigrp_peers")
    if "pair_secret_ref" in existing:
        return

    # Add NOT NULL with ``server_default=''`` so SQLite's ADD COLUMN
    # constraint (a NOT NULL column with no DEFAULT cannot be added
    # to a non-empty table) is satisfied, AND so runtime INSERTs that
    # pre-date the companion app-code update (Phase 1.0b) keep
    # working — they fall back to the empty-string sentinel which the
    # backfill loop overwrites for existing rows. The empty string is
    # a deliberate sentinel: it is structurally distinct from any
    # canonical pair-name (which always starts with ``aigrp-pair:``)
    # so a forensic sweep can spot rows that need re-canonicalization
    # if the runtime hasn't been updated yet.
    op.add_column(
        "aigrp_peers",
        sa.Column(
            "pair_secret_ref",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )

    # Backfill: derive canonical pair-name from self_l2_id (env) and
    # peer's l2_id (column). Done in Python rather than SQL so the
    # canonicalization rule (lex-min sort + ``aigrp-pair:`` prefix)
    # lives in one place — the helper above.
    self_l2 = _self_l2_id_from_env()
    rows = bind.execute(sa.text("SELECT l2_id FROM aigrp_peers")).fetchall()
    for (peer_l2_id,) in rows:
        ref = _canonical_pair_name(self_l2, peer_l2_id)
        bind.execute(
            sa.text("UPDATE aigrp_peers SET pair_secret_ref = :ref WHERE l2_id = :l2_id"),
            {"ref": ref, "l2_id": peer_l2_id},
        )

    # Convenience index: lookups by pair-name for forensic review +
    # admin-console "show grants involving pair X" queries.
    op.create_index(
        "idx_aigrp_peers_pair_secret_ref",
        "aigrp_peers",
        ["pair_secret_ref"],
    )


def downgrade() -> None:
    """Drop the ``pair_secret_ref`` column + index."""
    bind = op.get_bind()
    if not _table_exists(bind, "aigrp_peers"):
        return

    existing = _column_names(bind, "aigrp_peers")
    if "pair_secret_ref" not in existing:
        return

    # Drop index first (safe even if missing — guard).
    inspector = sa.inspect(bind)
    idx_names = {idx["name"] for idx in inspector.get_indexes("aigrp_peers")}
    if "idx_aigrp_peers_pair_secret_ref" in idx_names:
        op.drop_index(
            "idx_aigrp_peers_pair_secret_ref",
            table_name="aigrp_peers",
        )

    with op.batch_alter_table("aigrp_peers") as batch_op:
        batch_op.drop_column("pair_secret_ref")
