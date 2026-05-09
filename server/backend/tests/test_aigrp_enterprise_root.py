"""Tests for the SSM-backed Enterprise AIGRP root (Decision 28 §1.2)."""

from __future__ import annotations

from unittest import mock

import pytest

from cq_server.aigrp import enterprise_root


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    enterprise_root.invalidate_cache()
    yield
    enterprise_root.invalidate_cache()


@pytest.fixture
def fake_ssm() -> mock.Mock:
    client = mock.Mock()
    return client


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: mock.Mock) -> None:
    monkeypatch.setattr(enterprise_root, "_get_ssm_client", lambda: client)


class TestRead:
    def test_get_root_decodes_hex(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "ab" * 32}}
        _patch_client(monkeypatch, fake_ssm)

        root = enterprise_root.get_enterprise_root("acme")

        assert len(root) == 32
        assert root == bytes([0xAB] * 32)
        fake_ssm.get_parameter.assert_called_once_with(
            Name="/8th-layer/aigrp/enterprise-root/acme",
            WithDecryption=True,
        )

    def test_corrupt_value_rejected(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "not-hex"}}
        _patch_client(monkeypatch, fake_ssm)
        with pytest.raises(ValueError, match="hex"):
            enterprise_root.get_enterprise_root("acme")

    def test_short_value_rejected(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "ab" * 16}}  # 16 bytes
        _patch_client(monkeypatch, fake_ssm)
        with pytest.raises(ValueError, match="32 bytes"):
            enterprise_root.get_enterprise_root("acme")


class TestCache:
    def test_caches_within_ttl(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "ab" * 32}}
        _patch_client(monkeypatch, fake_ssm)

        a = enterprise_root.get_enterprise_root("acme")
        b = enterprise_root.get_enterprise_root("acme")
        assert a == b
        # One SSM hit, two callsite reads.
        assert fake_ssm.get_parameter.call_count == 1

    def test_invalidate_forces_refetch(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "ab" * 32}}
        _patch_client(monkeypatch, fake_ssm)
        enterprise_root.get_enterprise_root("acme")
        enterprise_root.invalidate_cache("acme")
        enterprise_root.get_enterprise_root("acme")
        assert fake_ssm.get_parameter.call_count == 2

    def test_ttl_zero_disables_cache(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        monkeypatch.setenv("CQ_AIGRP_ROOT_CACHE_TTL_SEC", "0")
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "ab" * 32}}
        _patch_client(monkeypatch, fake_ssm)
        enterprise_root.get_enterprise_root("acme")
        enterprise_root.get_enterprise_root("acme")
        assert fake_ssm.get_parameter.call_count == 2

    def test_separate_enterprises_have_separate_entries(
        self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock
    ) -> None:
        fake_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "ab" * 32}},
            {"Parameter": {"Value": "cd" * 32}},
        ]
        _patch_client(monkeypatch, fake_ssm)
        a = enterprise_root.get_enterprise_root("acme")
        b = enterprise_root.get_enterprise_root("globex")
        assert a != b
        assert fake_ssm.get_parameter.call_count == 2


class TestBootstrap:
    def test_bootstrap_writes_securestring(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        _patch_client(monkeypatch, fake_ssm)
        root = enterprise_root.bootstrap_enterprise_root("acme", "alias/8th-layer/aigrp/acme")
        assert len(root) == 32
        fake_ssm.put_parameter.assert_called_once()
        kwargs = fake_ssm.put_parameter.call_args.kwargs
        assert kwargs["Type"] == "SecureString"
        assert kwargs["KeyId"] == "alias/8th-layer/aigrp/acme"
        assert kwargs["Name"] == "/8th-layer/aigrp/enterprise-root/acme"
        # Hex-encoded.
        assert kwargs["Value"] == root.hex()
        assert kwargs["Overwrite"] is False

    def test_bootstrap_requires_kms_key(self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock) -> None:
        _patch_client(monkeypatch, fake_ssm)
        with pytest.raises(ValueError, match="kms_key_id"):
            enterprise_root.bootstrap_enterprise_root("acme", "")

    def test_bootstrap_overwrite_flag_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, fake_ssm: mock.Mock
    ) -> None:
        _patch_client(monkeypatch, fake_ssm)
        enterprise_root.bootstrap_enterprise_root("acme", "alias/x", overwrite=True)
        assert fake_ssm.put_parameter.call_args.kwargs["Overwrite"] is True


class TestPathValidation:
    def test_slash_in_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="slash-free"):
            enterprise_root._param_path("acme/sub")

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            enterprise_root._param_path("")
