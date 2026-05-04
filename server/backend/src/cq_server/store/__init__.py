"""Public store package surface.

Post-PR-B (#105): RemoteStore is gone. SqliteStore is the only implementation;
schema is owned by Alembic migrations (``cq_server.migrations.run_migrations``),
which ``SqliteStore.__init__`` invokes idempotently on every open.
"""

from ._normalize import normalize_domains
from ._protocol import Store
from ._sqlite import DEFAULT_DB_PATH, SqliteStore

__all__ = [
    "DEFAULT_DB_PATH",
    "SqliteStore",
    "Store",
    "normalize_domains",
]
