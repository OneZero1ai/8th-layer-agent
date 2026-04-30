"""Database schema definitions and migration logic."""

import sqlite3

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

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
