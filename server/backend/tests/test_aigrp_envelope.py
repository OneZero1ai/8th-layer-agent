"""AIGRP HMAC envelope tests (Decision 28 §1.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cq_server.aigrp.envelope import (
    EnvelopeVerificationError,
    make_replay_cache,
    sign_envelope,
    verify_envelope,
)
from cq_server.aigrp.pair_secret import derive_pair_secret

ROOT = bytes.fromhex("ab" * 32)


@pytest.fixture
def secret_ab() -> bytes:
    return derive_pair_secret(ROOT, "ent/a", "ent/b")


class TestRoundtrip:
    def test_sign_and_verify_succeeds(self, secret_ab: bytes) -> None:
        env = sign_envelope(
            pair_secret=secret_ab,
            src_l2_id="ent/a",
            dst_l2_id="ent/b",
            payload={"hello": "world"},
        )
        cache = make_replay_cache()
        payload = verify_envelope(
            envelope=env,
            pair_secret=secret_ab,
            expected_dst_l2_id="ent/b",
            replay_cache=cache,
        )
        assert payload == {"hello": "world"}

    def test_pair_id_is_lex_canonical_regardless_of_direction(self, secret_ab: bytes) -> None:
        env_ab = sign_envelope(
            pair_secret=secret_ab,
            src_l2_id="ent/a",
            dst_l2_id="ent/b",
            payload={},
        )
        env_ba = sign_envelope(
            pair_secret=secret_ab,
            src_l2_id="ent/b",
            dst_l2_id="ent/a",
            payload={},
        )
        # Same pair_id, different src/dst — replay cross-direction is
        # blocked by the dst check, not by the pair_id.
        assert env_ab["pair_id"] == env_ba["pair_id"]
        assert env_ab["src_l2_id"] != env_ba["src_l2_id"]


class TestReplayProtection:
    def test_duplicate_msg_id_rejected(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={"x": 1})
        cache = make_replay_cache()
        verify_envelope(envelope=env, pair_secret=secret_ab, expected_dst_l2_id="ent/b", replay_cache=cache)
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(envelope=env, pair_secret=secret_ab, expected_dst_l2_id="ent/b", replay_cache=cache)
        assert excinfo.value.reason == "replay"

    def test_skewed_ts_rejected(self, secret_ab: bytes) -> None:
        # Build an envelope with a ts 6 minutes in the past — over the 5-min
        # tolerance window.
        old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        env = sign_envelope(
            pair_secret=secret_ab,
            src_l2_id="ent/a",
            dst_l2_id="ent/b",
            payload={},
            ts=old,
        )
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "ts_out_of_bounds"

    def test_naive_ts_rejected(self, secret_ab: bytes) -> None:
        env = sign_envelope(
            pair_secret=secret_ab,
            src_l2_id="ent/a",
            dst_l2_id="ent/b",
            payload={},
            ts="2026-05-09T12:00:00",  # no tz
        )
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "malformed"


class TestTampering:
    def test_tampered_payload_breaks_mac(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={"x": 1})
        env["payload"]["x"] = 2
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "mac"

    def test_wrong_secret_rejected(self, secret_ab: bytes) -> None:
        wrong = bytes(32)
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={"x": 1})
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=wrong,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "mac"

    def test_dst_mismatch_rejected(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={"x": 1})
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/c",  # not the addressee
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "dst_mismatch"

    def test_swapped_src_dst_rejected_by_pair_id(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={})
        # Swap src/dst on the wire WITHOUT re-signing — the pair_id was
        # already computed from canonical (lex-min) form, so it survives,
        # but the mac is over the original src/dst — verifier sees mac
        # mismatch.
        env["src_l2_id"] = "ent/b"
        env["dst_l2_id"] = "ent/a"
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/a",
                replay_cache=make_replay_cache(),
            )
        # Could be 'mac' or 'pair_mismatch' depending on canonicalisation;
        # both are valid rejects for a tampered envelope.
        assert excinfo.value.reason in ("mac", "pair_mismatch")


class TestMalformed:
    def test_missing_field_rejected(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={})
        del env["mac"]
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        assert excinfo.value.reason == "malformed"

    def test_wrong_version_rejected(self, secret_ab: bytes) -> None:
        env = sign_envelope(pair_secret=secret_ab, src_l2_id="ent/a", dst_l2_id="ent/b", payload={})
        env["version"] = "v2"
        with pytest.raises(EnvelopeVerificationError) as excinfo:
            verify_envelope(
                envelope=env,
                pair_secret=secret_ab,
                expected_dst_l2_id="ent/b",
                replay_cache=make_replay_cache(),
            )
        # version field is checked early; the mismatch is logged as 'version'.
        assert excinfo.value.reason in ("version", "mac")
