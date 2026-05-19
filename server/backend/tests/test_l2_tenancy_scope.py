"""agent#303 — an FO-2-provisioned L2 must run in its own tenancy scope.

Regression pin for the bug where every FO-2-provisioned L2 silently
stamped ``tenant_enterprise=default-enterprise`` / ``tenant_group=
default-group`` on its ``activity_log`` rows, KUs, and agents — because
the marketplace CFN template fed the L2's *own slug* into ``CQ_ENTERPRISE``
instead of the parent Enterprise id, and hardcoded ``CQ_GROUP=default``.

These tests assert the cq-server side of the contract: given the
canonical ``CQ_ENTERPRISE`` / ``CQ_GROUP`` env vars, the runtime
resolves a non-default ``(enterprise_id, group_id)`` rather than
falling through to the ``tables.py`` defaults.
"""

from __future__ import annotations

import pytest

from cq_server import aigrp
from cq_server.aigrp import _legacy
from cq_server.tables import DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID


def test_canonical_env_vars_are_cq_enterprise_and_cq_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime reads ``CQ_ENTERPRISE`` / ``CQ_GROUP`` — nothing else.

    This pins the env-var contract the marketplace template + provisioning
    service must target. If someone renames the env vars on the app side,
    this test fails and forces the template to be updated in lockstep.
    """
    monkeypatch.setenv("CQ_ENTERPRISE", "acme-corp")
    monkeypatch.setenv("CQ_GROUP", "platform")
    assert _legacy.enterprise() == "acme-corp"
    assert _legacy.group() == "platform"
    # Re-exported on the package so callers use ``aigrp.enterprise()``.
    assert aigrp.enterprise() == "acme-corp"
    assert aigrp.group() == "platform"


def test_fo2_provisioned_l2_resolves_non_default_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An L2 carrying its real Enterprise/Group env does NOT fall to default.

    Simulates an FO-2 additional-L2 deploy: the L2's own slug differs from
    its parent Enterprise, and the template now passes the parent Enterprise
    id into ``CQ_ENTERPRISE``. The resolved scope must be the parent
    Enterprise, never ``default-enterprise`` / ``default-group``.
    """
    # FO-2: parent Enterprise "globex", a non-default group, L2 slug would
    # be something else entirely (e.g. "globex-eu-l2") — irrelevant here,
    # because tenancy is driven by CQ_ENTERPRISE/CQ_GROUP, not the slug.
    monkeypatch.setenv("CQ_ENTERPRISE", "globex")
    monkeypatch.setenv("CQ_GROUP", "research")

    resolved_enterprise = _legacy.enterprise()
    resolved_group = _legacy.group()

    assert resolved_enterprise == "globex"
    assert resolved_group == "research"
    assert resolved_enterprise != DEFAULT_ENTERPRISE_ID
    assert resolved_group != DEFAULT_GROUP_ID


def test_unset_env_falls_through_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env set, the runtime falls to the documented defaults.

    This is the *pre-fix* failure mode — kept as a guard so the fallback
    behaviour stays explicit. ``group()`` defaults to bare ``"default"``
    (not ``DEFAULT_GROUP_ID``); both are accepted as "unscoped".
    """
    monkeypatch.delenv("CQ_ENTERPRISE", raising=False)
    monkeypatch.delenv("CQ_GROUP", raising=False)
    assert _legacy.enterprise() == DEFAULT_ENTERPRISE_ID
    assert _legacy.group() == "default"
