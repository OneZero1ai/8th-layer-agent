"""Tests for the password-login admin bootstrap (agent#165).

Covers ``bootstrap_password_admin_if_needed``:

* seeds an admin when CQ_INITIAL_ADMIN_PASSWORD is set and users empty
* the seeded password actually verifies via the login hash check
* tenancy (enterprise_id / group_id) is pinned from CQ_ENTERPRISE /
  CQ_GROUP, not left on the column server_default
* idempotent — a no-op once any non-system user exists
* a no-op when CQ_INITIAL_ADMIN_PASSWORD is unset
* honours CQ_INITIAL_ADMIN_USERNAME
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from cq_server.auth import hash_password, verify_password
from cq_server.bootstrap_admin import bootstrap_password_admin_if_needed
from cq_server.store import SqliteStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    from cq_server.migrations import run_migrations

    db = tmp_path / "bootstrap.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _user_row(store: SqliteStore, username: str) -> tuple[str, str, str, str] | None:
    """Return (password_hash, role, enterprise_id, group_id) or None."""
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text(
                "SELECT password_hash, role, enterprise_id, group_id "
                "FROM users WHERE username = :u"
            ),
            {"u": username},
        ).fetchone()
    return tuple(row) if row else None  # type: ignore[return-value]


async def test_seeds_admin_when_password_set(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CQ_INITIAL_ADMIN_PASSWORD", "s3cret-bootstrap-pw")
    monkeypatch.setenv("CQ_ENTERPRISE", "8th-layer-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    monkeypatch.delenv("CQ_INITIAL_ADMIN_USERNAME", raising=False)

    await bootstrap_password_admin_if_needed(store)

    row = _user_row(store, "admin")
    assert row is not None, "admin user should have been seeded"
    password_hash, role, enterprise_id, group_id = row
    assert role == "admin"
    assert verify_password("s3cret-bootstrap-pw", password_hash)
    # Tenancy pinned to the L2's identity, not the default-* server_default.
    assert enterprise_id == "8th-layer-corp"
    assert group_id == "engineering"


async def test_noop_when_password_unset(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CQ_INITIAL_ADMIN_PASSWORD", raising=False)

    await bootstrap_password_admin_if_needed(store)

    assert _user_row(store, "admin") is None


async def test_idempotent_when_user_already_exists(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-existing operator-created user — bootstrap must not run.
    await store.create_user("founder", hash_password("orig-pw"), role="admin")
    monkeypatch.setenv("CQ_INITIAL_ADMIN_PASSWORD", "would-be-new-pw")

    await bootstrap_password_admin_if_needed(store)

    assert _user_row(store, "admin") is None, "must not seed over an existing user base"
    founder = _user_row(store, "founder")
    assert founder is not None and verify_password("orig-pw", founder[0])


async def test_honours_custom_username(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CQ_INITIAL_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.setenv("CQ_INITIAL_ADMIN_USERNAME", "operator")
    monkeypatch.delenv("CQ_ENTERPRISE", raising=False)
    monkeypatch.delenv("CQ_GROUP", raising=False)

    await bootstrap_password_admin_if_needed(store)

    assert _user_row(store, "admin") is None
    row = _user_row(store, "operator")
    assert row is not None and row[1] == "admin"
    # Tenancy env unset → fall back to the column server_default.
    assert row[2] == "default-enterprise"
    assert row[3] == "default-group"
