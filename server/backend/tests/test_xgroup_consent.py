"""Round-trip + revocation + lineage tests for xgroup_consent (Phase 1.0b).

Direct-on-store tests (no FastAPI) so the cryptographic logic is
exercised independently of the HTTP layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cq_server import xgroup_consent as xgc
from cq_server.crypto import b64u, canonicalize, public_key_b64u, sign_raw
from cq_server.store._sqlite import SqliteStore

ENT = "acme"
SRC = "acme/engineering"
DST = "acme/sga"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db = tmp_path / "xgc.db"
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


@pytest.fixture
def admin_a_keys() -> tuple[Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    return sk, public_key_b64u(sk)


@pytest.fixture
def admin_b_keys() -> tuple[Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    return sk, public_key_b64u(sk)


@pytest.fixture
def recovery_keys() -> tuple[Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    return sk, public_key_b64u(sk)


def _sign_body(sk: Ed25519PrivateKey, body: dict[str, Any]) -> str:
    return sign_raw(sk, canonicalize(body))


def _now() -> datetime:
    return datetime.now(UTC)


def _build_body(recovery_pk: str, *, ttl_days: int = 30) -> dict[str, Any]:
    issued = _now()
    return xgc.build_grant_body(
        enterprise_id=ENT,
        source_l2=SRC,
        target_l2=DST,
        scope_kind="domains",
        scope_values=["aws", "iam-policy"],
        issued_at=issued.isoformat(),
        expires_at=(issued + timedelta(days=ttl_days)).isoformat(),
        recovery_operator_pubkey_b64u=recovery_pk,
    )


def _sign_revoke_envelope(sk: Ed25519PrivateKey, *, grant_id: str, revoke_ts: str, reason: str, revoker_l2: str) -> str:
    env = {"grant_id": grant_id, "revoke_ts": revoke_ts, "reason": reason, "revoker_l2": revoker_l2}
    return sign_raw(sk, canonicalize(env))


# ---------------------------------------------------------------------------
# Happy-path round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_propose_cosign_ratify_revoke(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys

        body = _build_body(pk_rec)
        sig_a = _sign_body(sk_a, body)

        async def run() -> None:
            propose_out = await xgc.propose_grant(
                store,
                body=body,
                proposer_l2=SRC,
                proposer_pubkey_b64u=pk_a,
                proposer_signature_b64u=sig_a,
            )
            assert propose_out["status"] == "proposed"
            pending_id = propose_out["pending_id"]

            sig_b = _sign_body(sk_b, body)
            cosign_out = await xgc.cosign_grant(
                store,
                pending_id=pending_id,
                cosigner_l2=DST,
                cosigner_pubkey_b64u=pk_b,
                cosigner_signature_b64u=sig_b,
            )
            assert cosign_out["status"] == "cosigned"

            ratify_out = await xgc.ratify_grant(store, pending_id=pending_id)
            assert ratify_out["status"] == "active"
            assert ratify_out["grant_id"] == body["grant_id"]

            assert await xgc.is_grant_usable(store, body["grant_id"]) is True

            # Revoke by signer A.
            revoke_ts = _now().isoformat()
            revoke_sig = _sign_revoke_envelope(
                sk_a, grant_id=body["grant_id"], revoke_ts=revoke_ts, reason="rotation", revoker_l2=SRC
            )
            revoke_out = await xgc.revoke_grant(
                store,
                grant_id=body["grant_id"],
                revoker_pubkey_b64u=pk_a,
                revoker_signature_b64u=revoke_sig,
                revoker_l2=SRC,
                reason="rotation",
                revoke_ts=revoke_ts,
            )
            assert revoke_out["status"] == "revoked"
            assert revoke_out["revoked_by_recovery"] is False
            assert await xgc.is_grant_usable(store, body["grant_id"]) is False

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Replay / re-ratify
# ---------------------------------------------------------------------------


class TestReplay:
    def test_cannot_ratify_twice(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys

        body = _build_body(pk_rec)

        async def run() -> None:
            out = await xgc.propose_grant(
                store,
                body=body,
                proposer_l2=SRC,
                proposer_pubkey_b64u=pk_a,
                proposer_signature_b64u=_sign_body(sk_a, body),
            )
            await xgc.cosign_grant(
                store,
                pending_id=out["pending_id"],
                cosigner_l2=DST,
                cosigner_pubkey_b64u=pk_b,
                cosigner_signature_b64u=_sign_body(sk_b, body),
            )
            await xgc.ratify_grant(store, pending_id=out["pending_id"])
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.ratify_grant(store, pending_id=out["pending_id"])
            assert excinfo.value.code == "not_found"

        asyncio.run(run())

    def test_propose_with_bad_proposer_sig_rejected(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        _, pk_b = admin_b_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        async def run() -> None:
            # Sign with sk_a but claim pk_b — verifies false.
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.propose_grant(
                    store,
                    body=body,
                    proposer_l2=SRC,
                    proposer_pubkey_b64u=pk_b,
                    proposer_signature_b64u=_sign_body(sk_a, body),
                )
            assert excinfo.value.code == "invalid_signature"

        asyncio.run(run())

    def test_cosigner_must_be_target(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        async def run() -> None:
            out = await xgc.propose_grant(
                store,
                body=body,
                proposer_l2=SRC,
                proposer_pubkey_b64u=pk_a,
                proposer_signature_b64u=_sign_body(sk_a, body),
            )
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.cosign_grant(
                    store,
                    pending_id=out["pending_id"],
                    cosigner_l2=SRC,  # WRONG: source, not target
                    cosigner_pubkey_b64u=pk_b,
                    cosigner_signature_b64u=_sign_body(sk_b, body),
                )
            assert excinfo.value.code == "bad_request"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Recovery + lineage
# ---------------------------------------------------------------------------


async def _ratified_grant(
    store: SqliteStore,
    body: dict[str, Any],
    sk_a: Ed25519PrivateKey,
    pk_a: str,
    sk_b: Ed25519PrivateKey,
    pk_b: str,
) -> str:
    out = await xgc.propose_grant(
        store,
        body=body,
        proposer_l2=SRC,
        proposer_pubkey_b64u=pk_a,
        proposer_signature_b64u=_sign_body(sk_a, body),
    )
    await xgc.cosign_grant(
        store,
        pending_id=out["pending_id"],
        cosigner_l2=DST,
        cosigner_pubkey_b64u=pk_b,
        cosigner_signature_b64u=_sign_body(sk_b, body),
    )
    await xgc.ratify_grant(store, pending_id=out["pending_id"])
    return body["grant_id"]


class TestRecovery:
    def test_recovery_revoke_succeeds(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        sk_rec, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        async def run() -> None:
            grant_id = await _ratified_grant(store, body, sk_a, pk_a, sk_b, pk_b)
            ts = _now().isoformat()
            sig = _sign_revoke_envelope(
                sk_rec, grant_id=grant_id, revoke_ts=ts, reason="key-loss", revoker_l2="acme/recovery"
            )
            out = await xgc.revoke_grant(
                store,
                grant_id=grant_id,
                revoker_pubkey_b64u=pk_rec,
                revoker_signature_b64u=sig,
                revoker_l2="acme/recovery",
                reason="key-loss",
                revoke_ts=ts,
                is_recovery=True,
            )
            assert out["revoked_by_recovery"] is True

        asyncio.run(run())

    def test_arbitrary_admin_signature_rejected_on_recovery_path(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        async def run() -> None:
            grant_id = await _ratified_grant(store, body, sk_a, pk_a, sk_b, pk_b)
            ts = _now().isoformat()
            # Signer-A's key is NOT the recovery key.
            sig = _sign_revoke_envelope(sk_a, grant_id=grant_id, revoke_ts=ts, reason="x", revoker_l2=SRC)
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.revoke_grant(
                    store,
                    grant_id=grant_id,
                    revoker_pubkey_b64u=pk_a,  # not the recovery key
                    revoker_signature_b64u=sig,
                    revoker_l2=SRC,
                    reason="x",
                    revoke_ts=ts,
                    is_recovery=True,
                )
            assert excinfo.value.code == "invalid_signature"

        asyncio.run(run())


class TestKeyLineage:
    def test_post_rotation_revoke_succeeds_via_lineage_walk(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a_pinned, pk_a_pinned = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        # Simulate admin-A rotating to a new key after the grant lands.
        sk_a_new = Ed25519PrivateKey.generate()
        pk_a_new = public_key_b64u(sk_a_new)

        async def lineage_walker(signer_l2: str, pinned_pk: str, current_pk: str) -> bool:
            # Test stub: current chains back to pinned for this signer.
            return signer_l2 == SRC and pinned_pk == pk_a_pinned and current_pk == pk_a_new

        async def run() -> None:
            grant_id = await _ratified_grant(store, body, sk_a_pinned, pk_a_pinned, sk_b, pk_b)
            ts = _now().isoformat()
            sig = _sign_revoke_envelope(sk_a_new, grant_id=grant_id, revoke_ts=ts, reason="post-rotate", revoker_l2=SRC)
            out = await xgc.revoke_grant(
                store,
                grant_id=grant_id,
                revoker_pubkey_b64u=pk_a_new,
                revoker_signature_b64u=sig,
                revoker_l2=SRC,
                reason="post-rotate",
                revoke_ts=ts,
                key_lineage_walker=lineage_walker,
            )
            assert out["status"] == "revoked"

        asyncio.run(run())

    def test_post_rotation_without_lineage_walker_rejected(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a_pinned, pk_a_pinned = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        sk_a_new = Ed25519PrivateKey.generate()
        pk_a_new = public_key_b64u(sk_a_new)

        async def run() -> None:
            grant_id = await _ratified_grant(store, body, sk_a_pinned, pk_a_pinned, sk_b, pk_b)
            ts = _now().isoformat()
            sig = _sign_revoke_envelope(sk_a_new, grant_id=grant_id, revoke_ts=ts, reason="x", revoker_l2=SRC)
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.revoke_grant(
                    store,
                    grant_id=grant_id,
                    revoker_pubkey_b64u=pk_a_new,
                    revoker_signature_b64u=sig,
                    revoker_l2=SRC,
                    reason="x",
                    revoke_ts=ts,
                    # No lineage walker -> should reject.
                )
            assert excinfo.value.code == "invalid_signature"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TTL expiration
# ---------------------------------------------------------------------------


class TestTTL:
    def test_expired_grant_not_usable(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        admin_b_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        sk_b, pk_b = admin_b_keys
        _, pk_rec = recovery_keys

        # Build a grant that was already past its expires_at — we have to
        # bypass build_grant_body's "future" check by hand-crafting.
        issued = _now() - timedelta(days=10)
        expires = _now() - timedelta(seconds=1)  # just past
        body = {
            "grant_id": "g-expired",
            "enterprise_id": ENT,
            "source_l2": SRC,
            "target_l2": DST,
            "scope": {"kind": "all", "values": []},
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
            "nonce": b64u(b"\x00" * 16),
            "version": "v1",
            "recovery_operator_pubkey_b64u": pk_rec,
        }

        async def run() -> None:
            out = await xgc.propose_grant(
                store,
                body=body,
                proposer_l2=SRC,
                proposer_pubkey_b64u=pk_a,
                proposer_signature_b64u=_sign_body(sk_a, body),
            )
            await xgc.cosign_grant(
                store,
                pending_id=out["pending_id"],
                cosigner_l2=DST,
                cosigner_pubkey_b64u=pk_b,
                cosigner_signature_b64u=_sign_body(sk_b, body),
            )
            # ratify must reject because body.expires_at is past.
            with pytest.raises(xgc.XGroupConsentError) as excinfo:
                await xgc.ratify_grant(store, pending_id=out["pending_id"])
            assert excinfo.value.code == "expired"

        asyncio.run(run())

    def test_max_ttl_enforced_at_build_time(self, recovery_keys: tuple[Ed25519PrivateKey, str]) -> None:
        _, pk_rec = recovery_keys
        issued = _now()
        with pytest.raises(xgc.XGroupConsentError) as excinfo:
            xgc.build_grant_body(
                enterprise_id=ENT,
                source_l2=SRC,
                target_l2=DST,
                scope_kind="all",
                scope_values=[],
                issued_at=issued.isoformat(),
                expires_at=(issued + timedelta(days=100)).isoformat(),  # > 90
                recovery_operator_pubkey_b64u=pk_rec,
            )
        assert excinfo.value.code == "bad_request"


class TestPendingList:
    def test_pending_visible_to_target(
        self,
        store: SqliteStore,
        admin_a_keys: tuple[Ed25519PrivateKey, str],
        recovery_keys: tuple[Ed25519PrivateKey, str],
    ) -> None:
        sk_a, pk_a = admin_a_keys
        _, pk_rec = recovery_keys
        body = _build_body(pk_rec)

        async def run() -> None:
            await xgc.propose_grant(
                store,
                body=body,
                proposer_l2=SRC,
                proposer_pubkey_b64u=pk_a,
                proposer_signature_b64u=_sign_body(sk_a, body),
            )
            rows = await xgc.list_pending_for_target(store, enterprise_id=ENT, target_l2=DST)
            assert len(rows) == 1
            assert rows[0]["status"] == "proposed"
            # The same target list filter should exclude when target differs.
            none = await xgc.list_pending_for_target(store, enterprise_id=ENT, target_l2="other/group")
            assert none == []

        asyncio.run(run())
