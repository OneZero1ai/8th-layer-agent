"""FO-1d: ``l2_brand`` single-row table — per-L2 brand overrides.

Revision ID: 0020_l2_brand
Revises: 0019_invites
Create Date: 2026-05-10

Decision 30 — three-tier branding hierarchy (platform / enterprise / L2).
This migration lands the L2-tier storage. Platform defaults live in code
(``cq_server.theme`` constants); Enterprise overrides come from the
directory record (V1 may stub them); L2 overrides come from this table.

# Single-row enforcement

There is exactly one L2 per L2-host process — the ``CQ_GROUP`` env pins it.
The brand row therefore holds (label, subaccent_hex, hero_motif) for *this*
L2 only, never a list. We enforce that with ``CHECK (id = 1)`` so any
``INSERT`` that doesn't use ``id = 1`` fails fast at the DB layer; readers
can issue ``SELECT * FROM l2_brand WHERE id = 1`` without scanning.

The table is *initially empty*. The resolver in ``cq_server.theme`` returns
the L2 layer with ``label = group_id`` (default to the env-pinned group)
and the optional fields null when the row is absent. The Theming admin
form (separate epic AS-5) is what populates the row; FO-1d ships only the
schema + read path.

# Why TEXT

ISO-8601 ``updated_at`` and free-form override values match the existing
schema convention (``users``, ``api_keys``, ``invites``) — sqlite stores
TEXT and the Pydantic models on the read side type-coerce.

# Idempotency

Standard ``_table_exists`` guard mirrors every migration in the chain.
Re-run is a no-op. Downgrade drops the table.

# Chain note

Sits on top of FO-1c's ``0019_invites``. After this migration lands, head
is ``0020_l2_brand``; ``cq_server.migrations.HEAD_REVISION`` is bumped in
the same PR.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_l2_brand"
down_revision: str | Sequence[str] | None = "0019_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the single-row ``l2_brand`` table."""
    bind = op.get_bind()

    if _table_exists(bind, "l2_brand"):
        return

    op.create_table(
        "l2_brand",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Override of the L2 short label (defaults to ``group_id`` at read
        # time; only stored here when an admin has explicitly customised).
        sa.Column("l2_label", sa.Text(), nullable=True),
        # Optional accent hex (#rrggbb, validated by the API on write).
        sa.Column("subaccent_hex", sa.Text(), nullable=True),
        # Catalog identifier for the hero motif — see Decision 30
        # "Open question 3" recommendation: V1 ships 4 gradient names
        # ("gradient.cyan-violet" etc.). Storage is opaque; the resolver
        # just returns it.
        sa.Column("hero_motif", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        # Single-row anchor: only id=1 is ever a valid row. Coupled with
        # the resolver's ``WHERE id = 1`` read, this collapses the table
        # to "exists or doesn't" without a separate flag.
        sa.CheckConstraint("id = 1", name="ck_l2_brand_single_row"),
        # Hex-shape constraint at the storage layer (defense in depth;
        # 8l-reviewer MEDIUM 1 on PR #219). The AS-5 admin write path
        # will also validate, but a typo'd direct UPDATE or a future
        # schema bug shouldn't be able to push a malformed value
        # through to the React `setProperty` call.
        sa.CheckConstraint(
            "subaccent_hex IS NULL OR "
            "subaccent_hex GLOB '#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]'",
            name="ck_l2_brand_subaccent_hex_shape",
        ),
    )


def downgrade() -> None:
    """Drop the table."""
    bind = op.get_bind()
    if not _table_exists(bind, "l2_brand"):
        return
    op.drop_table("l2_brand")
