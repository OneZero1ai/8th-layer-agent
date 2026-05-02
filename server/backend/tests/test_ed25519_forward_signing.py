"""Sprint 4 — Ed25519 per-L2 forward-signing tests (#44 / full CRIT #34 close).

The unit under test:

- Per-L2 keypair bootstrap (load existing / generate fresh / corrupt
  recovery / disabled-on-error) in ``forward_sign``.
- /aigrp/hello carries pubkey, receiver records it.
- /aigrp/peers response includes the recorded pubkey.
- Sender-side signature header on /aigrp/forward-query (network) and
  /consults/forward-* (consults).
- Receiver-side verification:
   - peer with pubkey + valid sig → accepted (info-log).
   - peer with pubkey + missing sig → 403.
   - peer with pubkey + invalid sig → 403.
   - peer with pubkey + tampered body → 403.
   - peer with pubkey + sig from wrong key → 403.
   - peer without pubkey → legacy mode accepts (warning) by default.
   - peer without pubkey + CQ_REQUIRE_SIGNED_FORWARDS=true → 403.
- Pubkey survives a re-hello with the same key (idempotent).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cq_server import aigrp as aigrp_mod
from cq_server import consults, forward_sign, network
from cq_server.app import _get_store, app
from cq_server.crypto import b64u, public_key_b64u

PEER_KEY = "test-peer-key-ed25519-fwd-sign!!"
PEER_L2 = "acme/engineering"
SELF_ENTERPRISE = "acme"
SELF_GROUP = "solutions"
SELF_L2 = f"{SELF_ENTERPRISE}/{SELF_GROUP}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def aigrp_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with this L2 as ``acme/solutions`` + per-L2 key on disk."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "ed25519.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_AIGRP_PEER_KEY", PEER_KEY)
    monkeypatch.setenv("CQ_ENTERPRISE", SELF_ENTERPRISE)
    monkeypatch.setenv("CQ_GROUP", SELF_GROUP)
    monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(tmp_path / "self_l2.key"))
    monkeypatch.setenv("CQ_EMBED_ENABLED", "false")
    monkeypatch.delenv("CQ_REQUIRE_SIGNED_FORWARDS", raising=False)
    forward_sign.reload_l2_privkey()
    network._signature_cache.clear()
    network._signature_cache_filled_at = 0.0
    with TestClient(app) as c:
        yield c
    forward_sign.reload_l2_privkey()


@pytest.fixture()
def peer_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def other_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


# ---------------------------------------------------------------------------
# 1. Per-L2 keypair bootstrap
# ---------------------------------------------------------------------------


class TestKeypairBootstrap:
    def test_generates_new_key_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "fresh.key"
        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(path))
        forward_sign.reload_l2_privkey()
        pk = forward_sign.get_l2_privkey()
        assert pk is not None
        assert path.exists()
        assert len(path.read_bytes()) == 32

    def test_loads_existing_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "existing.key"
        seeded = Ed25519PrivateKey.generate()
        path.write_bytes(seeded.private_bytes_raw())
        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(path))
        forward_sign.reload_l2_privkey()
        pk = forward_sign.get_l2_privkey()
        assert pk is not None
        assert public_key_b64u(pk) == public_key_b64u(seeded)

    def test_corrupt_key_disables_signing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "corrupt.key"
        path.write_bytes(b"too-short")
        monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(path))
        forward_sign.reload_l2_privkey()
        assert forward_sign.get_l2_privkey() is None
        assert forward_sign.self_public_key_b64u() is None
        # And signing returns None instead of crashing.
        assert forward_sign.sign_forward_request({"x": 1}, "acme/eng") is None


# ---------------------------------------------------------------------------
# 2. /aigrp/hello propagates pubkey
# ---------------------------------------------------------------------------


class TestHelloPubkeyExchange:
    def test_hello_records_pubkey(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        peer_pub = public_key_b64u(peer_keypair)
        r = aigrp_client.post(
            "/api/v1/aigrp/hello",
            headers={"authorization": f"Bearer {PEER_KEY}"},
            json={
                "l2_id": PEER_L2,
                "enterprise": "acme",
                "group": "engineering",
                "endpoint_url": "http://peer.test",
                "public_key_ed25519": peer_pub,
            },
        )
        assert r.status_code == 201
        store = _get_store()
        assert store.get_aigrp_peer_pubkey(PEER_L2) == peer_pub

    def test_peers_endpoint_exposes_pubkey(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        peer_pub = public_key_b64u(peer_keypair)
        # The /aigrp/hello response is what new joiners use to learn the
        # mesh topology — it must include our own pubkey in the self_entry
        # so the joiner can later verify forwards FROM us.
        hello = aigrp_client.post(
            "/api/v1/aigrp/hello",
            headers={"authorization": f"Bearer {PEER_KEY}"},
            json={
                "l2_id": PEER_L2,
                "enterprise": "acme",
                "group": "engineering",
                "endpoint_url": "http://peer.test",
                "public_key_ed25519": peer_pub,
            },
        )
        hello_peers = {p["l2_id"]: p for p in hello.json()["peers"]}
        assert hello_peers[SELF_L2]["public_key_ed25519"] == forward_sign.self_public_key_b64u()
        # /aigrp/peers reflects the recorded peer rows — joiner is in there.
        r = aigrp_client.get(
            "/api/v1/aigrp/peers", headers={"authorization": f"Bearer {PEER_KEY}"}
        )
        assert r.status_code == 200
        peers = {p["l2_id"]: p for p in r.json()["peers"]}
        assert peers[PEER_L2]["public_key_ed25519"] == peer_pub

    def test_rehello_same_key_is_idempotent(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        peer_pub = public_key_b64u(peer_keypair)
        body = {
            "l2_id": PEER_L2,
            "enterprise": "acme",
            "group": "engineering",
            "endpoint_url": "http://peer.test",
            "public_key_ed25519": peer_pub,
        }
        for _ in range(3):
            r = aigrp_client.post(
                "/api/v1/aigrp/hello",
                headers={"authorization": f"Bearer {PEER_KEY}"},
                json=body,
            )
            assert r.status_code == 201
        store = _get_store()
        assert store.get_aigrp_peer_pubkey(PEER_L2) == peer_pub

    def test_legacy_hello_without_pubkey_yields_null(
        self, aigrp_client: TestClient
    ) -> None:
        r = aigrp_client.post(
            "/api/v1/aigrp/hello",
            headers={"authorization": f"Bearer {PEER_KEY}"},
            json={
                "l2_id": PEER_L2,
                "enterprise": "acme",
                "group": "engineering",
                "endpoint_url": "http://peer.test",
            },
        )
        assert r.status_code == 201
        store = _get_store()
        assert store.get_aigrp_peer_pubkey(PEER_L2) is None


# ---------------------------------------------------------------------------
# 3. Sender-side signature header
# ---------------------------------------------------------------------------


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class TestSenderSideSignatures:
    def test_forward_query_sender_signs_body(
        self, aigrp_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``network._call_forward_query`` adds X-8L-Forwarder-Sig that
        verifies against this L2's pubkey + JCS(body) || forwarder id."""
        captured: dict[str, Any] = {}

        class StubResp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"results": [], "policy_applied": "denied", "result_count": 0}

        class StubClient:
            async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> StubResp:
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return StubResp()

        import asyncio

        async def _run() -> None:
            await network._call_forward_query(
                StubClient(),  # type: ignore[arg-type]
                target={
                    "endpoint": "http://peer.test",
                    "enterprise": "rival",
                    "slug": "rival",
                },
                requester={"enterprise": "acme", "group": "solutions"},
                requester_persona="alice",
                query_vec=[1.0, 0.0, 0.0],
                query_text="cf cache",
            )

        asyncio.run(_run())
        sig_header = captured["headers"].get(forward_sign.SIGNATURE_HEADER)
        assert sig_header, "sender omitted the signature header"
        # Verify it against this L2's own pubkey.
        self_pub = forward_sign.self_public_key_b64u()
        assert self_pub
        assert forward_sign.verify_forward_signature(
            self_pub, captured["json"], "acme/solutions", sig_header
        )

    def test_consult_forward_request_signed(
        self, aigrp_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict[str, Any]] = []

        class StubResp:
            status_code = 201
            text = "{}"

        class StubClient:
            def __init__(self, **kw: Any) -> None:
                pass

            def __enter__(self) -> "StubClient":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def post(
                self, url: str, *, headers: dict[str, str], json: dict[str, Any]
            ) -> StubResp:
                captured.append({"headers": headers, "json": json})
                return StubResp()

        monkeypatch.setattr(consults.httpx, "Client", StubClient)

        target = {
            "l2_id": PEER_L2,
            "endpoint_url": "http://peer.test",
        }
        payload = {
            "thread_id": "t1",
            "message_id": "m1",
            "from_l2_id": SELF_L2,
            "from_persona": "alice",
            "to_l2_id": PEER_L2,
            "to_persona": "bob",
            "subject": "hello",
            "content": "ping",
            "created_at": "2026-05-01T00:00:00+00:00",
        }
        consults._forward_request(target, payload)

        assert len(captured) == 1
        sig = captured[0]["headers"].get(forward_sign.SIGNATURE_HEADER)
        assert sig
        assert captured[0]["headers"][aigrp_mod.FORWARDER_HEADER] == SELF_L2
        self_pub = forward_sign.self_public_key_b64u()
        assert self_pub
        assert forward_sign.verify_forward_signature(self_pub, payload, SELF_L2, sig)


# ---------------------------------------------------------------------------
# 4. Receiver-side verification
# ---------------------------------------------------------------------------


def _seed_peer(store: Any, pubkey_b64u: str | None) -> None:
    store.upsert_aigrp_peer(
        l2_id=PEER_L2,
        enterprise="acme",
        group="engineering",
        endpoint_url="http://peer.test",
        embedding_centroid=None,
        domain_bloom=None,
        ku_count=0,
        domain_count=0,
        embedding_model=None,
        signature_received=False,
        public_key_ed25519=pubkey_b64u,
    )


def _forward_query_body(axis: int = 0) -> dict[str, Any]:
    vec = [0.0] * 8
    vec[axis] = 1.0
    return {
        "query_vec": vec,
        "query_text": "cdn cache",
        "requester_l2_id": PEER_L2,
        "requester_enterprise": "acme",
        "requester_group": "engineering",
        "requester_persona": "alice",
        "max_results": 5,
    }


class TestReceiverVerification:
    def test_valid_signature_accepted(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        store = _get_store()
        _seed_peer(store, public_key_b64u(peer_keypair))
        body = _forward_query_body()
        sig = b64u(peer_keypair.sign(forward_sign.signing_input_for(body, PEER_L2)))
        r = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                aigrp_mod.FORWARDER_HEADER: PEER_L2,
                forward_sign.SIGNATURE_HEADER: sig,
            },
            json=body,
        )
        assert r.status_code == 200, r.text

    def test_missing_signature_when_pubkey_on_file_403(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        store = _get_store()
        _seed_peer(store, public_key_b64u(peer_keypair))
        body = _forward_query_body()
        r = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                aigrp_mod.FORWARDER_HEADER: PEER_L2,
            },
            json=body,
        )
        assert r.status_code == 403
        assert "missing" in r.json()["detail"].lower()

    def test_tampered_body_rejected(
        self, aigrp_client: TestClient, peer_keypair: Ed25519PrivateKey
    ) -> None:
        store = _get_store()
        _seed_peer(store, public_key_b64u(peer_keypair))
        body = _forward_query_body()
        # Sign one body, send a different one.
        sig = b64u(peer_keypair.sign(forward_sign.signing_input_for(body, PEER_L2)))
        tampered = dict(body)
        tampered["query_text"] = "DIFFERENT QUERY"
        r = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                aigrp_mod.FORWARDER_HEADER: PEER_L2,
                forward_sign.SIGNATURE_HEADER: sig,
            },
            json=tampered,
        )
        assert r.status_code == 403
        assert "verification failed" in r.json()["detail"]

    def test_signature_from_wrong_key_rejected(
        self,
        aigrp_client: TestClient,
        peer_keypair: Ed25519PrivateKey,
        other_keypair: Ed25519PrivateKey,
    ) -> None:
        store = _get_store()
        _seed_peer(store, public_key_b64u(peer_keypair))
        body = _forward_query_body()
        # Signed with a *different* key than the one we recorded.
        sig = b64u(other_keypair.sign(forward_sign.signing_input_for(body, PEER_L2)))
        r = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                aigrp_mod.FORWARDER_HEADER: PEER_L2,
                forward_sign.SIGNATURE_HEADER: sig,
            },
            json=body,
        )
        assert r.status_code == 403

    def test_legacy_no_pubkey_accepted_with_warning(
        self, aigrp_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _get_store()
        _seed_peer(store, None)  # legacy peer
        body = _forward_query_body()
        with caplog.at_level(logging.WARNING, logger=aigrp_mod.logger.name):
            r = aigrp_client.post(
                "/api/v1/aigrp/forward-query",
                headers={
                    "authorization": f"Bearer {PEER_KEY}",
                    aigrp_mod.FORWARDER_HEADER: PEER_L2,
                },
                json=body,
            )
        assert r.status_code == 200
        assert any("legacy unsigned forward" in m for m in caplog.messages)

    def test_strict_mode_rejects_legacy(
        self, aigrp_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _get_store()
        _seed_peer(store, None)  # legacy peer
        monkeypatch.setenv("CQ_REQUIRE_SIGNED_FORWARDS", "true")
        body = _forward_query_body()
        r = aigrp_client.post(
            "/api/v1/aigrp/forward-query",
            headers={
                "authorization": f"Bearer {PEER_KEY}",
                aigrp_mod.FORWARDER_HEADER: PEER_L2,
            },
            json=body,
        )
        assert r.status_code == 403
        assert "strict mode" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. Sign/verify primitive
# ---------------------------------------------------------------------------


class TestSigningInput:
    def test_signing_input_is_canonical_plus_l2_id(self) -> None:
        body_a = {"a": 1, "b": 2}
        body_b = {"b": 2, "a": 1}  # different dict order, same JCS
        m1 = forward_sign.signing_input_for(body_a, "acme/eng")
        m2 = forward_sign.signing_input_for(body_b, "acme/eng")
        assert m1 == m2
        # Different forwarder id -> different bytes.
        m3 = forward_sign.signing_input_for(body_a, "acme/sol")
        assert m1 != m3
