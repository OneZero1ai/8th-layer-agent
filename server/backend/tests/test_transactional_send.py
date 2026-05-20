"""End-to-end tests for ``POST /api/v1/transactional/send`` (Decision 34).

Covers all six required scenarios from the implementation brief:

1. HTTP send happy path (HMAC ok, tenancy ok, no suppression → 202 + SES call)
2. Suppression block (pre-seeded suppression row → 409)
3. Tenancy violation (foreign ``to`` → 403)
4. Idempotency dedup (same key in <60s → single SES dispatch)
5. HMAC signature mismatch → 401
6. (L2-side) — covered in test_email_sender_http_client.py
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app
from cq_server.transactional.auth import StaticKeyResolver, compute_signature
from cq_server.transactional.dispatcher import MockSesDispatcher
from cq_server.transactional.idempotency import IdempotencyStore
from cq_server.transactional.routes import (
    _set_dispatcher,
    _set_idempotency_store,
    _set_resolver,
)

ENT = "acme"
GRP = "engineering"
L2_ID = f"{ENT}/{GRP}"
HMAC_KEY = "test-hmac-key-thirty-two-chars-min"  # pragma: allowlist secret

ADMIN_EMAIL = "admin@acme.example.com"
KNOWN_USER_EMAIL = "alice@acme.example.com"
PENDING_INVITEE_EMAIL = "carmen@acme.example.com"
FOREIGN_USER_EMAIL = "evil@globex.example.com"
SUPPRESSED_EMAIL = "bounced@acme.example.com"

ADMIN_PASSWORD = "password123!"  # pragma: allowlist secret


@pytest.fixture
def mock_dispatcher() -> MockSesDispatcher:
    return MockSesDispatcher()


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_dispatcher: MockSesDispatcher,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "transactional.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")

    # Override the three singletons before TestClient starts the lifespan.
    _set_dispatcher(mock_dispatcher)
    _set_resolver(StaticKeyResolver(keys={L2_ID: HMAC_KEY}))
    _set_idempotency_store(IdempotencyStore())

    with TestClient(app) as c:
        store = _get_store()
        pw = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        # Two users: admin (issuer) + alice (recipient). Tenancy on both.
        store.sync.create_user("admin", pw)
        store.sync.create_user("alice", pw)
        store.sync.create_user("evil", pw)
        with store._engine.begin() as conn:  # noqa: SLF001
            conn.execute(
                text(
                    "UPDATE users SET email=:e, enterprise_id=:ent, group_id=:grp "
                    "WHERE username='admin'"
                ),
                {"e": ADMIN_EMAIL, "ent": ENT, "grp": GRP},
            )
            conn.execute(
                text(
                    "UPDATE users SET email=:e, enterprise_id=:ent, group_id=:grp "
                    "WHERE username='alice'"
                ),
                {"e": KNOWN_USER_EMAIL, "ent": ENT, "grp": GRP},
            )
            # Foreign tenancy
            conn.execute(
                text(
                    "UPDATE users SET email=:e, enterprise_id='globex', group_id='engineering' "
                    "WHERE username='evil'"
                ),
                {"e": FOREIGN_USER_EMAIL},
            )
            # Pending invite for carmen — issued by admin (acme/engineering)
            conn.execute(
                text(
                    "INSERT INTO invites "
                    "(jti, email, role, target_l2_id, issued_by, issued_at, expires_at) "
                    "VALUES (:j, :e, 'user', NULL, "
                    "  (SELECT id FROM users WHERE username='admin'), "
                    "  '2026-05-20T00:00:00Z', '2026-05-27T00:00:00Z')"
                ),
                {"j": "test-jti-1", "e": PENDING_INVITEE_EMAIL},
            )
            # Pre-seed a suppression
            conn.execute(
                text(
                    "INSERT INTO transactional_suppression "
                    "(address, reason, suppressed_at, source_event_id) "
                    "VALUES (:a, 'hard_bounce_2026-05-15', '2026-05-15T12:00:00Z', 'evt-1')"
                ),
                {"a": SUPPRESSED_EMAIL},
            )
        yield c


def _valid_body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "from_persona": "invites",
        "to": PENDING_INVITEE_EMAIL,
        "subject": "Welcome to Acme",
        "text": "Click here to claim your account: https://example.com/x",
        "category": "invite_magic_link",
    }
    body.update(over)
    return body


def _signed_post(
    client: TestClient,
    body: dict[str, object],
    *,
    l2_id: str = L2_ID,
    key: str = HMAC_KEY,
    idempotency_key: str | None = None,
) -> object:
    import json

    raw = json.dumps(body).encode("utf-8")
    headers = {
        "X-8L-L2-Id": l2_id,
        "X-8L-Signature": compute_signature(key, raw),
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post("/api/v1/transactional/send", content=raw, headers=headers)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_pending_invitee(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    resp = _signed_post(client, _valid_body())
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["delivery_handle"].startswith("tx_")
    assert body["suppression_check"] == "passed"
    assert body["ses_message_id"] is not None
    # One SES call landed with the right shape.
    assert len(mock_dispatcher.sent) == 1
    sent = mock_dispatcher.sent[0]
    assert sent["from_persona"] == "invites"
    assert sent["to"] == PENDING_INVITEE_EMAIL
    assert sent["subject"] == "Welcome to Acme"


def test_happy_path_known_user(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    body = _valid_body(
        to=KNOWN_USER_EMAIL,
        from_persona="auth",
        category="password_reset",
        subject="Reset your password",
    )
    resp = _signed_post(client, body)
    assert resp.status_code == 202, resp.text
    assert len(mock_dispatcher.sent) == 1


def test_happy_path_security_alert_for_known_user(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    body = _valid_body(
        to=KNOWN_USER_EMAIL,
        from_persona="notifications",
        category="security_alert",
        subject="Security alert",
    )
    resp = _signed_post(client, body)
    assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# 2. Suppression block
# ---------------------------------------------------------------------------


def test_suppression_blocks_send(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    # Need to add suppressed user so tenancy check passes — then suppression
    # should fire BEFORE the SES dispatch.
    store = _get_store()
    store.sync.create_user("bounced", "x")
    with store._engine.begin() as conn:  # noqa: SLF001
        conn.execute(
            text(
                "UPDATE users SET email=:e, enterprise_id=:ent, group_id=:grp "
                "WHERE username='bounced'"
            ),
            {"e": SUPPRESSED_EMAIL, "ent": ENT, "grp": GRP},
        )
    body = _valid_body(to=SUPPRESSED_EMAIL, category="account_event", from_persona="notifications")
    resp = _signed_post(client, body)
    assert resp.status_code == 409, resp.text
    # FastAPI wraps HTTPException.detail in {"detail": ...}.
    detail = resp.json()["detail"]
    assert detail["delivery_handle"] is None
    assert detail["suppression_check"] == "blocked"
    assert "hard_bounce" in detail["reason"]
    # Zero SES dispatches.
    assert len(mock_dispatcher.sent) == 0


# ---------------------------------------------------------------------------
# 3. Tenancy violation
# ---------------------------------------------------------------------------


def test_foreign_user_is_403(client: TestClient, mock_dispatcher: MockSesDispatcher) -> None:
    body = _valid_body(
        to=FOREIGN_USER_EMAIL,
        category="security_alert",
        from_persona="notifications",
    )
    resp = _signed_post(client, body)
    assert resp.status_code == 403, resp.text
    assert len(mock_dispatcher.sent) == 0


def test_unknown_recipient_is_403(client: TestClient, mock_dispatcher: MockSesDispatcher) -> None:
    body = _valid_body(
        to="ghost@nowhere.example.com",
        category="security_alert",
        from_persona="notifications",
    )
    resp = _signed_post(client, body)
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 4. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_header_dedups_within_window(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    body = _valid_body()
    resp1 = _signed_post(client, body, idempotency_key="abc-123")
    assert resp1.status_code == 202
    handle1 = resp1.json()["delivery_handle"]
    # Second identical send should replay the cached response.
    resp2 = _signed_post(client, body, idempotency_key="abc-123")
    assert resp2.status_code == 202
    assert resp2.json()["delivery_handle"] == handle1
    # One SES dispatch total.
    assert len(mock_dispatcher.sent) == 1


def test_idempotency_audit_ref_fallback(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    body = _valid_body(audit_ref="fo2-job-7e3a")
    resp1 = _signed_post(client, body)
    resp2 = _signed_post(client, body)
    assert resp1.json()["delivery_handle"] == resp2.json()["delivery_handle"]
    assert len(mock_dispatcher.sent) == 1


def test_no_idempotency_key_no_dedup(
    client: TestClient, mock_dispatcher: MockSesDispatcher
) -> None:
    body = _valid_body()
    _signed_post(client, body)
    _signed_post(client, body)
    # Both went through — different SES dispatches, different handles.
    assert len(mock_dispatcher.sent) == 2


# ---------------------------------------------------------------------------
# 5. HMAC signature
# ---------------------------------------------------------------------------


def test_missing_l2_id_header_401(client: TestClient) -> None:
    body = _valid_body()
    import json

    raw = json.dumps(body).encode()
    resp = client.post(
        "/api/v1/transactional/send",
        content=raw,
        headers={"X-8L-Signature": compute_signature(HMAC_KEY, raw)},
    )
    assert resp.status_code == 401


def test_missing_signature_header_401(client: TestClient) -> None:
    body = _valid_body()
    import json

    resp = client.post(
        "/api/v1/transactional/send",
        content=json.dumps(body).encode(),
        headers={"X-8L-L2-Id": L2_ID},
    )
    assert resp.status_code == 401


def test_wrong_signature_401(client: TestClient) -> None:
    body = _valid_body()
    resp = _signed_post(client, body, key="wrong-key")
    assert resp.status_code == 401


def test_unknown_l2_id_401(client: TestClient) -> None:
    body = _valid_body()
    resp = _signed_post(client, body, l2_id="unknown/l2")
    assert resp.status_code == 401


def test_malformed_l2_id_401(client: TestClient) -> None:
    body = _valid_body()
    resp = _signed_post(client, body, l2_id="no-slash")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Misc — bad input shapes
# ---------------------------------------------------------------------------


def test_invalid_email_shape_422(client: TestClient) -> None:
    body = _valid_body(to="not-an-email")
    resp = _signed_post(client, body)
    assert resp.status_code == 422


def test_invalid_persona_422(client: TestClient) -> None:
    body = _valid_body(from_persona="marketing")
    resp = _signed_post(client, body)
    assert resp.status_code == 422


def test_invalid_category_422(client: TestClient) -> None:
    body = _valid_body(category="newsletter")
    resp = _signed_post(client, body)
    assert resp.status_code == 422
