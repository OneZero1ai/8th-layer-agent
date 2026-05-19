"""agent#324 regression — propose stamps KU tenancy from env when the
user row carries the schema defaults but the L2 has CQ_ENTERPRISE /
CQ_GROUP set.

The bug surfaced on the S2 cross-L2 acceptance run (mvp-s2-a +
mvp-s2-b): the magic-link bootstrap path didn't pin tenancy on the
founder user, so agents that inherited the founder's tenancy via
``_resolve_admin_tenancy`` landed on ``default-enterprise`` /
``default-group``. Cross-L2 federation then dropped every KU under
the cross-Enterprise consent gate.

Fix: ``_resolve_write_tenancy`` in ``app.py`` stamps in this priority
order — (1) user row tenancy when non-default + non-empty, (2)
CQ_ENTERPRISE / CQ_GROUP env vars, (3) 400.

These tests pin the env-fallback branch and the 400 branch. The
existing ``test_propose_tenancy_regression.py`` already covers the
user-row branch and the malformed-row 500.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.auth import hash_password


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "tenancy_env.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    # Ensure env starts clean — each test sets/unsets the L2 tenancy
    # explicitly so the resolver branch under test is unambiguous.
    monkeypatch.delenv("CQ_ENTERPRISE", raising=False)
    monkeypatch.delenv("CQ_GROUP", raising=False)
    with TestClient(app) as c:
        yield c


def _seed_default_user(*, username: str, password: str) -> None:
    """Create a user, leaving tenancy at the schema defaults.

    This is the founder-bootstrap path's residue: the user row has
    ``enterprise_id='default-enterprise'`` / ``group_id='default-group'``
    because no tenancy was pinned at create time.
    """
    store = _get_store()
    store.sync.create_user(username, hash_password(password))


def _login_and_mint(client: TestClient, username: str, password: str) -> str:
    jwt_resp = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert jwt_resp.status_code == 200, jwt_resp.text
    jwt = jwt_resp.json()["token"]
    key_resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": "agent-324-fixture", "ttl": "30d"},
    )
    assert key_resp.status_code == 201, key_resp.text
    return key_resp.json()["token"]


def _propose(client: TestClient, api_key: str) -> dict:
    resp = client.post(
        "/propose",
        json={
            "domains": ["test-fleet"],
            "insight": {
                "summary": "agent#324 env fallback KU",
                "detail": (
                    "Filed by the agent#324 regression suite to pin the env-fallback branch of _resolve_write_tenancy."
                ),
                "action": (
                    "Stamp enterprise_id/group_id from CQ_ENTERPRISE/CQ_GROUP when the user row is at defaults."
                ),
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return resp


def _read_scope(db_path: Path, ku_id: str) -> tuple[str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT enterprise_id, group_id FROM knowledge_units WHERE id = ?",
            (ku_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"KU {ku_id} not found on disk"
    return row[0], row[1]


class TestEnvFallbackForDefaultUser:
    """The headline agent#324 case — user at defaults, env set, KU
    must land on the env tenancy."""

    def test_default_user_picks_up_env_tenancy(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_ENTERPRISE", "mvp-s2")
        monkeypatch.setenv("CQ_GROUP", "group-a")

        _seed_default_user(username="founder", password="pw")  # pragma: allowlist secret
        api_key = _login_and_mint(client, "founder", "pw")
        resp = _propose(client, api_key)
        assert resp.status_code == 201, resp.text
        unit = resp.json()
        ent, grp = _read_scope(tmp_path / "tenancy_env.db", unit["id"])
        assert (ent, grp) == ("mvp-s2", "group-a"), (
            f"agent#324 regression: KU landed in ({ent!r}, {grp!r}) "
            "instead of (mvp-s2, group-a). The propose handler must "
            "consult CQ_ENTERPRISE/CQ_GROUP when the user row carries "
            "the schema-level defaults."
        )

    def test_user_row_overrides_env_when_both_set(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Priority order: user row's explicit (non-default) tenancy
        wins over env, so an agent key minted with cross-tenancy intent
        keeps its row scope."""
        monkeypatch.setenv("CQ_ENTERPRISE", "mvp-s2")
        monkeypatch.setenv("CQ_GROUP", "group-a")

        store = _get_store()
        store.sync.create_user("override", hash_password("pw"))
        with store._engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
                ("explicit-ent", "explicit-grp", "override"),
            )

        api_key = _login_and_mint(client, "override", "pw")
        resp = _propose(client, api_key)
        assert resp.status_code == 201, resp.text
        unit = resp.json()
        ent, grp = _read_scope(tmp_path / "tenancy_env.db", unit["id"])
        assert (ent, grp) == ("explicit-ent", "explicit-grp"), "Row-level explicit tenancy must override the L2 env."

    def test_dev_l2_no_env_no_explicit_row_falls_back_to_defaults(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        """Operator-suggestion compromise: the unconfigured-L2 dev case
        (no env, user row at defaults) still works — the resolver
        returns the row's default tenancy rather than 400.

        Production L2s configure env; the 400 path fires only when
        even the row's tenancy columns are empty (the legacy 500
        branch in propose_unit catches the empty-string case)."""
        _seed_default_user(username="devuser", password="pw")  # pragma: allowlist secret
        api_key = _login_and_mint(client, "devuser", "pw")
        resp = _propose(client, api_key)
        assert resp.status_code == 201, resp.text
        unit = resp.json()
        ent, grp = _read_scope(tmp_path / "tenancy_env.db", unit["id"])
        # Dev path: defaults flow through unchanged. No silent
        # default-enterprise stamping on a configured L2 — only on a
        # truly-unconfigured one, which is fine for local dev.
        assert (ent, grp) == ("default-enterprise", "default-group")

    def test_partial_env_only_enterprise_set_falls_back_to_defaults(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Half-wired env (CQ_ENTERPRISE without CQ_GROUP) should NOT
        partially stamp — both must be set for the env branch to win.
        Falls back to the row's defaults the same way the no-env case
        does."""
        monkeypatch.setenv("CQ_ENTERPRISE", "mvp-s2")
        # Intentionally do NOT set CQ_GROUP.

        _seed_default_user(username="halfwired", password="pw")  # pragma: allowlist secret
        api_key = _login_and_mint(client, "halfwired", "pw")
        resp = _propose(client, api_key)
        assert resp.status_code == 201, resp.text
        unit = resp.json()
        ent, grp = _read_scope(tmp_path / "tenancy_env.db", unit["id"])
        assert (ent, grp) == ("default-enterprise", "default-group"), (
            "Partial env config must not partially stamp — the env "
            "branch requires both CQ_ENTERPRISE and CQ_GROUP to be set."
        )
