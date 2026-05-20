"""Tests for the L2-side HTTP-client ``EmailSender`` (Decision 34).

The L2's ``EmailSender`` POSTs to the central transactional service
instead of calling SES directly. These tests verify:

* SES boto3 is not imported on the happy HTTP path.
* HTTP body is HMAC-signed correctly (server-side verification round-trip).
* 202 → ``status="sent"`` + delivery_handle / ses_message_id propagated.
* 409 → ``status="suppressed"`` + reason propagated.
* Missing ``TX_SEND_KEY`` → falls back to legacy SES when
  ``CQ_EMAIL_LEGACY_SES=1``, errors otherwise.
* The legacy fallback path emits a deprecation log line.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from cq_server.email_sender import EmailSender, EmailSendOutcome
from cq_server.transactional.auth import (
    StaticKeyResolver,
    verify_hmac_signature,
)

L2_ID = "acme/engineering"
KEY = "test-hmac-key"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CQ_ENTERPRISE", "acme")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    monkeypatch.setenv("TX_SEND_KEY", KEY)
    monkeypatch.setenv("TX_SEND_BASE_URL", "http://central.test")
    monkeypatch.delenv("CQ_EMAIL_LEGACY_SES", raising=False)


class _StubHttpClient:
    """Minimal httpx-compatible stub. Returns scripted responses."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> httpx.Response:
        self.calls.append({"url": url, "content": content, "headers": headers})
        return self._response


def _make_response(status_code: int, json_body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(json_body).encode(),
        headers={"Content-Type": "application/json"},
    )


def _make_sender(stub: _StubHttpClient) -> EmailSender:
    sender = EmailSender()
    sender._client = stub  # noqa: SLF001 — test injection
    return sender


def test_happy_path_202(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubHttpClient(
        _make_response(
            202,
            {
                "delivery_handle": "tx_abc",
                "ses_message_id": "ses-1",
                "suppression_check": "passed",
            },
        )
    )
    sender = _make_sender(stub)

    result = sender.send_invite(
        to="alice@example.com",
        jwt="JWT.STR.HERE",
        inviter_name="Dirk",
        enterprise_name="Acme",
        expiry=datetime(2026, 5, 27, 0, 0, 0),
    )
    assert result["status"] == "sent"
    assert result["delivery_handle"] == "tx_abc"
    assert result["MessageId"] == "ses-1"

    # One HTTP call, right URL, right headers.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"] == "http://central.test/api/v1/transactional/send"
    assert call["headers"]["X-8L-L2-Id"] == L2_ID
    assert call["headers"]["X-8L-Signature"].startswith("sha256=")
    # Body is well-formed JSON with the right persona + category.
    body = json.loads(call["content"])
    assert body["from_persona"] == "invites"
    assert body["category"] == "invite_magic_link"
    assert body["to"] == "alice@example.com"
    assert "audit_ref" in body  # auto-derived from jwt


def test_signature_verifies_against_server_resolver() -> None:
    """The client's signature must verify with the server's resolver."""
    stub = _StubHttpClient(_make_response(202, {"delivery_handle": "h", "ses_message_id": "s", "suppression_check": "passed"}))
    sender = _make_sender(stub)
    sender.send_invite(
        to="alice@example.com",
        jwt="JWT.STR.HERE",
        inviter_name="Dirk",
        enterprise_name="Acme",
        expiry=datetime(2026, 5, 27, 0, 0, 0),
    )
    call = stub.calls[0]
    resolver = StaticKeyResolver(keys={L2_ID: KEY})
    assert verify_hmac_signature(
        body=call["content"],
        signature_header=call["headers"]["X-8L-Signature"],
        l2_id=L2_ID,
        resolver=resolver,
    )


def test_suppression_409_propagates() -> None:
    stub = _StubHttpClient(
        _make_response(
            409,
            {
                "detail": {
                    "delivery_handle": None,
                    "suppression_check": "blocked",
                    "reason": "hard_bounce_2026-05-15",
                }
            },
        )
    )
    sender = _make_sender(stub)
    result = sender.send_invite(
        to="bounced@example.com",
        jwt="JWT.STR.HERE",
        inviter_name="Dirk",
        enterprise_name="Acme",
        expiry=datetime(2026, 5, 27, 0, 0, 0),
    )
    assert result["status"] == "suppressed"
    assert result["reason"] == "hard_bounce_2026-05-15"
    assert result["MessageId"] is None
    assert result["delivery_handle"] is None


def test_send_invite_outcome_returns_envelope() -> None:
    stub = _StubHttpClient(
        _make_response(
            202,
            {"delivery_handle": "tx_x", "ses_message_id": "ses-x", "suppression_check": "passed"},
        )
    )
    sender = _make_sender(stub)
    outcome = sender.send_invite_outcome(
        to="alice@example.com",
        jwt="JWT",
        inviter_name="Dirk",
        enterprise_name="Acme",
        expiry=datetime(2026, 5, 27),
    )
    assert isinstance(outcome, EmailSendOutcome)
    assert outcome.ok
    assert outcome.delivery_handle == "tx_x"


def test_missing_key_no_legacy_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TX_SEND_KEY", raising=False)
    sender = EmailSender()
    result = sender.send_invite(
        to="x@example.com",
        jwt="J",
        inviter_name="D",
        enterprise_name="A",
        expiry=datetime(2026, 5, 27),
    )
    assert result["status"] == "error"
    assert result["reason"] == "tx_send_key_missing"


def test_missing_key_with_legacy_flag_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TX_SEND_KEY", raising=False)
    monkeypatch.setenv("CQ_EMAIL_LEGACY_SES", "1")
    sender = EmailSender()

    # Inject a fake legacy SES client to avoid touching boto3.
    class _StubSes:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        def send_email(self, **kwargs: Any) -> dict[str, Any]:
            self.sent.append(kwargs)
            return {"MessageId": "legacy-1"}

    stub_ses = _StubSes()
    sender._legacy_ses_client = stub_ses  # noqa: SLF001

    # Attach a stub handler directly to the email_sender logger so we
    # capture the deprecation line regardless of pytest's caplog state
    # (some test modules earlier in the run reconfigure root handlers).
    import logging

    captured: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("cq_server.email_sender")
    handler = _CaptureHandler(level=logging.WARNING)
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        result = sender.send_invite(
            to="x@example.com",
            jwt="J",
            inviter_name="D",
            enterprise_name="A",
            expiry=datetime(2026, 5, 27),
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    assert result["status"] == "sent"
    assert result["MessageId"] == "legacy-1"
    assert len(stub_ses.sent) == 1
    # Deprecation log line fired.
    joined = "\n".join(captured)
    assert "TX_SEND_KEY missing" in joined
    assert "legacy SES" in joined


def test_no_ses_boto3_on_http_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP-happy path must not import boto3 SES.

    The promise of Decision 34 is that customer L2s have zero SES
    setup. If the L2 codebase ever auto-imports boto3 on the send
    path, that promise leaks back to the customer (their task role
    needs SES IAM, etc.). Guard with a sentinel.
    """
    stub = _StubHttpClient(_make_response(202, {"delivery_handle": "h", "ses_message_id": "s", "suppression_check": "passed"}))
    sender = _make_sender(stub)
    sender.send_invite(
        to="alice@example.com",
        jwt="J",
        inviter_name="D",
        enterprise_name="A",
        expiry=datetime(2026, 5, 27),
    )
    # ``_legacy_ses_client`` MUST remain None — proves the legacy
    # branch was not entered.
    assert sender._legacy_ses_client is None  # noqa: SLF001
