"""Regression tests for #89 — KU tenancy must come from auth claims.

The bug surfaced in the moscowmul3 onboarding (2026-05-05): KUs
proposed via ``POST /propose`` with a valid API key landed in
``enterprise_id=default-enterprise`` / ``group_id=default-group``
regardless of the L2's configured Enterprise. Root cause: ``INSERT_UNIT``
omitted the tenancy columns and let the schema-level ``server_default``
populate them.

This file pins the fix:

* The propose handler resolves tenancy from the authenticated user's
  row and passes it to ``store.insert(...)`` explicitly.
* The store accepts ``enterprise_id`` / ``group_id`` kwargs and writes
  them through ``INSERT_UNIT_WITH_TENANCY``.
* The legacy fixture path (``store.sync.insert(unit)`` with no kwargs)
  still uses the defaults, so existing test fixtures are unaffected.

If a future change reintroduces the bug — e.g. by routing all inserts
through ``INSERT_UNIT`` regardless of caller scope — these tests fail
loudly. The smoke test is verbatim the workaround SQL from the bug
report (``SELECT id, enterprise_id, group_id FROM knowledge_units``).
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
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "tenancy.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


def _seed_user(*, username: str, password: str, enterprise_id: str, group_id: str) -> None:
    """Create a user, then UPDATE their tenancy columns to non-default values.

    ``store.sync.create_user`` lands the row at ``default-enterprise`` /
    ``default-group``. The follow-up UPDATE is what makes this user
    represent a real tenant — same recipe ``test_consults.py`` uses.
    """
    store = _get_store()
    store.sync.create_user(username, hash_password(password))
    with store._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            (enterprise_id, group_id, username),
        )


def _login_jwt(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _mint_api_key(client: TestClient, jwt_token: str, *, name: str = "regression-89") -> str:
    """Login + mint an API key. Propose requires API-key auth, not JWT."""
    resp = client.post(
        "/auth/api-keys",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": name, "ttl": "30d"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _login_and_mint(client: TestClient, username: str, password: str) -> str:
    """End-to-end: login with password, mint an API key, return the API key."""
    jwt = _login_jwt(client, username, password)
    return _mint_api_key(client, jwt)


def _propose(client: TestClient, api_key: str, **overrides: object) -> dict:
    body = {
        "domains": ["agent-memory", "test-fleet"],
        "insight": {
            "summary": "Tenancy regression test KU",
            "detail": "Filed by the #89 regression suite to pin the propose-handler tenancy fix.",
            "action": "Read enterprise_id from the authenticated user row, never the schema default.",
        },
    }
    body.update(overrides)
    resp = client.post(
        "/propose",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _read_scope(db_path: Path, ku_id: str) -> tuple[str, str]:
    """Inspect the on-disk row for the KU. Bypasses the store entirely."""
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


# ---------------------------------------------------------------------------
# The headline regression — KU written via API matches authenticated user.
# ---------------------------------------------------------------------------


class TestProposeHonoursAuthClaims:
    def test_ku_lands_in_authed_users_enterprise_not_default(self, client: TestClient, tmp_path: Path) -> None:
        """The bug: the KU lands in default-enterprise. The fix: it lands in moscowmul3.

        Verbatim the bug-report repro shape — same user setup, same
        propose body, same SELECT against the on-disk DB.
        """
        _seed_user(
            username="bran",
            password="pw",
            enterprise_id="moscowmul3",
            group_id="engineering",
        )
        api_key = _login_and_mint(client, "bran", "pw")
        unit = _propose(client, api_key)

        ent, grp = _read_scope(tmp_path / "tenancy.db", unit["id"])
        assert ent == "moscowmul3", (
            f"#89 regression: KU landed in {ent!r} instead of moscowmul3. "
            "The propose handler must derive enterprise_id from the user "
            "row, not the schema-level server_default."
        )
        assert grp == "engineering", f"#89 regression: KU group_id is {grp!r} instead of engineering"

    def test_ku_never_lands_in_default_enterprise_for_authed_user(self, client: TestClient, tmp_path: Path) -> None:
        """Cover the inverse: even with an obviously-non-default user,
        the row never falls back to ``default-enterprise``.

        Different enterprise/group from the headline test — guards
        against a regression where the fix worked for one specific
        Enterprise but fell back for another (e.g. accidental string
        equality).
        """
        _seed_user(
            username="alice",
            password="pw",
            enterprise_id="acme",
            group_id="solutions",
        )
        api_key = _login_and_mint(client, "alice", "pw")
        unit = _propose(client, api_key)

        ent, grp = _read_scope(tmp_path / "tenancy.db", unit["id"])
        assert ent != "default-enterprise"
        assert grp != "default-group"
        assert (ent, grp) == ("acme", "solutions")

    def test_two_users_two_enterprises_no_cross_contamination(self, client: TestClient, tmp_path: Path) -> None:
        """Two users in two different Enterprises propose; each KU lands
        in its own user's tenant. Pins that the resolution is per-call,
        not cached or thread-local.
        """
        _seed_user(
            username="moscow_user",
            password="pw",
            enterprise_id="moscowmul3",
            group_id="engineering",
        )
        _seed_user(
            username="acme_user",
            password="pw",
            enterprise_id="acme",
            group_id="solutions",
        )
        moscow_key = _login_and_mint(client, "moscow_user", "pw")
        acme_key = _login_and_mint(client, "acme_user", "pw")

        moscow_ku = _propose(client, moscow_key)
        acme_ku = _propose(client, acme_key)

        moscow_scope = _read_scope(tmp_path / "tenancy.db", moscow_ku["id"])
        acme_scope = _read_scope(tmp_path / "tenancy.db", acme_ku["id"])
        assert moscow_scope == ("moscowmul3", "engineering")
        assert acme_scope == ("acme", "solutions")

    def test_propose_rejects_when_user_row_missing_tenancy(self, client: TestClient, tmp_path: Path) -> None:
        """Defensive: if a future migration somehow leaves a user row
        with NULL tenancy, the handler should fail loudly rather than
        silently writing the KU into the schema default. We simulate
        this by NULL-ing the columns on a real user."""
        store = _get_store()
        store.sync.create_user("nullable", hash_password("pw"))
        # The migration enforces NOT NULL on these columns, so we have
        # to drop the constraint to exercise the defensive path. Doing
        # the SQL via the raw connection bypasses the schema check.
        with store._engine.begin() as conn:
            # SQLite allows NULL writes when ALTER TABLE has not yet
            # rebuilt the column; we simulate the malformed-row state
            # by writing empty strings (the handler treats them the
            # same way: 500 rather than silent default).
            conn.exec_driver_sql("UPDATE users SET enterprise_id = '', group_id = '' WHERE username = 'nullable'")

        api_key = _login_and_mint(client, "nullable", "pw")
        resp = client.post(
            "/propose",
            json={
                "domains": ["test-fleet"],
                "insight": {
                    "summary": "Should never land",
                    "detail": "Bran filed this regression so the handler 500s "
                    "rather than silently writing into default-enterprise.",
                    "action": "Restore the user's tenancy columns and retry.",
                },
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 500
        assert "tenancy claims" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Backwards-compatibility — legacy ``store.sync.insert(unit)`` path unchanged.
# ---------------------------------------------------------------------------


class TestLegacyInsertPathPreserved:
    def test_no_kwargs_insert_still_uses_server_defaults(self, tmp_path: Path) -> None:
        """The legacy fixture path (``store.sync.insert(unit)``) doesn't
        carry tenancy — by design, since unit-test fixtures don't always
        manufacture a tenant. The fix preserves that behaviour: the row
        falls back to the schema-level defaults.

        If this test fails it means the #89 fix accidentally broke the
        zero-arg insert path; existing tenancy_columns + sqlite_store
        tests would fire immediately. Pinning here makes the contract
        explicit.
        """
        from cq.models import Insight, create_knowledge_unit

        from cq_server.migrations import run_migrations
        from cq_server.store import SqliteStore

        db = tmp_path / "legacy.db"
        run_migrations(f"sqlite:///{db}")
        store = SqliteStore(db_path=db)
        try:
            unit = create_knowledge_unit(
                domains=["test-fleet"],
                insight=Insight(
                    summary="Legacy insert path",
                    detail="No kwargs => server_default fills the columns.",
                    action="Verify the fix preserves backwards compat.",
                ),
            )
            store.sync.insert(unit)
            ent, grp = _read_scope(db, unit.id)
            assert ent == "default-enterprise"
            assert grp == "default-group"
        finally:
            store.close_sync()
