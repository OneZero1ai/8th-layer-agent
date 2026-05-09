"""Phase 1.0d (Decision 28) — AIGRP runtime wiring tests.

Covers the bridge between ``enterprise_root`` (SSM-backed root) and
``pair_secret`` (HKDF derivation) into the HTTP auth path.

Acceptance: two L2s under the same Enterprise but different Groups,
seeded with the SAME Enterprise root, derive matching bearer tokens
without any shared per-stack secret.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi import Request as FastApiRequest

from cq_server import aigrp
from cq_server.aigrp import _legacy, enterprise_root, runtime


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wipe AIGRP env so each test starts from a known state.
    for k in (
        "CQ_AIGRP_PEER_KEY",
        "CQ_AIGRP_IS_FIRST_DEPLOY",
        "CQ_AIGRP_SEED_PEER_URL",
        "CQ_ENTERPRISE_ROOT_BOOTSTRAP",
        "CQ_ENTERPRISE_ROOT_KMS_KEY_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    # Default identity for the running L2.
    monkeypatch.setenv("CQ_ENTERPRISE", "8th-layer-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    enterprise_root.invalidate_cache()
    runtime.invalidate_pair_cache()
    yield
    enterprise_root.invalidate_cache()
    runtime.invalidate_pair_cache()


@pytest.fixture
def fake_root() -> bytes:
    return bytes.fromhex("ab" * 32)


def _stub_root_in_ssm(monkeypatch: pytest.MonkeyPatch, root: bytes) -> mock.Mock:
    client = mock.Mock()
    client.get_parameter.return_value = {"Parameter": {"Value": root.hex()}}
    monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# Acceptance #1 — both sides of a pair derive matching bearer tokens.
# ---------------------------------------------------------------------------


class TestSymmetricDerivation:
    def test_engineering_and_sga_derive_matching_bearer(
        self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes
    ) -> None:
        """8th-layer-corp/engineering and /sga compute the same token."""
        _stub_root_in_ssm(monkeypatch, fake_root)

        # Side A: engineering deriving for sga.
        monkeypatch.setenv("CQ_GROUP", "engineering")
        runtime.invalidate_pair_cache()
        token_from_eng = runtime.derive_bearer_token("8th-layer-corp/sga")

        # Side B: sga deriving for engineering.
        monkeypatch.setenv("CQ_GROUP", "sga")
        runtime.invalidate_pair_cache()
        token_from_sga = runtime.derive_bearer_token("8th-layer-corp/engineering")

        assert token_from_eng == token_from_sga
        # And the token is non-empty + b64url-shape (no padding chars).
        assert token_from_eng
        assert "=" not in token_from_eng

    def test_different_pairs_produce_different_tokens(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        _stub_root_in_ssm(monkeypatch, fake_root)
        token_eng_sga = runtime.derive_bearer_token("8th-layer-corp/sga")
        token_eng_finance = runtime.derive_bearer_token("8th-layer-corp/finance")
        assert token_eng_sga != token_eng_finance

    def test_self_pair_rejected(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        _stub_root_in_ssm(monkeypatch, fake_root)
        with pytest.raises(ValueError):
            runtime.derive_bearer_token("8th-layer-corp/engineering")


# ---------------------------------------------------------------------------
# verify_bearer_against_peer — receiver-side check.
# ---------------------------------------------------------------------------


class TestVerify:
    def test_round_trip_accepts(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        _stub_root_in_ssm(monkeypatch, fake_root)
        # Sender (sga) derives, receiver (engineering) verifies.
        monkeypatch.setenv("CQ_GROUP", "sga")
        runtime.invalidate_pair_cache()
        bearer = runtime.derive_bearer_token("8th-layer-corp/engineering")

        monkeypatch.setenv("CQ_GROUP", "engineering")
        runtime.invalidate_pair_cache()
        assert runtime.verify_bearer_against_peer("8th-layer-corp/sga", bearer) is True

    def test_tampered_bearer_rejected(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        _stub_root_in_ssm(monkeypatch, fake_root)
        monkeypatch.setenv("CQ_GROUP", "engineering")
        good = runtime.derive_bearer_token("8th-layer-corp/sga")
        bad = good[:-2] + ("AA" if good[-2:] != "AA" else "BB")
        assert runtime.verify_bearer_against_peer("8th-layer-corp/sga", bad) is False

    def test_empty_inputs_rejected(self) -> None:
        assert runtime.verify_bearer_against_peer("", "anything") is False
        assert runtime.verify_bearer_against_peer("8th-layer-corp/sga", "") is False

    def test_ssm_failure_returns_false_not_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("boom")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        runtime.invalidate_pair_cache()
        assert runtime.verify_bearer_against_peer("8th-layer-corp/sga", "anything") is False


# ---------------------------------------------------------------------------
# require_peer_key — dual-mode FastAPI dependency.
# ---------------------------------------------------------------------------


def _request_with(headers: dict[str, str]) -> FastApiRequest:
    """Construct a minimal FastAPI Request with the given headers."""
    raw = []
    for k, v in headers.items():
        raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
    }
    return FastApiRequest(scope=scope)


class TestRequirePeerKey:
    def test_pair_secret_bearer_accepted(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        _stub_root_in_ssm(monkeypatch, fake_root)
        # Receiver = engineering. Sender = sga.
        monkeypatch.setenv("CQ_GROUP", "engineering")
        runtime.invalidate_pair_cache()

        # Sender (sga) computes the bearer.
        monkeypatch.setenv("CQ_GROUP", "sga")
        runtime.invalidate_pair_cache()
        bearer = runtime.derive_bearer_token("8th-layer-corp/engineering")

        # Switch to receiver context.
        monkeypatch.setenv("CQ_GROUP", "engineering")
        runtime.invalidate_pair_cache()

        req = _request_with(
            {
                "authorization": f"Bearer {bearer}",
                aigrp.FORWARDER_HEADER: "8th-layer-corp/sga",
            }
        )
        # Should not raise.
        _legacy.require_peer_key(req)

    def test_legacy_bearer_accepted_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "legacy-shared-secret")
        req = _request_with({"authorization": "Bearer legacy-shared-secret"})
        _legacy.require_peer_key(req)  # no raise

    def test_invalid_bearer_rejected_with_legacy_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "legacy-shared-secret")
        req = _request_with({"authorization": "Bearer wrong"})
        with pytest.raises(HTTPException) as exc:
            _legacy.require_peer_key(req)
        assert exc.value.status_code == 401

    def test_missing_authorization_rejected(self) -> None:
        req = _request_with({})
        with pytest.raises(HTTPException) as exc:
            _legacy.require_peer_key(req)
        assert exc.value.status_code == 401

    def test_neither_mode_configured_yields_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No CQ_AIGRP_PEER_KEY, no SSM root.
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("no param")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        req = _request_with({"authorization": "Bearer anything"})
        with pytest.raises(HTTPException) as exc:
            _legacy.require_peer_key(req)
        assert exc.value.status_code == 503

    def test_pair_secret_failure_falls_through_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pair-secret derive fails (no SSM), but legacy bearer matches.
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("ssm down")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "legacy-secret")
        req = _request_with(
            {
                "authorization": "Bearer legacy-secret",
                aigrp.FORWARDER_HEADER: "8th-layer-corp/sga",
            }
        )
        _legacy.require_peer_key(req)  # no raise — legacy fallback kicked in

    def test_cross_enterprise_falls_through_to_legacy(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        """Aggregator from a foreign Enterprise doesn't trigger pair-secret mode."""
        _stub_root_in_ssm(monkeypatch, fake_root)
        monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "legacy-secret")
        # Receiver is 8th-layer-corp, forwarder is from 'aggregator-corp'.
        req = _request_with(
            {
                "authorization": "Bearer legacy-secret",
                aigrp.FORWARDER_HEADER: "aggregator-corp/marketing",
            }
        )
        _legacy.require_peer_key(req)  # legacy bearer passes, pair-secret skipped


# ---------------------------------------------------------------------------
# bootstrap_root_if_needed — first-boot mint.
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_skips_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # CQ_ENTERPRISE_ROOT_BOOTSTRAP unset by fixture.
        client = mock.Mock()
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        assert runtime.bootstrap_root_if_needed() is False
        client.put_parameter.assert_not_called()

    def test_skips_when_root_already_present(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        monkeypatch.setenv("CQ_ENTERPRISE_ROOT_BOOTSTRAP", "true")
        monkeypatch.setenv("CQ_ENTERPRISE_ROOT_KMS_KEY_ID", "alias/x")
        client = _stub_root_in_ssm(monkeypatch, fake_root)
        assert runtime.bootstrap_root_if_needed() is False
        client.put_parameter.assert_not_called()

    def test_mints_when_enabled_and_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_ENTERPRISE_ROOT_BOOTSTRAP", "true")
        monkeypatch.setenv("CQ_ENTERPRISE_ROOT_KMS_KEY_ID", "alias/x")
        client = mock.Mock()
        # First read: missing.
        client.get_parameter.side_effect = RuntimeError("ParameterNotFound")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        assert runtime.bootstrap_root_if_needed() is True
        client.put_parameter.assert_called_once()
        kwargs = client.put_parameter.call_args.kwargs
        assert kwargs["Type"] == "SecureString"
        assert kwargs["KeyId"] == "alias/x"
        assert kwargs["Overwrite"] is False

    def test_refuses_to_mint_without_kms_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_ENTERPRISE_ROOT_BOOTSTRAP", "true")
        # No kms key id.
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("ParameterNotFound")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        assert runtime.bootstrap_root_if_needed() is False
        client.put_parameter.assert_not_called()


# ---------------------------------------------------------------------------
# is_first_deploy_runtime — replaces the static env hardcode.
# ---------------------------------------------------------------------------


class TestIsFirstDeployRuntime:
    def test_no_seed_url_means_first_deploy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CQ_AIGRP_SEED_PEER_URL", raising=False)
        assert runtime.is_first_deploy_runtime() is True

    def test_seed_url_means_not_first_deploy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_AIGRP_SEED_PEER_URL", "http://seed.example/")
        assert runtime.is_first_deploy_runtime() is False

    def test_legacy_env_overrides_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with a seed, the legacy override wins.
        monkeypatch.setenv("CQ_AIGRP_SEED_PEER_URL", "http://seed.example/")
        monkeypatch.setenv("CQ_AIGRP_IS_FIRST_DEPLOY", "true")
        assert runtime.is_first_deploy_runtime() is True


# ---------------------------------------------------------------------------
# aigrp_enabled — Phase 1.0d makes Enterprise root sufficient.
# ---------------------------------------------------------------------------


class TestAigrpEnabled:
    def test_enabled_with_legacy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CQ_AIGRP_PEER_KEY", "anything")
        assert _legacy.aigrp_enabled() is True

    def test_enabled_with_only_root_present(self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes) -> None:
        # No legacy env, but SSM has the root.
        _stub_root_in_ssm(monkeypatch, fake_root)
        assert _legacy.aigrp_enabled() is True

    def test_disabled_when_neither_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("ParameterNotFound")
        monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)
        assert _legacy.aigrp_enabled() is False


# ---------------------------------------------------------------------------
# End-to-end pair derivation matches what envelope.py expects.
# ---------------------------------------------------------------------------


class TestEnvelopeIntegration:
    def test_envelope_signed_one_side_verified_other_side(
        self, monkeypatch: pytest.MonkeyPatch, fake_root: bytes
    ) -> None:
        """A signed/verified envelope cycle using runtime-derived secrets.

        Decision 28 §1.6 — envelope HMAC uses the pair-secret. This test
        proves runtime + envelope agree on the derivation, which is the
        bigger-picture acceptance: an actual cross-L2 message round-trips.
        """
        from cq_server.aigrp import envelope

        _stub_root_in_ssm(monkeypatch, fake_root)
        envelope.reset_replay_cache_for_tests()

        # Sender = engineering. Compute pair_secret directly via runtime.
        monkeypatch.setenv("CQ_GROUP", "engineering")
        runtime.invalidate_pair_cache()
        sender_secret = runtime._pair_secret_for_peer("8th-layer-corp/sga")

        env = envelope.sign_envelope(
            pair_secret=sender_secret,
            src_l2_id="8th-layer-corp/engineering",
            dst_l2_id="8th-layer-corp/sga",
            payload={"hello": "world"},
        )

        # Receiver = sga. Derive its view of the pair-secret independently.
        monkeypatch.setenv("CQ_GROUP", "sga")
        runtime.invalidate_pair_cache()
        receiver_secret = runtime._pair_secret_for_peer("8th-layer-corp/engineering")

        # The two sides MUST agree on the secret.
        assert sender_secret == receiver_secret

        payload = envelope.verify_envelope(
            envelope=env,
            pair_secret=receiver_secret,
            expected_dst_l2_id="8th-layer-corp/sga",
        )
        assert payload == {"hello": "world"}
