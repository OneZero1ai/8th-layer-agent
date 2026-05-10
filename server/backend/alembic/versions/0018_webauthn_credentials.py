"""FO-1a: ``webauthn_credentials`` table for passkey enrollment.

Revision ID: 0018_webauthn_credentials
Revises: 0017_add_user_email
Create Date: 2026-05-10

Founder Onboarding (#191, phase 1a). Stores one row per registered
WebAuthn credential — credential id, COSE-encoded public key, and the
authenticator's monotonic ``sign_count``. ``credential_id`` and
``public_key`` are bytes (BLOB on SQLite, BYTEA on PostgreSQL), the
shape py_webauthn returns from ``verify_registration_response``.

# Schema

* ``id`` — surrogate INT PK, sqlite_autoincrement matches baseline.
* ``user_id`` — FK to ``users.id`` with CASCADE (delete a user, their
  credentials go with them).
* ``credential_id`` — UNIQUE, the value the authenticator returns and
  the key into our lookup on assertion.
* ``public_key`` — COSE-encoded public key bytes.
* ``sign_count`` — monotonic counter; assertion verification requires
  a strictly-greater value (clone detection).
* ``transports`` — comma-separated transport hints (``usb``,
  ``ble``, ``nfc``, ``internal``); informational.
* ``aaguid`` — authenticator model identifier (16 bytes); informational
  but useful for revoking a known-compromised model.
* ``name`` — optional human-readable label (e.g. "DW's YubiKey 5C").
* ``created_at`` / ``last_used_at`` — ISO-8601 strings, matching the
  ``users`` / ``api_keys`` convention.

Index on ``user_id`` mirrors ``idx_api_keys_user`` for the same
"list-by-owner" query path the admin UI uses for API keys.

# Idempotency

Standard table-existence + index-existence guards (see 0015 / 0016)
so the migration is safe to re-run. Downgrade drops index then table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_webauthn_credentials"
down_revision: str | Sequence[str] | None = "0017_add_user_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create ``webauthn_credentials`` + supporting index."""
    bind = op.get_bind()

    if not _table_exists(bind, "webauthn_credentials"):
        op.create_table(
            "webauthn_credentials",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("credential_id", sa.LargeBinary(), nullable=False),
            sa.Column("public_key", sa.LargeBinary(), nullable=False),
            sa.Column(
                "sign_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("transports", sa.Text(), nullable=True),
            sa.Column("aaguid", sa.LargeBinary(), nullable=True),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("last_used_at", sa.Text(), nullable=True),
            sa.UniqueConstraint("credential_id", name="uq_webauthn_credentials_credential_id"),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                ondelete="CASCADE",
                name="fk_webauthn_credentials_user_id",
            ),
            # Match the baseline convention for INTEGER PK tables.
            sqlite_autoincrement=True,
        )
        op.create_index(
            "idx_webauthn_user",
            "webauthn_credentials",
            ["user_id"],
        )


def downgrade() -> None:
    """Drop ``webauthn_credentials`` + index."""
    bind = op.get_bind()
    if _table_exists(bind, "webauthn_credentials"):
        inspector = sa.inspect(bind)
        idx_names = {idx["name"] for idx in inspector.get_indexes("webauthn_credentials")}
        if "idx_webauthn_user" in idx_names:
            op.drop_index("idx_webauthn_user", table_name="webauthn_credentials")
        op.drop_table("webauthn_credentials")
