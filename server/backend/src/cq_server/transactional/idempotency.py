"""In-memory idempotency cache for ``Idempotency-Key`` headers (Decision 34).

The window is 60 seconds — small enough that a single restarted task
forgets pre-restart keys (acceptable; the L2 retries on a different
key or accepts a duplicate send). Distributed dedup is not in scope
for V1; the central service runs as a single ECS task right now, so
in-process state is the right granularity.

Storage:

* ``dict[key, (timestamp, cached_response)]``.
* Lazy eviction on access — no background sweeper. The cache stays
  small in practice (~ tens of keys / minute).

Cached value is the full response envelope (``dict``) so a replay
returns the exact same body the original send produced (handle,
ses_message_id, suppression_check). This is the property a
well-behaved retry expects: same key → same answer, byte-for-byte.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 60.0


@dataclass
class IdempotencyStore:
    """Thread-safe in-memory dedup cache.

    Two methods:

    * :meth:`get` — return the cached response if the key is fresh,
      else None.
    * :meth:`put` — record a response under the key.

    The lock is a stdlib ``Lock``; contention is negligible at our
    rate (sub-100 sends/sec realistic).
    """

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    _entries: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            ts, cached = entry
            if now - ts > self.ttl_seconds:
                # Lazy eviction — stale entry, drop it.
                self._entries.pop(key, None)
                return None
            return cached

    def put(self, key: str, response: dict[str, Any]) -> None:
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            self._entries[key] = (now, response)

    def _sweep_locked(self) -> None:
        """Drop expired entries — exposed for tests, not the hot path."""
        now = time.monotonic()
        stale = [k for k, (ts, _) in self._entries.items() if now - ts > self.ttl_seconds]
        for k in stale:
            self._entries.pop(k, None)
