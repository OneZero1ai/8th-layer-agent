"""Tests for the L2-side 8th-Layer Directory client (sprint 3).

Covers:
  - crypto helpers (sign / verify / JCS roundtrip)
  - feature-flag default-off behaviour
  - announce flow (201 first, 200 update; 4xx permanent failure)
  - peerings pull + signature verification + persistence
  - bad-sig records are NOT persisted
  - store schema for aigrp_directory_peerings exists and accepts upsert
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cq_server import directory_client as dc
from cq_server.app import _get_store, app


@pytest.fixture()
def keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def keypair_b() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def privkey_path(tmp_path: Path, keypair: Ed25519PrivateKey) -> Path:
    p = tmp_path / "ed25519.key"
    p.write_bytes(keypair.private_bytes_raw())
    return p


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A FastAPI TestClient solely so the store schema is initialised.

    We don't exercise HTTP endpoints in this file — we just need the
    aigrp_directory_peerings table to exist for the persistence tests.
    """
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "false")  # keep loop dormant
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------


class TestCryptoHelpers:
    def test_sign_and_verify_roundtrip(self, keypair: Ed25519PrivateKey) -> None:
        payload = {"hello": "world", "n": 7}
        envelope = dc.sign_envelope(keypair, payload)

        assert envelope["payload"] == payload
        assert envelope["signing_key_id"] == dc.public_key_b64u(keypair)

        # Canonical bytes must round-trip via rfc8785.
        assert envelope["payload_canonical"].encode() == rfc8785.dumps(payload)

        ok = dc.verify_envelope_signature(
            envelope["signing_key_id"],
            envelope["payload_canonical"],
            envelope["signature"],
        )
        assert ok is True

    def test_verify_rejects_tampered_payload(self, keypair: Ed25519PrivateKey) -> None:
        envelope = dc.sign_envelope(keypair, {"a": 1})
        # Flip a byte in the canonical payload — sig over the original
        # payload should no longer verify.
        tampered = envelope["payload_canonical"].replace('"a":1', '"a":2')
        ok = dc.verify_envelope_signature(
            envelope["signing_key_id"], tampered, envelope["signature"]
        )
        assert ok is False

    def test_verify_rejects_wrong_key(
        self, keypair: Ed25519PrivateKey, keypair_b: Ed25519PrivateKey
    ) -> None:
        envelope = dc.sign_envelope(keypair, {"a": 1})
        ok = dc.verify_envelope_signature(
            dc.public_key_b64u(keypair_b),
            envelope["payload_canonical"],
            envelope["signature"],
        )
        assert ok is False

    def test_load_private_key_roundtrip(
        self, tmp_path: Path, keypair: Ed25519PrivateKey
    ) -> None:
        p = tmp_path / "k.key"
        p.write_bytes(keypair.private_bytes_raw())
        loaded = dc.load_private_key(p)
        assert dc.public_key_b64u(loaded) == dc.public_key_b64u(keypair)

    def test_load_private_key_rejects_wrong_size(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.key"
        p.write_bytes(b"x" * 31)
        with pytest.raises(ValueError, match="32 raw bytes"):
            dc.load_private_key(p)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    @pytest.mark.asyncio
    async def test_disabled_skips_bootstrap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "false")
        called: list[str] = []
        monkeypatch.setattr(
            dc, "_announce_with_retries", lambda *a, **kw: called.append("announce")
        )
        # Should return immediately without any side effects.
        from cq_server.store import SqliteStore

        store = SqliteStore(db_path=tmp_path / "x.db")
        await dc.directory_bootstrap_and_loop(store)
        assert called == []
        store.sync.close()

    @pytest.mark.asyncio
    async def test_enabled_but_unconfigured_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Enabled but no privkey path → graceful skip, not crash.
        monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "true")
        monkeypatch.delenv("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", raising=False)
        from cq_server.store import SqliteStore

        store = SqliteStore(db_path=tmp_path / "y.db")
        await dc.directory_bootstrap_and_loop(store)  # should not raise
        store.sync.close()

    @pytest.mark.asyncio
    async def test_skip_announce_mode_runs_pull_loop_without_privkey(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pull-only mode: no privkey needed; goes straight to the pull loop.

        Operator manages the announce out-of-band via 8l-directory CLI from
        their workstation. The L2 just pulls peerings to authorize cross-
        Enterprise forwards.
        """
        monkeypatch.setenv("CQ_DIRECTORY_ENABLED", "true")
        monkeypatch.setenv("CQ_DIRECTORY_SKIP_ANNOUNCE", "true")
        monkeypatch.setenv("CQ_ENTERPRISE", "8th-layer")
        # NB: no privkey path, no contact email — pull-only doesn't need them.

        announce_called: list[str] = []
        monkeypatch.setattr(
            dc, "_announce_with_retries", lambda *a, **kw: announce_called.append("nope")
        )
        # Stub the pull loop to record-and-return immediately so the test
        # doesn't actually loop forever.
        pull_called: list[tuple] = []

        async def _stub_pull_loop(privkey, enterprise_id, store):  # noqa: ANN001
            pull_called.append((privkey, enterprise_id))

        monkeypatch.setattr(dc, "_pull_loop", _stub_pull_loop)

        from cq_server.store import SqliteStore

        store = SqliteStore(db_path=tmp_path / "skip.db")
        await dc.directory_bootstrap_and_loop(store)
        store.sync.close()

        assert announce_called == [], "skip-announce mode must NOT announce"
        assert len(pull_called) == 1, "skip-announce mode must run the pull loop"
        privkey_arg, enterprise_arg = pull_called[0]
        assert privkey_arg is None, "skip-announce mode passes privkey=None"
        assert enterprise_arg == "8th-layer"


# ---------------------------------------------------------------------------
# Announce flow (mocked httpx)
# ---------------------------------------------------------------------------


class TestAnnounce:
    @pytest.mark.asyncio
    async def test_announce_201_first_time(
        self, monkeypatch: pytest.MonkeyPatch, keypair: Ed25519PrivateKey
    ) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "enterprise_id": "acme",
                    "directory_record_id": "rec_test",
                    "registered_at": "2026-05-01T20:00:00Z",
                    "verifying_key_fingerprint": "sha256:deadbeef",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            status, body = await dc._post_announce(
                client,
                keypair,
                enterprise_id="acme",
                display_name="Acme Industries",
                visibility="public",
                contact_email="admin@acme.example",
                l2_endpoints=[
                    {
                        "l2_id": "acme/engineering",
                        "endpoint_url": "https://eng.acme.example",
                        "groups": ["engineering"],
                    }
                ],
                discoverable_topics=["cloudfront"],
            )
        assert status == 201
        assert body is not None
        assert body["enterprise_id"] == "acme"
        # Body sent to directory must be the full signed envelope shape.
        env = captured["body"]
        assert env["payload"]["enterprise_id"] == "acme"
        assert env["signing_key_id"] == dc.public_key_b64u(keypair)
        # Signature must verify against the canonical bytes.
        assert dc.verify_envelope_signature(
            env["signing_key_id"], env["payload_canonical"], env["signature"]
        )

    @pytest.mark.asyncio
    async def test_announce_200_on_update(self, keypair: Ed25519PrivateKey) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "enterprise_id": "acme",
                    "directory_record_id": "rec_existing",
                    "registered_at": "2026-04-01T00:00:00Z",
                    "verifying_key_fingerprint": "sha256:deadbeef",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            status, body = await dc._post_announce(
                client,
                keypair,
                enterprise_id="acme",
                display_name="Acme",
                visibility="public",
                contact_email="a@b.c",
                l2_endpoints=[
                    {"l2_id": "acme/eng", "endpoint_url": "https://x", "groups": ["eng"]}
                ],
                discoverable_topics=[],
            )
        assert status == 200
        assert body is not None

    @pytest.mark.asyncio
    async def test_announce_4xx_permanent_no_retry(
        self, monkeypatch: pytest.MonkeyPatch, keypair: Ed25519PrivateKey
    ) -> None:
        # _announce_with_retries should give up on 400/409/422 (permanent
        # rejection — retrying won't help).
        attempts = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(409, json={"detail": "id taken with different key"})

        # Stub httpx.AsyncClient.post to use mock transport.
        original_async_client = httpx.AsyncClient

        def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(dc.httpx, "AsyncClient", patched_async_client)

        ok = await dc._announce_with_retries(
            keypair,
            enterprise_id="acme",
            display_name="Acme",
            visibility="public",
            contact_email="a@b.c",
            l2_endpoints=[{"l2_id": "x/y", "endpoint_url": "https://x", "groups": ["y"]}],
            discoverable_topics=[],
            max_attempts=3,
        )
        assert ok is False
        assert attempts == 1, "permanent 4xx must not retry"


# ---------------------------------------------------------------------------
# Pull + verify + persist
# ---------------------------------------------------------------------------


def _make_peering_record(
    initiator: Ed25519PrivateKey,
    responder: Ed25519PrivateKey,
    *,
    offer_id: str = "off_test",
    from_enterprise: str = "acme",
    to_enterprise: str = "rival",
) -> dict[str, Any]:
    """Build a fully-signed bilateral peering record. Real signatures."""
    offer_payload = {
        "offer_id": offer_id,
        "from_enterprise": from_enterprise,
        "to_enterprise": to_enterprise,
        "content_policy": "summary_only",
        "consult_logging_policy": "mutual_log_required",
        "topic_filters": ["cloudfront"],
        "expires_at": "2026-12-31T23:59:59Z",
        "offer_ts": "2026-05-01T20:30:00Z",
    }
    offer_envelope = dc.sign_envelope(initiator, offer_payload)
    accept_payload = {
        "offer_id": offer_id,
        "accepted_offer_payload_canonical": offer_envelope["payload_canonical"],
        "accept_ts": "2026-05-01T20:45:00Z",
    }
    accept_envelope = dc.sign_envelope(responder, accept_payload)

    return {
        "offer_id": offer_id,
        "status": "active",
        "from_enterprise": from_enterprise,
        "to_enterprise": to_enterprise,
        "content_policy": "summary_only",
        "consult_logging_policy": "mutual_log_required",
        "topic_filters": ["cloudfront"],
        "active_from": accept_payload["accept_ts"],
        "expires_at": offer_payload["expires_at"],
        "offer_payload_canonical": offer_envelope["payload_canonical"],
        "offer_signature": offer_envelope["signature"],
        "offer_signing_key_id": offer_envelope["signing_key_id"],
        "accept_payload_canonical": accept_envelope["payload_canonical"],
        "accept_signature": accept_envelope["signature"],
        "accept_signing_key_id": accept_envelope["signing_key_id"],
    }


class TestPullAndPersist:
    @pytest.mark.asyncio
    async def test_pull_persists_verified_records(
        self,
        app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        keypair: Ed25519PrivateKey,
        keypair_b: Ed25519PrivateKey,
    ) -> None:
        store = _get_store()
        record = _make_peering_record(keypair, keypair_b)

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/peerings/acme"):
                return httpx.Response(
                    200,
                    json={
                        "enterprise_id": "acme",
                        "as_of": "2026-05-01T21:00:00Z",
                        "peerings": [record],
                    },
                )
            if path.endswith("/enterprises/acme/key"):
                return httpx.Response(
                    200, json={"root_pubkey": dc.public_key_b64u(keypair)}
                )
            if path.endswith("/enterprises/rival/key"):
                return httpx.Response(
                    200, json={"root_pubkey": dc.public_key_b64u(keypair_b)}
                )
            return httpx.Response(404)

        original_async_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(dc.httpx, "AsyncClient", patched)

        n = await dc._pull_and_persist_once(keypair, "acme", store)
        assert n == 1

        rows = store.sync.list_directory_peerings(enterprise_id="acme")
        assert len(rows) == 1
        assert rows[0]["offer_id"] == "off_test"
        assert rows[0]["from_enterprise"] == "acme"
        assert rows[0]["to_enterprise"] == "rival"
        assert rows[0]["status"] == "active"
        assert json.loads(rows[0]["topic_filters_json"]) == ["cloudfront"]

    @pytest.mark.asyncio
    async def test_pull_drops_record_with_bad_signature(
        self,
        app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        keypair: Ed25519PrivateKey,
        keypair_b: Ed25519PrivateKey,
    ) -> None:
        store = _get_store()
        record = _make_peering_record(keypair, keypair_b, offer_id="off_bad")
        # Tamper: replace the offer signature with a junk value.
        record["offer_signature"] = "AA" * 32

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/peerings/acme"):
                return httpx.Response(
                    200,
                    json={
                        "enterprise_id": "acme",
                        "as_of": "2026-05-01T21:00:00Z",
                        "peerings": [record],
                    },
                )
            if path.endswith("/enterprises/acme/key"):
                return httpx.Response(
                    200, json={"root_pubkey": dc.public_key_b64u(keypair)}
                )
            if path.endswith("/enterprises/rival/key"):
                return httpx.Response(
                    200, json={"root_pubkey": dc.public_key_b64u(keypair_b)}
                )
            return httpx.Response(404)

        original_async_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(dc.httpx, "AsyncClient", patched)

        n = await dc._pull_and_persist_once(keypair, "acme", store)
        assert n == 0  # bad sig rejected
        rows = store.sync.list_directory_peerings(enterprise_id="acme")
        assert all(r["offer_id"] != "off_bad" for r in rows)

    @pytest.mark.asyncio
    async def test_pull_drops_record_when_pubkey_unavailable(
        self,
        app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        keypair: Ed25519PrivateKey,
        keypair_b: Ed25519PrivateKey,
    ) -> None:
        store = _get_store()
        record = _make_peering_record(keypair, keypair_b, offer_id="off_nokey")

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/peerings/acme"):
                return httpx.Response(
                    200,
                    json={
                        "enterprise_id": "acme",
                        "as_of": "2026-05-01T21:00:00Z",
                        "peerings": [record],
                    },
                )
            if "/enterprises/" in path and path.endswith("/key"):
                return httpx.Response(404)  # neither side resolvable
            return httpx.Response(404)

        original_async_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(dc.httpx, "AsyncClient", patched)

        n = await dc._pull_and_persist_once(keypair, "acme", store)
        assert n == 0


# ---------------------------------------------------------------------------
# Store schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_directory_peerings_table_exists_and_upserts(
        self, app_client: TestClient
    ) -> None:
        store = _get_store()
        store.sync.upsert_directory_peering(
            offer_id="off_schema",
            from_enterprise="a",
            to_enterprise="b",
            status="active",
            content_policy="summary_only",
            consult_logging_policy="mutual_log_required",
            topic_filters_json="[]",
            active_from="2026-05-01T00:00:00Z",
            expires_at="2026-12-31T00:00:00Z",
            offer_payload_canonical='{"x":1}',
            offer_signature_b64u="AA",
            offer_signing_key_id="K",
            accept_payload_canonical='{"y":2}',
            accept_signature_b64u="BB",
            accept_signing_key_id="J",
            last_synced_at="2026-05-01T21:00:00Z",
        )
        rows = store.sync.list_directory_peerings()
        assert len(rows) == 1
        assert rows[0]["offer_id"] == "off_schema"

        # Re-upsert with new status — same row, updated.
        store.sync.upsert_directory_peering(
            offer_id="off_schema",
            from_enterprise="a",
            to_enterprise="b",
            status="expired",
            content_policy="summary_only",
            consult_logging_policy="mutual_log_required",
            topic_filters_json="[]",
            active_from="2026-05-01T00:00:00Z",
            expires_at="2026-12-31T00:00:00Z",
            offer_payload_canonical='{"x":1}',
            offer_signature_b64u="AA",
            offer_signing_key_id="K",
            accept_payload_canonical='{"y":2}',
            accept_signature_b64u="BB",
            accept_signing_key_id="J",
            last_synced_at="2026-05-02T00:00:00Z",
        )
        rows = store.sync.list_directory_peerings()
        assert len(rows) == 1
        assert rows[0]["status"] == "expired"
