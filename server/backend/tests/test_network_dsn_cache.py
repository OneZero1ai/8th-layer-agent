"""Issue #23 — DSN routed-hop · in-memory signature cache.

Pins:
  - Pre-warmed cache: resolver reads it, NO live fan-out per request,
    cache_hit=True in the trace.
  - Cold start: first request triggers live fan-out, cache_hit=False,
    cache populated for next time.
  - Stale cache (older than DSN_CACHE_STALE_SECS): refresh on next request,
    cache_hit=False.
  - Partial cache (some L2s' last poll didn't return a signature): rank
    only the available ones, no live fan-out.
  - Background refill loop replaces stale entries on each cycle.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from cq_server import network
from cq_server.app import _get_store, app

ALICE = "alice"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _b64_centroid(axis: int, dim: int = 8) -> str:
    import base64

    v = [0.0] * dim
    v[axis] = 1.0
    return base64.b64encode(_pack(v)).decode("ascii")


def _snap(*, slug: str, enterprise: str, group: str, axis: int) -> network._L2Snapshot:
    s = network._L2Snapshot(
        slug=slug,
        enterprise=enterprise,
        group=group,
        endpoint=f"http://stub-{slug}.local",
        reachable=True,
    )
    s.peers = []
    s.signature = {
        "l2_id": f"{enterprise}/{group}",
        "ku_count": 5,
        "domain_count": 4,
        "computed_at": "2026-05-01T00:00:00+00:00",
        "embedding_centroid_b64": _b64_centroid(axis),
        "embedding_model": "amazon.titan-embed-text-v2:0",
    }
    s.active_personas = []
    return s


def _stub_embed(monkeypatch: pytest.MonkeyPatch, axis: int = 0, dim: int = 8) -> None:
    v = [0.0] * dim
    v[axis] = 1.0

    def _fake_embed(text: str):
        return _pack(v), "stub-model"

    monkeypatch.setattr(network, "embed_text", _fake_embed)


def _seed_cache(snapshots: list[network._L2Snapshot], age_seconds: float = 0.0) -> None:
    """Pre-fill the module-level signature cache as if the loop ran ``age_seconds`` ago."""
    network._signature_cache.clear()
    for s in snapshots:
        network._signature_cache[s.slug] = s
    network._signature_cache_filled_at = time.monotonic() - age_seconds


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "dsn-cache.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    network._TOPOLOGY_CACHE["value"] = None
    network._TOPOLOGY_CACHE["expires_at"] = 0.0
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0
    network._PEER_KEY_OVERRIDES.clear()
    network._PEER_KEY_OVERRIDES["orion"] = "stub-orion-key"
    network._PEER_KEY_OVERRIDES["acme"] = "stub-acme-key"

    # Park the background cache loop on a long sleep so it doesn't
    # interleave with the test body's _fan_out_all monkeypatches. Tests
    # that exercise the loop directly (test_refill_replaces_stale_entries)
    # call _refill_signature_cache themselves and don't use this fixture.
    monkeypatch.setattr(network, "DSN_CACHE_REFRESH_SECS", 86_400)

    # Pre-monkeypatch _fan_out_all to an empty no-op so the lifespan's
    # initial refill doesn't try to hit real ALBs. Tests then override
    # this with their own stub.
    async def _initial_noop(fleet):
        return []

    monkeypatch.setattr(network, "_fan_out_all", _initial_noop)

    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.create_user(ALICE, pw)
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE users SET enterprise_id = 'acme', group_id = 'engineering' WHERE username = ?",
                (ALICE,),
            )
        yield c


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"username": ALICE, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _full_fleet_snapshots(target_axis: int = 0) -> list[network._L2Snapshot]:
    axis_map = {
        "orion-eng": 1,
        "orion-sol": 2,
        "orion-gtm": 3,
        "acme-eng": 4,
        "acme-sol": target_axis,
        "acme-fin": 5,
    }
    return [
        _snap(slug=l2["slug"], enterprise=l2["enterprise"], group=l2["group"], axis=axis_map[l2["slug"]])
        for l2 in network.FLEET_L2S
    ]


# ---------------------------------------------------------------------------
# Cache-hit path: pre-warm cache, observe NO live fan-out, cache_hit=True
# ---------------------------------------------------------------------------


def test_cache_hit_skips_live_fetch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_embed(monkeypatch, axis=0)
    _seed_cache(_full_fleet_snapshots(target_axis=0), age_seconds=10.0)

    # Tripwire: if _fan_out_all is invoked, this test should fail.
    fan_out_called = {"count": 0}

    async def _no_fanout(fleet):
        fan_out_called["count"] += 1
        return []

    monkeypatch.setattr(network, "_fan_out_all", _no_fanout)

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "anything", "max_candidates": 3},
    )
    assert resp.status_code == 200, resp.text
    assert fan_out_called["count"] == 0, "cache hit should not trigger live fan-out"

    body = resp.json()
    cache_step = next(s for s in body["resolution_path"] if s["step"] == "cache_lookup")
    assert cache_step["cache_hit"] is True
    assert cache_step["l2_count"] == 6
    assert 9_000 <= cache_step["cache_age_ms"] <= 12_000  # ~10s seeded
    assert cache_step["latency_ms"] < 50  # cache reads should be µs


# ---------------------------------------------------------------------------
# Cache-miss path: cold cache, falls back to live fan-out, warms cache
# ---------------------------------------------------------------------------


def test_cache_miss_falls_back_to_live_fetch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_embed(monkeypatch, axis=0)
    # Cache is empty (the fixture's initial-noop refill writes nothing);
    # filled_at may be set by that initial refill but cache_hit gates on
    # len() > 0, so empty-with-stamp still falls back to live fetch.
    assert len(network._signature_cache) == 0

    fan_out_called = {"count": 0}

    async def _fake_fanout(fleet):
        fan_out_called["count"] += 1
        return _full_fleet_snapshots(target_axis=0)

    monkeypatch.setattr(network, "_fan_out_all", _fake_fanout)

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "anything", "max_candidates": 3},
    )
    assert resp.status_code == 200, resp.text
    assert fan_out_called["count"] == 1, "cold-start should trigger one live fan-out"

    body = resp.json()
    cache_step = next(s for s in body["resolution_path"] if s["step"] == "cache_lookup")
    assert cache_step["cache_hit"] is False
    assert cache_step["l2_count"] == 6  # warmed during the request
    # Cache is now populated for the next request
    assert len(network._signature_cache) == 6


# ---------------------------------------------------------------------------
# Stale cache: filled long ago, treated as miss, refreshes on next request
# ---------------------------------------------------------------------------


def test_stale_cache_falls_back(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_embed(monkeypatch, axis=0)
    _seed_cache(_full_fleet_snapshots(target_axis=0), age_seconds=network.DSN_CACHE_STALE_SECS + 60.0)

    fan_out_called = {"count": 0}

    async def _fake_fanout(fleet):
        fan_out_called["count"] += 1
        return _full_fleet_snapshots(target_axis=0)

    monkeypatch.setattr(network, "_fan_out_all", _fake_fanout)

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "anything", "max_candidates": 3},
    )
    assert resp.status_code == 200, resp.text
    assert fan_out_called["count"] == 1, "stale cache should trigger live fan-out"
    body = resp.json()
    cache_step = next(s for s in body["resolution_path"] if s["step"] == "cache_lookup")
    assert cache_step["cache_hit"] is False


# ---------------------------------------------------------------------------
# Partial cache: only 4 of 6 L2s have signatures cached. Resolver ranks
# the available ones, does not trigger fan-out.
# ---------------------------------------------------------------------------


def test_partial_cache_ranks_available_l2s(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_embed(monkeypatch, axis=0)
    # Only 4 of the 6 L2s have signatures cached; 2 were unreachable last poll.
    snapshots = _full_fleet_snapshots(target_axis=0)
    _seed_cache(snapshots[:4], age_seconds=5.0)

    async def _no_fanout(fleet):
        raise AssertionError("partial cache hit should still skip live fan-out")

    monkeypatch.setattr(network, "_fan_out_all", _no_fanout)

    jwt = _login(client)
    resp = client.post(
        "/api/v1/network/dsn/resolve",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"intent": "anything", "max_candidates": 6},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Up to 4 candidates, since 2 L2s aren't in the cache.
    assert len(body["candidates"]) <= 4
    cache_step = next(s for s in body["resolution_path"] if s["step"] == "cache_lookup")
    assert cache_step["cache_hit"] is True
    assert cache_step["l2_count"] == 4


# ---------------------------------------------------------------------------
# Background refill: _refill_signature_cache replaces stale entries
# ---------------------------------------------------------------------------


def test_refill_replaces_stale_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0

    snapshots_v1 = _full_fleet_snapshots(target_axis=0)
    snapshots_v2 = _full_fleet_snapshots(target_axis=1)  # different "freshness"

    # First refill: 6 L2s
    async def _v1(fleet):
        return snapshots_v1

    monkeypatch.setattr(network, "_fan_out_all", _v1)
    filled, total = asyncio.run(network._refill_signature_cache())
    assert filled == 6 and total == 6
    assert len(network._signature_cache) == 6

    # Simulate one L2 going dark: only 5 snapshots returned next cycle
    async def _v2(fleet):
        return [s for s in snapshots_v2 if s.slug != "orion-gtm"]

    monkeypatch.setattr(network, "_fan_out_all", _v2)
    filled, total = asyncio.run(network._refill_signature_cache())
    assert filled == 5 and total == 6
    assert "orion-gtm" not in network._signature_cache
    assert len(network._signature_cache) == 5
