"""Phase 1.0b: xgroup_consent (active) + xgroup_consent_pending tables.

Revision ID: 0016_xgroup_consent
Revises: 0015_phase_1_0c_aigrp_peers_pair_secret_ref
Create Date: 2026-05-09

Adds the schema substrate for Decision 28 §2 — 2-of-2 admin co-signed
intra-Enterprise xgroup_consent grants.

Two tables:

* ``xgroup_consent`` — active (ratified) grants. Pinned signer pubkeys
  per Decision 28 §3.1 so admin-key rotation does not invalidate live
  grants. Mirrored to both L2s at ratify time (AIGRP fan-out).

* ``xgroup_consent_pending`` — first-signer state. Source L2 only;
  target L2 fetches via cross-L2 AIGRP read. Auto-expires at
  ``expires_at`` (7d default).

Indexes:

* ``idx_xgroup_consent_target`` — query path: "is there an active grant
  for source→target with this scope?" (cross-group read filter).
* ``idx_xgroup_consent_pending_target`` — pending-list pull from L2-B.

Idempotent CREATE TABLE matches the convention in 0002 / 0014 — a
runtime ``ensure_xgroup_consent_schema`` mirror could be added later if
needed; not required for v1 since migrations run on every L2 startup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_xgroup_consent"
down_revision: str | Sequence[str] | None = "0015_phase_1_0c_aigrp_peers_pair_secret_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """Create ``xgroup_consent`` + ``xgroup_consent_pending``."""
    bind = op.get_bind()

    if not _table_exists(bind, "xgroup_consent"):
        op.create_table(
            "xgroup_consent",
            sa.Column("grant_id", sa.Text(), primary_key=True),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("source_l2", sa.Text(), nullable=False),
            sa.Column("target_l2", sa.Text(), nullable=False),
            # JSON canonical (RFC 8785 JCS) text — exact bytes both
            # signers signed. Verifiers re-canonicalise the body dict
            # and check equality before mac/sig verify.
            sa.Column("body_canonical", sa.Text(), nullable=False),
            sa.Column("body_canonical_sha256_hex", sa.Text(), nullable=False),
            # Scope is denormalised (kind + JSON values) to make the
            # cross-group read filter cheap; body_canonical is the
            # source of truth.
            sa.Column("scope_kind", sa.Text(), nullable=False),
            sa.Column("scope_values_json", sa.Text(), nullable=False),
            # Pinned signer pubkeys (Decision 28 §3.1).
            sa.Column("signer_a_l2", sa.Text(), nullable=False),
            sa.Column("signer_a_pubkey_b64u", sa.Text(), nullable=False),
            sa.Column("signer_a_signature_b64u", sa.Text(), nullable=False),
            sa.Column("signer_b_l2", sa.Text(), nullable=False),
            sa.Column("signer_b_pubkey_b64u", sa.Text(), nullable=False),
            sa.Column("signer_b_signature_b64u", sa.Text(), nullable=False),
            # Recovery operator pubkey (Decision 28 §2.5).
            sa.Column("recovery_operator_pubkey_b64u", sa.Text(), nullable=False),
            # Lifecycle.
            sa.Column("issued_at", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("ratified_at", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
            sa.Column("revoked_at", sa.Text(), nullable=True),
            sa.Column("revoked_by_l2", sa.Text(), nullable=True),
            sa.Column("revoked_by_pubkey_b64u", sa.Text(), nullable=True),
            sa.Column("revoked_by_recovery", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("revoke_reason", sa.Text(), nullable=True),
            sa.Column("nonce_b64u", sa.Text(), nullable=False),
            sa.Column("version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        )
        op.create_index(
            "idx_xgroup_consent_target",
            "xgroup_consent",
            ["enterprise_id", "target_l2", "source_l2", "status"],
        )

    if not _table_exists(bind, "xgroup_consent_pending"):
        op.create_table(
            "xgroup_consent_pending",
            sa.Column("pending_id", sa.Text(), primary_key=True),
            sa.Column("enterprise_id", sa.Text(), nullable=False),
            sa.Column("source_l2", sa.Text(), nullable=False),
            sa.Column("target_l2", sa.Text(), nullable=False),
            sa.Column("body_canonical", sa.Text(), nullable=False),
            sa.Column("body_canonical_sha256_hex", sa.Text(), nullable=False),
            sa.Column("proposer_l2", sa.Text(), nullable=False),
            sa.Column("proposer_pubkey_b64u", sa.Text(), nullable=False),
            sa.Column("proposer_signature_b64u", sa.Text(), nullable=False),
            # Cosigner fields are populated by cosign(); ratify() requires
            # both to be non-null and re-verifies before promoting.
            sa.Column("cosigner_l2", sa.Text(), nullable=True),
            sa.Column("cosigner_pubkey_b64u", sa.Text(), nullable=True),
            sa.Column("cosigner_signature_b64u", sa.Text(), nullable=True),
            sa.Column("cosigned_at", sa.Text(), nullable=True),
            sa.Column("proposed_at", sa.Text(), nullable=False),
            # 7-day cosign-window default — Decision 28 §2.2.
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'proposed'"),
            ),
        )
        op.create_index(
            "idx_xgroup_consent_pending_target",
            "xgroup_consent_pending",
            ["enterprise_id", "target_l2", "status"],
        )


def downgrade() -> None:
    """Drop the new tables. Tests round-trip; production never downgrades."""
    bind = op.get_bind()
    if _table_exists(bind, "xgroup_consent_pending"):
        op.drop_index("idx_xgroup_consent_pending_target", table_name="xgroup_consent_pending")
        op.drop_table("xgroup_consent_pending")
    if _table_exists(bind, "xgroup_consent"):
        op.drop_index("idx_xgroup_consent_target", table_name="xgroup_consent")
        op.drop_table("xgroup_consent")
