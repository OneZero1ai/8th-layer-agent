"""Sprint 4 Track A — cross-Enterprise consult forward (sender side).

Phase 1 covers:
- bearer derivation from a peering's bilateral signatures (HKDF)
- find_active_directory_peering store method
- consults.py /request routes cross-Enterprise via the directory peering
  mirror when target enterprise differs from ours
- 403 'no active peering' when no peering covers the target's enterprise
- 403 when the peering exists but the target L2 isn't in the roster
- consult_logging_policy from the peering applies to the asker-side
  mirror-write (mutual_log_required full body, summary_only_log redacted,
  no_log_consults skip-write)
- forward call goes to the new x-enterprise-forward-request path with
  per-pair bearer + Ed25519 sig

Phase 2 (receiver side) is a separate test file landing with that PR.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import bcrypt
import httpx
import pytest
from fastapi.testclient import TestClient

from cq_server import consults, forward_sign
from cq_server.app import _get_store, app

ALICE = "alice"  # acme/engineering — this L2

ACME_PEER_KEY = "test-acme-peer-key-thirty-two-chars"

# A pre-built peering record from acme to globex (test enterprises). The
# offer / accept signatures are arbitrary 64-byte b64u strings — the
# sender side only derives the bearer from them, never re-verifies.
_FAKE_OFFER_SIG = "iuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiuiu"
_FAKE_ACCEPT_SIG = "abababababababababababababababababababababababababababababababababababababababababababab"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "xent.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_AIGRP_PEER_KEY", ACME_PEER_KEY)
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    monkeypatch.setenv("CQ_AIGRP_L2_PRIVKEY_PATH", str(tmp_path / "l2.key"))

    # Reset cached privkey across tests
    forward_sign._cached_privkey = None
    forward_sign._cached_loaded = False

    with TestClient(app) as c:
        store = _get_store()
        # Seed alice as a real user
        pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        store.sync.create_user(ALICE, pw)
        with store._engine.begin() as _c:
            _c.exec_driver_sql(
                "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
                ("acme", "engineering", ALICE),
            )
        yield c


def _login(client: TestClient, who: str = ALICE) -> str:
    r = client.post("/api/v1/auth/login", json={"username": who, "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_peering(
    *,
    offer_id: str,
    from_ent: str = "acme",
    to_ent: str = "globex",
    status: str = "active",
    expires_at: str = "2099-01-01T00:00:00Z",
    consult_logging_policy: str = "mutual_log_required",
    to_l2_endpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if to_l2_endpoints is None:
        to_l2_endpoints = [
            {
                "l2_id": f"{to_ent}/eng",
                "endpoint_url": f"https://{to_ent}-eng.example.com",
                "groups": ["eng"],
            }
        ]
    store = _get_store()
    store.sync.upsert_directory_peering(
        offer_id=offer_id,
        from_enterprise=from_ent,
        to_enterprise=to_ent,
        status=status,
        content_policy="summary_only",
        consult_logging_policy=consult_logging_policy,
        topic_filters_json="[]",
        active_from="2026-01-01T00:00:00Z",
        expires_at=expires_at,
        offer_payload_canonical='{"offer_id":"' + offer_id + '"}',
        offer_signature_b64u=_FAKE_OFFER_SIG,
        offer_signing_key_id="from-key",
        accept_payload_canonical='{"accepted":true}',
        accept_signature_b64u=_FAKE_ACCEPT_SIG,
        accept_signing_key_id="to-key",
        last_synced_at="2026-05-02T00:00:00Z",
        to_l2_endpoints_json=json.dumps(to_l2_endpoints),
    )
    return {
        "offer_id": offer_id,
        "from_enterprise": from_ent,
        "to_enterprise": to_ent,
        "to_l2_endpoints": to_l2_endpoints,
    }


def _capture_x_enterprise_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Patch consults._x_enterprise_forward_request to record the call rather than POST."""
    captured: list[dict[str, Any]] = []

    def _fake_x_enterprise_forward(
        peering: dict[str, Any],
        target_endpoint: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        captured.append({
            "peering": peering,
            "target_endpoint": target_endpoint,
            "payload": payload,
        })

    monkeypatch.setattr(consults, "_x_enterprise_forward_request", _fake_x_enterprise_forward)
    return captured


# ---------------------------------------------------------------------------
# Bearer derivation
# ---------------------------------------------------------------------------


def test_bearer_derivation_is_deterministic() -> None:
    """Both sides of a peering compute the same bearer."""
    b1 = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    b2 = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    assert b1 == b2
    assert len(b1) >= 40  # base64url of 32 bytes


def test_bearer_derivation_changes_with_signature_change() -> None:
    """A renewed peering (different sigs) yields a different bearer."""
    b1 = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    new_offer = _FAKE_OFFER_SIG.replace("i", "j")  # different valid b64u
    b2 = forward_sign.derive_peering_bearer(new_offer, _FAKE_ACCEPT_SIG)
    assert b1 != b2


def test_bearer_swap_yields_different_bearer() -> None:
    """Argument order matters — sigs are concatenated, not symmetric."""
    a = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    b = forward_sign.derive_peering_bearer(_FAKE_ACCEPT_SIG, _FAKE_OFFER_SIG)
    assert a != b


# ---------------------------------------------------------------------------
# Store: find_active_directory_peering
# ---------------------------------------------------------------------------


def test_find_active_peering_returns_match(client: TestClient) -> None:
    _seed_peering(offer_id="off_a")
    p = _get_store().sync.find_active_directory_peering(
        from_enterprise="acme", to_enterprise="globex"
    )
    assert p is not None
    assert p["offer_id"] == "off_a"


def test_find_active_peering_is_bidirectional(client: TestClient) -> None:
    """Peering from A→B is also queryable as B→A."""
    _seed_peering(offer_id="off_b", from_ent="acme", to_ent="globex")
    p = _get_store().sync.find_active_directory_peering(
        from_enterprise="globex", to_enterprise="acme"
    )
    assert p is not None
    assert p["offer_id"] == "off_b"


def test_find_active_peering_skips_expired(client: TestClient) -> None:
    """Past expires_at = no row."""
    _seed_peering(offer_id="off_old", expires_at="2020-01-01T00:00:00Z")
    p = _get_store().sync.find_active_directory_peering(
        from_enterprise="acme", to_enterprise="globex"
    )
    assert p is None


def test_find_active_peering_skips_non_active(client: TestClient) -> None:
    _seed_peering(offer_id="off_pending", status="pending")
    p = _get_store().sync.find_active_directory_peering(
        from_enterprise="acme", to_enterprise="globex"
    )
    assert p is None


# ---------------------------------------------------------------------------
# Cross-Enterprise consult routing on /api/v1/consults/request
# ---------------------------------------------------------------------------


def test_cross_enterprise_routes_via_directory_peering(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_peering(offer_id="off_e2e")
    captured = _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)

    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "globex/eng",
            "to_persona": "their_alice",
            "content": "cross-enterprise hello",
        },
    )
    assert r.status_code == 201, r.text
    assert len(captured) == 1
    forward = captured[0]
    assert forward["peering"]["offer_id"] == "off_e2e"
    assert forward["target_endpoint"]["l2_id"] == "globex/eng"
    assert forward["payload"]["to_l2_id"] == "globex/eng"
    assert forward["payload"]["content"] == "cross-enterprise hello"


def test_cross_enterprise_no_peering_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)
    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "rival/somewhere",
            "to_persona": "they",
            "content": "x",
        },
    )
    assert r.status_code == 403, r.text
    assert "no active peering" in r.json()["detail"].lower()
    assert captured == []


def test_cross_enterprise_target_l2_not_in_roster_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peering covers globex/eng but client asks for globex/marketing."""
    _seed_peering(
        offer_id="off_partial",
        to_l2_endpoints=[
            {"l2_id": "globex/eng", "endpoint_url": "https://globex-eng.example.com", "groups": ["eng"]},
        ],
    )
    captured = _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)
    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "globex/marketing",
            "to_persona": "they",
            "content": "x",
        },
    )
    assert r.status_code == 403, r.text
    assert captured == []


def test_cross_enterprise_logging_policy_summary_only_redacts_local_mirror(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_peering(offer_id="off_summary", consult_logging_policy="summary_only_log")
    _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)

    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "globex/eng",
            "to_persona": "they",
            "content": "secret content here",
        },
    )
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]

    # Local mirror should be redacted per the peering's logging policy.
    msgs = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers={"authorization": f"Bearer {token}"},
    )
    body = msgs.json()
    assert body["messages"][0]["content"] == "<redacted: summary_only_log>"


def test_cross_enterprise_logging_policy_no_log_skips_message_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_peering(offer_id="off_nolog", consult_logging_policy="no_log_consults")
    _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)

    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "globex/eng",
            "to_persona": "they",
            "content": "ephemeral",
        },
    )
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]

    # Thread row exists (audit point: who tried to consult whom)
    # but no message row was written.
    msgs = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers={"authorization": f"Bearer {token}"},
    )
    body = msgs.json()
    assert body["messages"] == []


def test_cross_enterprise_mutual_log_writes_full_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default policy mutual_log_required → asker side sees full content."""
    _seed_peering(offer_id="off_mutual")
    _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)

    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "globex/eng",
            "to_persona": "they",
            "content": "full content visible",
        },
    )
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]

    msgs = client.get(
        f"/api/v1/consults/{thread_id}/messages",
        headers={"authorization": f"Bearer {token}"},
    )
    assert msgs.json()["messages"][0]["content"] == "full content visible"


def test_malformed_to_l2_id_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_l2_id without enterprise/group separator is rejected before lookup."""
    _capture_x_enterprise_forwards(monkeypatch)
    token = _login(client)
    r = client.post(
        "/api/v1/consults/request",
        headers={"authorization": f"Bearer {token}"},
        json={
            "to_l2_id": "no-slash-here",
            "to_persona": "they",
            "content": "x",
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# _x_enterprise_forward_request — wire-shape unit test (no real httpx)
# ---------------------------------------------------------------------------


def test_x_enterprise_forward_request_sends_bearer_and_sig_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward call uses peering-derived bearer + per-L2 Ed25519 sig + offer-id header."""
    _seed_peering(offer_id="off_wire")
    captured: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

        def post(
            self, url: str, *, headers: dict[str, str], json: dict[str, Any]
        ) -> httpx.Response:
            captured.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(201)

    monkeypatch.setattr(consults.httpx, "Client", _FakeClient)

    peering = _get_store().sync.find_active_directory_peering(
        from_enterprise="acme", to_enterprise="globex"
    )
    assert peering is not None
    target_endpoint = json.loads(peering["to_l2_endpoints_json"])[0]

    payload = {
        "thread_id": "th_x",
        "from_l2_id": "acme/engineering",
        "to_l2_id": "globex/eng",
        "content": "hi",
    }
    consults._x_enterprise_forward_request(peering, target_endpoint, payload)

    assert len(captured) == 1
    sent = captured[0]
    assert sent["url"].endswith("/api/v1/consults/x-enterprise-forward-request")
    expected_bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    assert sent["headers"]["authorization"] == f"Bearer {expected_bearer}"
    assert sent["headers"]["x-8l-peering-offer-id"] == "off_wire"
    assert sent["headers"]["x-8l-forwarder-l2-id"] == "acme/engineering"
    # Ed25519 sig is generated lazily on first call (key file in tmp_path)
    assert "x-8l-forwarder-sig" in sent["headers"]
    assert sent["json"] == payload


# ---------------------------------------------------------------------------
# Phase 2 — receiver side: /api/v1/consults/x-enterprise-forward-request
# ---------------------------------------------------------------------------
#
# These tests exercise the receiver. The fixture's L2 is acme/engineering;
# we simulate forwards arriving from globex (the OTHER side of the peering).
# We seed peerings with from_ent="globex", to_ent="acme" so this L2 is the
# receiver and globex is the sender.


def _x_enterprise_headers(
    *,
    bearer: str,
    forwarder_l2_id: str = "globex/eng",
    offer_id: str = "off_recv",
) -> dict[str, str]:
    return {
        "authorization": f"Bearer {bearer}",
        "x-8l-forwarder-l2-id": forwarder_l2_id,
        "x-8l-peering-offer-id": offer_id,
    }


def _x_enterprise_payload(
    thread_id: str = "th_recv_1",
    message_id: str = "msg_recv_1",
    content: str = "incoming from globex",
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "from_l2_id": "globex/eng",
        "from_persona": "their_alice",
        "to_l2_id": "acme/engineering",
        "to_persona": ALICE,
        "subject": "x-ent test",
        "content": content,
        "created_at": "2026-05-02T10:00:00Z",
    }


def test_x_enterprise_receiver_happy_path(client: TestClient) -> None:
    """Bearer matches, body identity matches, forwarder enterprise is the peer."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "mirrored"
    assert body["logging_policy_applied"] == "mutual_log_required"


def test_x_enterprise_receiver_bad_bearer_401(client: TestClient) -> None:
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer="not-the-real-bearer"),
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 401, r.text
    assert "bearer" in r.json()["detail"].lower()


def test_x_enterprise_receiver_missing_bearer_401(client: TestClient) -> None:
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    headers = _x_enterprise_headers(bearer="x")
    del headers["authorization"]  # remove the bearer
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=headers,
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 401, r.text


def test_x_enterprise_receiver_missing_offer_id_400(client: TestClient) -> None:
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    headers = _x_enterprise_headers(bearer=bearer)
    del headers["x-8l-peering-offer-id"]
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=headers,
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 400, r.text
    assert "offer-id" in r.json()["detail"].lower()


def test_x_enterprise_receiver_unknown_offer_id_403(client: TestClient) -> None:
    """The sender claims a peering offer_id we don't have on file."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer, offer_id="off_unknown"),
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 403, r.text
    assert "no active peering" in r.json()["detail"].lower()


def test_x_enterprise_receiver_body_header_mismatch_403(client: TestClient) -> None:
    """X-8L-Forwarder-L2-Id and body.from_l2_id must agree."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    payload = _x_enterprise_payload()
    payload["from_l2_id"] = "globex/marketing"  # different from header
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),  # header says globex/eng
        json=payload,
    )
    assert r.status_code == 403, r.text
    assert "mismatch" in r.json()["detail"].lower()


def test_x_enterprise_receiver_wrong_enterprise_403(client: TestClient) -> None:
    """Forwarder claims to be from an enterprise that's not the peer."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    payload = _x_enterprise_payload()
    payload["from_l2_id"] = "rival/eng"
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer, forwarder_l2_id="rival/eng"),
        json=payload,
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"].lower()
    assert "rival" in detail and "other side" in detail


def test_x_enterprise_receiver_logging_policy_summary_only_redacts(
    client: TestClient,
) -> None:
    _seed_peering(
        offer_id="off_recv",
        from_ent="globex",
        to_ent="acme",
        consult_logging_policy="summary_only_log",
    )
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=_x_enterprise_payload(content="confidential"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["logging_policy_applied"] == "summary_only_log"

    # Verify the receiver-side message row is redacted.
    msg_rows = _get_store().sync.list_consult_messages("th_recv_1")
    assert len(msg_rows) == 1
    assert msg_rows[0]["content"] == "<redacted: summary_only_log>"


def test_x_enterprise_receiver_logging_policy_no_log_skips_message(
    client: TestClient,
) -> None:
    _seed_peering(
        offer_id="off_recv",
        from_ent="globex",
        to_ent="acme",
        consult_logging_policy="no_log_consults",
    )
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=_x_enterprise_payload(content="ephemeral"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["logging_policy_applied"] == "no_log_consults"

    # Thread row exists (audit point) but no message row.
    thread = _get_store().sync.get_consult("th_recv_1")
    assert thread is not None
    msg_rows = _get_store().sync.list_consult_messages("th_recv_1")
    assert msg_rows == []


def test_x_enterprise_receiver_idempotent_on_redelivery(client: TestClient) -> None:
    """Same offer_id + thread_id + message_id replayed → 201, no dup row."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    payload = _x_enterprise_payload()
    h = _x_enterprise_headers(bearer=bearer)

    r1 = client.post("/api/v1/consults/x-enterprise-forward-request", headers=h, json=payload)
    r2 = client.post("/api/v1/consults/x-enterprise-forward-request", headers=h, json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201

    msg_rows = _get_store().sync.list_consult_messages("th_recv_1")
    assert len(msg_rows) == 1


def test_x_enterprise_receiver_does_not_use_enterprise_peer_key(
    client: TestClient,
) -> None:
    """Even with a valid EnterprisePeerKey, no peering means 403.

    Defensive — the cross-Enterprise endpoint must NOT fall back to
    the intra-Enterprise auth shape.
    """
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers={
            "authorization": f"Bearer {bearer}",  # arbitrary bearer, no peering registered
            "x-8l-forwarder-l2-id": "globex/eng",
            "x-8l-peering-offer-id": "off_does_not_exist",
        },
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 403, r.text


def test_x_enterprise_receiver_inbox_visibility(client: TestClient) -> None:
    """After the receiver mirrors, alice's inbox shows the cross-Enterprise thread."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 201

    token = _login(client)
    inbox = client.get("/api/v1/consults/inbox", headers={"authorization": f"Bearer {token}"})
    assert inbox.status_code == 200
    threads = inbox.json()["threads"]
    assert any(t["thread_id"] == "th_recv_1" for t in threads)


# ---------------------------------------------------------------------------
# Issue #98 — to_persona must exist on the receiving L2
# ---------------------------------------------------------------------------
#
# Without this guard, a typo'd or stale persona name produces a thread +
# message pair that nobody can ever read (the addressed user doesn't exist
# on this L2, so the inbox surface never returns the row) — and the sender
# gets a false-positive "delivered" 201 response. See issue #98.


def test_x_enterprise_receiver_unknown_persona_returns_404(client: TestClient) -> None:
    """to_persona that doesn't exist as a user on this L2 → 404, no rows written."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)
    payload = _x_enterprise_payload()
    payload["to_persona"] = "nonexistent_user"

    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=payload,
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "to_persona not found"

    # Defensive: no thread row, no message row landed.
    assert _get_store().sync.get_consult(payload["thread_id"]) is None
    assert _get_store().sync.list_consult_messages(payload["thread_id"]) == []


def test_x_enterprise_receiver_persona_in_wrong_enterprise_returns_404(
    client: TestClient,
) -> None:
    """A username that exists but belongs to a different enterprise/group is not addressable."""
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)

    # Seed a user with the right username but wrong tenancy. The receiver
    # L2 is acme/engineering — this user is on rival/somewhere.
    store = _get_store()
    pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
    store.sync.create_user("foreigner", pw)
    with store._engine.begin() as _c:
        _c.exec_driver_sql(
            "UPDATE users SET enterprise_id = ?, group_id = ? WHERE username = ?",
            ("rival", "somewhere", "foreigner"),
        )

    payload = _x_enterprise_payload()
    payload["to_persona"] = "foreigner"

    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=payload,
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "to_persona not found"
    assert _get_store().sync.get_consult(payload["thread_id"]) is None


def test_x_enterprise_receiver_valid_persona_succeeds(client: TestClient) -> None:
    """The happy-path mirror — explicit baseline that #98's validation
    didn't regress real users.

    The fixture seeds ALICE on acme/engineering (the receiving L2's
    tenancy). The default _x_enterprise_payload() addresses ALICE, so
    this should still 201 + create the thread, even with the new check.
    """
    _seed_peering(offer_id="off_recv", from_ent="globex", to_ent="acme")
    bearer = forward_sign.derive_peering_bearer(_FAKE_OFFER_SIG, _FAKE_ACCEPT_SIG)

    r = client.post(
        "/api/v1/consults/x-enterprise-forward-request",
        headers=_x_enterprise_headers(bearer=bearer),
        json=_x_enterprise_payload(),
    )
    assert r.status_code == 201, r.text
    thread = _get_store().sync.get_consult("th_recv_1")
    assert thread is not None
    assert thread["to_persona"] == ALICE
