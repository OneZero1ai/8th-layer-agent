"""Tests for ``bootstrap_liaison_key_if_needed`` (decision 42 / W2).

The Quick-enablement sidecar (Liaison Server) needs a ``cqa.v1.*`` bearer to talk
to a freshly cold-provisioned L2, but no admin principal exists at cold boot in
the founder path. The provisioning worker seeds the SAME token here
(``CQ_INITIAL_LIAISON_KEY``) and into the LS; on first boot we insert it as a
full-capability agent key owned by a dedicated ``_liaison_service`` user.

Covers: seeds + stored hash matches the verify-path hash; idempotent; no-op when
unset; invalid token skips without raising; and — critically — the
``_liaison_service`` marker never blocks the founder/password admin bootstrap.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from cq_server.api_keys import encode_token, generate_secret, hash_secret
from cq_server.bootstrap_admin import (
    _LIAISON_USERNAME,
    _users_exist,
    bootstrap_first_admin_if_needed,
    bootstrap_liaison_key_if_needed,
)
from cq_server.store import SqliteStore

_PEPPER = "test-pepper-0123456789abcdef"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    from cq_server.migrations import run_migrations

    db = tmp_path / "liaison.db"
    run_migrations(f"sqlite:///{db}")
    s = SqliteStore(db_path=db)
    yield s
    s.close_sync()


def _make_token() -> tuple[uuid.UUID, str, str]:
    key_id = uuid.uuid4()
    secret = generate_secret()
    return key_id, secret, encode_token(key_id=key_id, secret=secret)


def _api_key_hash(store: SqliteStore, key_id_hex: str) -> str | None:
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text("SELECT key_hash FROM api_keys WHERE id = :id"), {"id": key_id_hex}
        ).fetchone()
    return row[0] if row else None


async def test_seeds_liaison_key(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    key_id, secret, token = _make_token()
    monkeypatch.setenv("CQ_INITIAL_LIAISON_KEY", token)
    monkeypatch.setenv("CQ_API_KEY_PEPPER", _PEPPER)
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "engineering")

    await bootstrap_liaison_key_if_needed(store)

    user = await store.get_user(_LIAISON_USERNAME)
    assert user is not None
    # The stored hash must equal what the server's verify path computes from the
    # plaintext secret + pepper — i.e. the seeded key will actually authenticate.
    assert _api_key_hash(store, key_id.hex) == hash_secret(secret, pepper=_PEPPER)


async def test_idempotent(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, token = _make_token()
    monkeypatch.setenv("CQ_INITIAL_LIAISON_KEY", token)
    monkeypatch.setenv("CQ_API_KEY_PEPPER", _PEPPER)

    await bootstrap_liaison_key_if_needed(store)
    await bootstrap_liaison_key_if_needed(store)  # second run: must be a no-op

    with store._engine.connect() as conn:  # noqa: SLF001
        n = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE username = :u"),
            {"u": _LIAISON_USERNAME},
        ).scalar()
    assert n == 1


async def test_noop_when_unset(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CQ_INITIAL_LIAISON_KEY", raising=False)
    await bootstrap_liaison_key_if_needed(store)
    assert await store.get_user(_LIAISON_USERNAME) is None


async def test_invalid_token_skips_without_raising(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CQ_INITIAL_LIAISON_KEY", "not-a-valid-cqa-token")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", _PEPPER)
    await bootstrap_liaison_key_if_needed(store)  # must not raise
    assert await store.get_user(_LIAISON_USERNAME) is None


async def test_does_not_block_admin_bootstrap(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The _liaison_service marker must not make the founder bootstrap skip."""
    _, _, token = _make_token()
    monkeypatch.setenv("CQ_INITIAL_LIAISON_KEY", token)
    monkeypatch.setenv("CQ_API_KEY_PEPPER", _PEPPER)
    await bootstrap_liaison_key_if_needed(store)

    # _liaison_service exists, but no REAL user does — so the admin bootstraps
    # must still consider the L2 un-onboarded.
    assert await _users_exist(store) is False

    monkeypatch.setenv("CQ_INITIAL_ADMIN_EMAIL", "founder@example.com")
    await bootstrap_first_admin_if_needed(store)
    with store._engine.connect() as conn:  # noqa: SLF001
        sys_n = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE username = '_bootstrap_system'")
        ).scalar()
    assert sys_n == 1  # the founder bootstrap ran despite the liaison user
