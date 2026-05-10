"""FO-1b: ``invites`` table — magic-link invite minting + redeem state.

Revision ID: 0019_invites
Revises: 0018_webauthn_credentials
Create Date: 2026-05-10

Founder Onboarding (#191, phase 1b). Single-use, signed-JWT-backed
magic-link invites. The JWT is the bearer; this table records the
issuance + lifecycle so single-use is enforceable atomically (UPDATE …
WHERE claimed_at IS NULL pattern, gated by the ``UNIQUE(jti)`` index).

Schema mirrors the FO-1 spec on issue #191:

* ``jti`` carries the JWT id (uuid4 hex); ``UNIQUE`` because two
  invites can never share a jti — the canonical single-use anchor.
* ``role`` is ``'enterprise_admin' | 'l2_admin' | 'user'``; ``target_l2_id``
  is nullable because an enterprise-admin invite has no specific L2.
* ``issued_by`` / ``claimed_by`` reference ``users(id)``; the FK is the
  admin who minted the invite and the user who redeemed it (if any).
* Timestamps are TEXT (ISO 8601) per the existing convention used by
  ``users``, ``api_keys``, ``xgroup_consent``, etc.

Indexes:

* ``idx_invites_email`` — admin "list invites for foo@bar" lookups +
  duplicate-email guard at mint time (we *allow* duplicates, but the
  index makes the existence check cheap).
* ``idx_invites_status`` — composite over the three lifecycle columns
  used by ``GET /api/v1/admin/invites?status=...`` filtering.

# Idempotency

Standard ``_table_exists`` guard mirrors every migration in the chain.
Re-run is a no-op. Downgrade drops the indexes then the table.

# Chain note

Depends on FO-1a's ``0018_webauthn_credentials``. FO-1a lands first;
this migration sits on top. On a fresh DB without 0017/0018 the chain
will not resolve — see PR body for merge ordering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_invites"
down_revision: str | Sequence[str] | None = "0018_webauthn_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create the ``invites`` table + lookup indexes."""
    bind = op.get_bind()

    if _table_exists(bind, "invites"):
        return

    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # JWT id — the single-use anchor. UNIQUE so two invites can never
        # collide on jti even if mint() is called concurrently.
        sa.Column("jti", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        # Nullable for ``role='enterprise_admin'`` invites — no L2 yet.
        sa.Column("target_l2_id", sa.Text(), nullable=True),
        sa.Column(
            "issued_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("issued_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.Text(), nullable=True),
        sa.Column(
            "claimed_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.UniqueConstraint("jti", name="uq_invites_jti"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_invites_email", "invites", ["email"])
    op.create_index(
        "idx_invites_status",
        "invites",
        ["claimed_at", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    """Drop the indexes and table."""
    bind = op.get_bind()
    if not _table_exists(bind, "invites"):
        return

    inspector = sa.inspect(bind)
    idx_names = {idx["name"] for idx in inspector.get_indexes("invites")}
    if "idx_invites_status" in idx_names:
        op.drop_index("idx_invites_status", table_name="invites")
    if "idx_invites_email" in idx_names:
        op.drop_index("idx_invites_email", table_name="invites")
    op.drop_table("invites")
