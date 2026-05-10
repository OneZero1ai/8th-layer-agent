"""End-to-end tests for the FO-1a passkey enrollment substrate (#191).

Covers the four ceremony endpoints plus a replay-attack negative case:

* ``test_enroll_begin_returns_options_shape`` — the registration options
  emitted to the browser have the WebAuthn-mandated keys and the
  challenge round-trips through the in-process cache.
* ``test_enroll_finish_persists_credential`` — a synthesised "none"
  attestation completes registration and a row lands in
  ``webauthn_credentials``.
* ``test_login_begin_returns_options_for_known_user`` — assertion
  options come back with the user's credential id pre-populated.
* ``test_login_finish_mints_jwt_and_advances_sign_count`` — a successful
  assertion increments ``sign_count`` and yields a JWT verifiable by
  the existing ``auth.verify_token``.
* ``test_login_finish_rejects_replay`` — re-using the same authenticator
  output (same ``signCount``) on the next login fails with 400. This is
  the one safety property the WebAuthn spec demands of relying parties.

The "fake authenticator" is a Python EC P-256 key + manual CBOR
(authenticator data + COSE public-key) construction. py_webauthn's
``verify_registration_response`` happily accepts ``fmt="none"`` so we
don't need to construct an attestation statement.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url

from cq_server import passkey
from cq_server.app import app
from cq_server.auth import get_current_user, hash_password, verify_token
from cq_server.deps import require_api_key

RP_ID = "localhost"
RP_ORIGIN = "http://localhost:3000"
RP_NAME = "8th-Layer test"


# --- Fixtures -------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Spin up a TestClient with a fresh DB and the auth dep overridden."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_TESTING", "1")
    monkeypatch.setenv("CQ_WEBAUTHN_RP_ID", RP_ID)
    monkeypatch.setenv("CQ_WEBAUTHN_RP_ORIGIN", RP_ORIGIN)
    monkeypatch.setenv("CQ_WEBAUTHN_RP_NAME", RP_NAME)
    # Reset the in-process challenge cache between tests so per-test
    # state never leaks across cases.
    passkey._set_challenge_cache_override_for_tests(None)
    app.dependency_overrides[require_api_key] = lambda: "alice"
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(require_api_key, None)
    app.dependency_overrides.pop(get_current_user, None)
    passkey._set_challenge_cache_override_for_tests(None)


def _seed_user(username: str = "alice", password: str = "secret123") -> None:
    """Insert a user row directly via the store sync proxy."""
    from cq_server.app import _get_store

    store = _get_store()
    store.sync.create_user(username, hash_password(password))


# --- Fake-authenticator helpers ------------------------------------------


class _FakeAuthenticator:
    """Holds an EC P-256 keypair and produces WebAuthn ceremony payloads.

    Real authenticators ship signed authenticator data + a COSE public
    key; we synthesise both. Default attestation format is "none" since
    py_webauthn accepts that for any algorithm and skips trust-path
    verification.
    """

    def __init__(self) -> None:
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.aaguid = b"\x00" * 16

    def cose_public_key(self) -> bytes:
        nums = self.priv.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty=EC2
                3: -7,  # alg=ES256
                -1: 1,  # crv=P-256
                -2: nums.x.to_bytes(32, "big"),
                -3: nums.y.to_bytes(32, "big"),
            }
        )

    def _rp_id_hash(self, rp_id: str) -> bytes:
        return hashlib.sha256(rp_id.encode()).digest()

    def make_registration_credential(
        self, *, challenge: bytes, rp_id: str, origin: str
    ) -> dict[str, Any]:
        """Build a registration response with fmt=none."""
        flags = 0x41  # UP | AT
        sign_count = 0
        attested_cred_data = (
            self.aaguid
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.cose_public_key()
        )
        auth_data = (
            self._rp_id_hash(rp_id)
            + bytes([flags])
            + struct.pack(">I", sign_count)
            + attested_cred_data
        )
        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": bytes_to_base64url(challenge),
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        attestation_object = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": auth_data}
        )
        cred_id_b64u = bytes_to_base64url(self.credential_id)
        return {
            "id": cred_id_b64u,
            "rawId": cred_id_b64u,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_object),
            },
        }

    def make_authentication_credential(
        self,
        *,
        challenge: bytes,
        rp_id: str,
        origin: str,
        sign_count: int,
        user_handle: bytes = b"\x00" * 8,
    ) -> dict[str, Any]:
        """Build an assertion response signed with the stored EC key."""
        auth_data = (
            self._rp_id_hash(rp_id) + bytes([0x01]) + struct.pack(">I", sign_count)
        )
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": bytes_to_base64url(challenge),
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        signature = self.priv.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        cred_id_b64u = bytes_to_base64url(self.credential_id)
        return {
            "id": cred_id_b64u,
            "rawId": cred_id_b64u,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(user_handle),
            },
        }


def _challenge_from_options(options: dict[str, Any]) -> bytes:
    """Pull the challenge bytes out of the options dict.

    py_webauthn emits challenges as base64url strings on the JSON
    options shape; both registration and authentication share that
    field name.
    """
    from webauthn.helpers import base64url_to_bytes

    return base64url_to_bytes(options["challenge"])


# --- Tests ----------------------------------------------------------------


class TestEnrollBegin:
    def test_enroll_begin_returns_options_shape(self, client: TestClient) -> None:
        _seed_user()
        resp = client.post("/auth/passkey/enroll/begin", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # WebAuthn mandates these keys on the registration options.
        assert body["rp"]["id"] == RP_ID
        assert body["rp"]["name"] == RP_NAME
        assert body["user"]["name"] == "alice"
        assert isinstance(body["challenge"], str) and len(body["challenge"]) > 0
        assert body["pubKeyCredParams"]
        # The challenge round-trips through the in-process cache.
        from cq_server import passkey as pk

        assert "alice" in pk._challenge_cache  # noqa: SLF001 — test introspection


class TestEnrollFinish:
    def test_enroll_finish_persists_credential(self, client: TestClient) -> None:
        _seed_user()
        begin = client.post("/auth/passkey/enroll/begin", json={}).json()
        challenge = _challenge_from_options(begin)

        authenticator = _FakeAuthenticator()
        cred = authenticator.make_registration_credential(
            challenge=challenge, rp_id=RP_ID, origin=RP_ORIGIN
        )
        resp = client.post(
            "/auth/passkey/enroll/finish",
            json={"credential": cred, "name": "DW's YubiKey"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["credential_db_id"] >= 1
        assert body["sign_count"] == 0

        # Row landed; credential_id matches the synthetic authenticator.
        from cq_server.app import _get_store

        store = _get_store()
        rows = store.sync.list_webauthn_credentials_for_user(1)
        assert len(rows) == 1
        assert rows[0]["credential_id"] == authenticator.credential_id
        assert rows[0]["sign_count"] == 0
        assert rows[0]["name"] == "DW's YubiKey"


class TestLoginBegin:
    def test_login_begin_returns_options_for_known_user(
        self, client: TestClient
    ) -> None:
        _seed_user()
        # First enroll one credential so login/begin has something to allow.
        begin = client.post("/auth/passkey/enroll/begin", json={}).json()
        authenticator = _FakeAuthenticator()
        cred = authenticator.make_registration_credential(
            challenge=_challenge_from_options(begin),
            rp_id=RP_ID,
            origin=RP_ORIGIN,
        )
        client.post("/auth/passkey/enroll/finish", json={"credential": cred})

        resp = client.post(
            "/auth/passkey/login/begin", json={"username": "alice"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rpId"] == RP_ID
        # ``allowCredentials`` is the camelCased wire shape py_webauthn emits.
        assert len(body["allowCredentials"]) == 1
        assert body["allowCredentials"][0]["id"] == bytes_to_base64url(
            authenticator.credential_id
        )

    def test_login_begin_unknown_user_404(self, client: TestClient) -> None:
        resp = client.post(
            "/auth/passkey/login/begin", json={"username": "ghost"}
        )
        assert resp.status_code == 404


class TestLoginFinish:
    def _enroll(self, client: TestClient) -> _FakeAuthenticator:
        _seed_user()
        begin = client.post("/auth/passkey/enroll/begin", json={}).json()
        authenticator = _FakeAuthenticator()
        cred = authenticator.make_registration_credential(
            challenge=_challenge_from_options(begin),
            rp_id=RP_ID,
            origin=RP_ORIGIN,
        )
        client.post("/auth/passkey/enroll/finish", json={"credential": cred})
        return authenticator

    def test_login_finish_mints_jwt_and_advances_sign_count(
        self, client: TestClient
    ) -> None:
        authenticator = self._enroll(client)
        login_begin = client.post(
            "/auth/passkey/login/begin", json={"username": "alice"}
        ).json()
        assertion = authenticator.make_authentication_credential(
            challenge=_challenge_from_options(login_begin),
            rp_id=RP_ID,
            origin=RP_ORIGIN,
            sign_count=1,
        )
        resp = client.post(
            "/auth/passkey/login/finish",
            json={"username": "alice", "credential": assertion},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["username"] == "alice"
        assert body["sign_count"] == 1
        # The JWT verifies under the same secret the server uses.
        payload = verify_token(
            body["token"], secret="test-secret-thirty-two-chars-min!"
        )
        assert payload["sub"] == "alice"

        # The DB row's sign_count advanced too.
        from cq_server.app import _get_store

        store = _get_store()
        rows = store.sync.list_webauthn_credentials_for_user(1)
        assert rows[0]["sign_count"] == 1
        assert rows[0]["last_used_at"] is not None

    def test_login_finish_rejects_replay(self, client: TestClient) -> None:
        """Same signCount on a subsequent assertion is a clone signal."""
        authenticator = self._enroll(client)
        # First successful login — advances sign_count to 1.
        login_begin = client.post(
            "/auth/passkey/login/begin", json={"username": "alice"}
        ).json()
        assertion = authenticator.make_authentication_credential(
            challenge=_challenge_from_options(login_begin),
            rp_id=RP_ID,
            origin=RP_ORIGIN,
            sign_count=1,
        )
        ok = client.post(
            "/auth/passkey/login/finish",
            json={"username": "alice", "credential": assertion},
        )
        assert ok.status_code == 200

        # Replay with the SAME signCount — py_webauthn requires strictly
        # greater. New begin first to seed a fresh challenge so the only
        # thing failing is the signCount.
        login_begin_2 = client.post(
            "/auth/passkey/login/begin", json={"username": "alice"}
        ).json()
        replay = authenticator.make_authentication_credential(
            challenge=_challenge_from_options(login_begin_2),
            rp_id=RP_ID,
            origin=RP_ORIGIN,
            sign_count=1,  # same as the previous successful login
        )
        bad = client.post(
            "/auth/passkey/login/finish",
            json={"username": "alice", "credential": replay},
        )
        assert bad.status_code == 400
        # Generic detail — py_webauthn's internal exception message is
        # logged server-side, not surfaced to the client (review fix #2).
        assert bad.json()["detail"] == "passkey verification failed"


class TestRpConfigHardening:
    """Verify the env-gated dev-default pattern (review fix #1)."""

    def test_rp_id_raises_in_non_dev_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CQ_ENV", "production")
        monkeypatch.delenv("CQ_WEBAUTHN_RP_ID", raising=False)
        with pytest.raises(RuntimeError, match="CQ_WEBAUTHN_RP_ID is required"):
            passkey.rp_id()

    def test_rp_origin_rejects_http_in_non_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CQ_ENV", "production")
        monkeypatch.setenv("CQ_WEBAUTHN_RP_ORIGIN", "http://insecure.example.com")
        with pytest.raises(RuntimeError, match="must use https://"):
            passkey.rp_origin()

    def test_rp_id_returns_default_in_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CQ_ENV", "dev")
        monkeypatch.delenv("CQ_WEBAUTHN_RP_ID", raising=False)
        assert passkey.rp_id() == passkey.DEFAULT_RP_ID


class TestChallengeCacheGate:
    """Verify the test-only injection point refuses to fire in production
    (review fix #3)."""

    def test_override_refuses_without_cq_testing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CQ_TESTING", raising=False)
        with pytest.raises(RuntimeError, match="test-only"):
            passkey._set_challenge_cache_override_for_tests({})
