"""Unit tests for the central write-path tenancy resolver (agent#339)."""

from __future__ import annotations

import pytest

from cq_server.tables import DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID
from cq_server.tenancy import resolve_tenancy


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CQ_ENTERPRISE", raising=False)
    monkeypatch.delenv("CQ_GROUP", raising=False)


def test_nondefault_row_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with env set, a real row tenancy is authoritative.
    monkeypatch.setenv("CQ_ENTERPRISE", "env-ent")
    monkeypatch.setenv("CQ_GROUP", "env-grp")
    ent, grp, source = resolve_tenancy({"enterprise_id": "acme", "group_id": "eng"})
    assert (ent, grp, source) == ("acme", "eng", "row")


def test_default_row_falls_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #324/#333/#335 bug case: a default-* row on a CONFIGURED L2 must
    # resolve to env, not the literal defaults.
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "eng")
    ent, grp, source = resolve_tenancy(
        {"enterprise_id": DEFAULT_ENTERPRISE_ID, "group_id": DEFAULT_GROUP_ID}
    )
    assert (ent, grp, source) == ("acme", "eng", "env")


def test_none_user_falls_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # System events / vanished-user race resolve to the L2's env identity.
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "eng")
    assert resolve_tenancy(None) == ("acme", "eng", "env")


def test_empty_row_no_env_returns_default_constants() -> None:
    # Truly unconfigured: empty row + no env → schema constants, source default.
    assert resolve_tenancy({"enterprise_id": "", "group_id": ""}) == (
        DEFAULT_ENTERPRISE_ID,
        DEFAULT_GROUP_ID,
        "default",
    )


def test_default_row_no_env_is_dev_default() -> None:
    # Unconfigured dev L2: row carries the non-empty defaults, no env →
    # keep working on defaults (source default so strict callers can 400).
    ent, grp, source = resolve_tenancy(
        {"enterprise_id": DEFAULT_ENTERPRISE_ID, "group_id": DEFAULT_GROUP_ID}
    )
    assert (ent, grp, source) == (DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID, "default")


def test_partial_env_warns_and_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # One env var set is a misconfiguration — must NOT half-wire; warns loud.
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")  # CQ_GROUP unset
    with caplog.at_level("WARNING"):
        ent, grp, source = resolve_tenancy(
            {"enterprise_id": DEFAULT_ENTERPRISE_ID, "group_id": DEFAULT_GROUP_ID},
            context="unit",
        )
    assert source == "default"
    assert (ent, grp) == (DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID)
    assert any("partial env" in r.message for r in caplog.records)


def test_configured_l2_can_never_resolve_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The agent#339 invariant: with BOTH env vars set, no input resolves to
    # source 'default' — so a fully-configured L2 can't silently default.
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "eng")
    for user in (None, {}, {"enterprise_id": DEFAULT_ENTERPRISE_ID, "group_id": DEFAULT_GROUP_ID}, {"enterprise_id": "", "group_id": ""}):
        _, _, source = resolve_tenancy(user)
        assert source != "default", f"configured L2 defaulted for user={user!r}"
