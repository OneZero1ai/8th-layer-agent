"""Database schema definitions and migration logic."""

import sqlite3

# Phase 6 step 1 — additive tenancy columns. Defaults defined here so that
# both runtime-created DBs (via _ensure_schema) and Alembic-migrated DBs
# converge on the same scope for legacy rows. See
# docs/plans/06-gap-analysis-deployed-vs-target.md (Section A).
DEFAULT_ENTERPRISE_ID = "default-enterprise"
DEFAULT_GROUP_ID = "default-group"

_REVIEW_COLUMN_STATEMENTS = [
    "ALTER TABLE knowledge_units ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE knowledge_units ADD COLUMN reviewed_by TEXT",
    "ALTER TABLE knowledge_units ADD COLUMN reviewed_at TEXT",
    "ALTER TABLE knowledge_units ADD COLUMN created_at TEXT",
    "ALTER TABLE knowledge_units ADD COLUMN tier TEXT NOT NULL DEFAULT 'private'",
]

_EMBEDDING_COLUMN_STATEMENTS = [
    "ALTER TABLE knowledge_units ADD COLUMN embedding BLOB",
    "ALTER TABLE knowledge_units ADD COLUMN embedding_model TEXT",
]

_TENANCY_COLUMN_STATEMENTS_KU = [
    f"ALTER TABLE knowledge_units ADD COLUMN enterprise_id TEXT NOT NULL DEFAULT '{DEFAULT_ENTERPRISE_ID}'",
    f"ALTER TABLE knowledge_units ADD COLUMN group_id TEXT NOT NULL DEFAULT '{DEFAULT_GROUP_ID}'",
]

_TENANCY_COLUMN_STATEMENTS_USERS = [
    f"ALTER TABLE users ADD COLUMN enterprise_id TEXT NOT NULL DEFAULT '{DEFAULT_ENTERPRISE_ID}'",
    f"ALTER TABLE users ADD COLUMN group_id TEXT NOT NULL DEFAULT '{DEFAULT_GROUP_ID}'",
]

# Phase 6 step 2 — cross-group / cross-enterprise sharing controls.
# `cross_group_allowed` is the per-KU opt-in flag the forward-query
# endpoint consults when an in-Enterprise sibling Group asks. Default 0
# preserves the prior behavior (no implicit cross-Group sharing) for
# every existing row.
_XGROUP_COLUMN_STATEMENTS = [
    "ALTER TABLE knowledge_units ADD COLUMN cross_group_allowed INTEGER NOT NULL DEFAULT 0",
]

CROSS_ENTERPRISE_CONSENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cross_enterprise_consents (
    consent_id TEXT PRIMARY KEY,
    requester_enterprise TEXT NOT NULL,
    responder_enterprise TEXT NOT NULL,
    requester_group TEXT,
    responder_group TEXT,
    policy TEXT NOT NULL,
    signed_by_admin TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    expires_at TEXT,
    audit_log_id TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_xent_consents_pair
    ON cross_enterprise_consents(requester_enterprise, responder_enterprise);
"""

CROSS_L2_AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cross_l2_audit (
    audit_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    requester_l2_id TEXT,
    requester_enterprise TEXT,
    requester_group TEXT,
    requester_persona TEXT,
    responder_l2_id TEXT,
    responder_enterprise TEXT,
    responder_group TEXT,
    policy_applied TEXT,
    result_count INTEGER,
    consent_id TEXT
);
"""

AIGRP_PEERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS aigrp_peers (
    l2_id TEXT PRIMARY KEY,
    enterprise TEXT NOT NULL,
    "group" TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    embedding_centroid BLOB,
    domain_bloom BLOB,
    ku_count INTEGER NOT NULL DEFAULT 0,
    domain_count INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_signature_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_aigrp_peers_enterprise ON aigrp_peers(enterprise);
"""

USERS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    enterprise_id TEXT NOT NULL DEFAULT '{DEFAULT_ENTERPRISE_ID}',
    group_id TEXT NOT NULL DEFAULT '{DEFAULT_GROUP_ID}',
    role TEXT NOT NULL DEFAULT 'user'
);
"""

# Phase 6 step 3 — role column on users for admin-only routes.
# Defaults to 'user'; promote with an UPDATE to grant admin (Lane D).
_USER_ROLE_COLUMN_STATEMENTS = [
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
]

PEERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS peers (
    persona TEXT PRIMARY KEY,
    user_id INTEGER,
    enterprise_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expertise_vector BLOB,
    expertise_domains TEXT,
    discoverable INTEGER NOT NULL DEFAULT 0,
    working_dir_hint TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_peers_enterprise_group ON peers(enterprise_id, group_id);
CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen_at);
"""

API_KEYS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '[]',
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    ttl TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
"""


def ensure_api_keys_table(conn: sqlite3.Connection) -> None:
    """Create the api_keys table and its indexes if they do not exist."""
    conn.executescript(API_KEYS_TABLE_SQL)


def ensure_review_columns(conn: sqlite3.Connection) -> None:
    """Add review status columns if they do not exist."""
    cursor = conn.execute("PRAGMA table_info(knowledge_units)")
    existing = {row[1] for row in cursor.fetchall()}
    for statement in _REVIEW_COLUMN_STATEMENTS:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing:
            conn.execute(statement)
    conn.commit()


def ensure_embedding_columns(conn: sqlite3.Connection) -> None:
    """Add embedding columns if they do not exist."""
    cursor = conn.execute("PRAGMA table_info(knowledge_units)")
    existing = {row[1] for row in cursor.fetchall()}
    for statement in _EMBEDDING_COLUMN_STATEMENTS:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing:
            conn.execute(statement)
    conn.commit()


def ensure_aigrp_peers_table(conn: sqlite3.Connection) -> None:
    """Create the AIGRP peer table if it does not exist.

    Holds this L2's view of every other L2 it knows about in the same
    Enterprise, including their last-published signature (centroid +
    Bloom). Built up via /aigrp/hello flooding at deploy time and
    refreshed by the periodic peer-poll task.
    """
    conn.executescript(AIGRP_PEERS_TABLE_SQL)


def ensure_users_table(conn: sqlite3.Connection) -> None:
    """Create the users table if it does not exist."""
    conn.executescript(USERS_TABLE_SQL)


def ensure_tenancy_columns(conn: sqlite3.Connection) -> None:
    """Add enterprise_id / group_id columns to knowledge_units and users.

    Phase 6 step 1 — additive only. Backfills any pre-existing rows with
    the project-wide defaults so the columns can be NOT NULL without a
    sentinel value showing up. Idempotent: skips columns that already
    exist (so this is safe to call on every server startup, mirroring
    the existing ensure_review_columns / ensure_embedding_columns
    pattern).

    The same backfill happens inside the Alembic baseline migration so
    DBs created the new way (Alembic-first) and the legacy way (runtime
    _ensure_schema) end up with the same shape.
    """
    cursor = conn.execute("PRAGMA table_info(knowledge_units)")
    existing_ku = {row[1] for row in cursor.fetchall()}
    for statement in _TENANCY_COLUMN_STATEMENTS_KU:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing_ku:
            conn.execute(statement)

    cursor = conn.execute("PRAGMA table_info(users)")
    existing_users = {row[1] for row in cursor.fetchall()}
    for statement in _TENANCY_COLUMN_STATEMENTS_USERS:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing_users:
            conn.execute(statement)

    # Backfill any rows that pre-date the column add. SQLite's ALTER TABLE
    # ADD COLUMN ... DEFAULT only stamps the default on rows inserted after
    # the alter; existing rows get NULL despite the NOT NULL on the column
    # spec (this is an old SQLite quirk — see the SQLite docs on ALTER
    # TABLE). Run an explicit UPDATE so legacy rows pick up the default.
    conn.execute(f"UPDATE knowledge_units SET enterprise_id = '{DEFAULT_ENTERPRISE_ID}' WHERE enterprise_id IS NULL")
    conn.execute(f"UPDATE knowledge_units SET group_id = '{DEFAULT_GROUP_ID}' WHERE group_id IS NULL")
    conn.execute(f"UPDATE users SET enterprise_id = '{DEFAULT_ENTERPRISE_ID}' WHERE enterprise_id IS NULL")
    conn.execute(f"UPDATE users SET group_id = '{DEFAULT_GROUP_ID}' WHERE group_id IS NULL")
    conn.commit()


def ensure_user_role_column(conn: sqlite3.Connection) -> None:
    """Phase 6 step 3 — add ``role`` column to ``users`` if missing.

    Idempotent: checks the existing schema and only ALTERs when the
    column doesn't already exist. Pre-existing rows backfill to ``'user'``
    so the admin gate stays closed by default.
    """
    cursor = conn.execute("PRAGMA table_info(users)")
    existing = {row[1] for row in cursor.fetchall()}
    for statement in _USER_ROLE_COLUMN_STATEMENTS:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing:
            conn.execute(statement)
    # Same SQLite quirk as the tenancy backfill — explicit UPDATE so any
    # legacy row that pre-dates the column add picks up the default.
    conn.execute("UPDATE users SET role = 'user' WHERE role IS NULL")
    conn.commit()


def ensure_peers_schema(conn: sqlite3.Connection) -> None:
    """Phase 6 step 3 — create the presence registry table + indexes.

    Idempotent on every startup, mirroring ``ensure_xgroup_consent_schema``.
    The ``peers`` table is the live registry of which personas are heart-
    beating against this L2; surfaces "active agents per L2" to the demo
    frontend. Schema is wholly additive — no relationship to existing
    tables beyond an optional FK-shaped ``user_id`` column (kept as a
    plain integer to avoid retro-fitting FK constraints across legacy
    DBs).
    """
    conn.executescript(PEERS_TABLE_SQL)
    conn.commit()


def ensure_xgroup_consent_schema(conn: sqlite3.Connection) -> None:
    """Phase 6 step 2 — additive schema for cross-L2 forward-query.

    Idempotent on every startup, mirroring ``ensure_tenancy_columns``.
    Adds ``cross_group_allowed`` to ``knowledge_units`` (default 0 — no
    implicit sharing) and creates the ``cross_enterprise_consents`` and
    ``cross_l2_audit`` tables. Both tables are write-once-from-server's
    perspective (admin signs consents in Lane D; audits are append-only)
    so no schema migration besides the initial CREATE.
    """
    cursor = conn.execute("PRAGMA table_info(knowledge_units)")
    existing_ku = {row[1] for row in cursor.fetchall()}
    for statement in _XGROUP_COLUMN_STATEMENTS:
        col = statement.split("COLUMN ")[1].split()[0]
        if col not in existing_ku:
            conn.execute(statement)
    # Backfill: SQLite's ADD COLUMN ... DEFAULT 0 leaves pre-existing
    # rows NULL on some older builds; force every row to 0 for safety.
    conn.execute("UPDATE knowledge_units SET cross_group_allowed = 0 WHERE cross_group_allowed IS NULL")
    conn.executescript(CROSS_ENTERPRISE_CONSENTS_TABLE_SQL)
    conn.executescript(CROSS_L2_AUDIT_TABLE_SQL)
    conn.commit()
