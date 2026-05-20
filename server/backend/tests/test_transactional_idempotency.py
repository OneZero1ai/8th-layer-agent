"""Unit tests for the in-memory ``IdempotencyStore`` (Decision 34)."""

from __future__ import annotations

import time

from cq_server.transactional.idempotency import IdempotencyStore


def test_put_then_get_returns_value() -> None:
    s = IdempotencyStore(ttl_seconds=60)
    s.put("k1", {"handle": "tx_1"})
    assert s.get("k1") == {"handle": "tx_1"}


def test_get_returns_none_for_unknown_key() -> None:
    s = IdempotencyStore()
    assert s.get("nope") is None


def test_empty_key_is_noop() -> None:
    s = IdempotencyStore()
    s.put("", {"x": 1})
    assert s.get("") is None


def test_ttl_expiry_drops_entry() -> None:
    s = IdempotencyStore(ttl_seconds=0.01)
    s.put("k", {"v": 1})
    time.sleep(0.02)
    assert s.get("k") is None
    # And lazy eviction means the entry was actually removed.
    assert "k" not in s._entries


def test_thread_safety_basic_smoke() -> None:
    import threading

    s = IdempotencyStore(ttl_seconds=10)
    errors: list[Exception] = []

    def worker(start: int) -> None:
        try:
            for i in range(100):
                key = f"k{start + i}"
                s.put(key, {"i": i})
                assert s.get(key) == {"i": i}
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i * 100,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
