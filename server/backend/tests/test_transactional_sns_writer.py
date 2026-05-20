"""Tests for the SNS → suppression writer (``transactional.sns_writer``).

Real SES bounce/complaint JSON envelopes (sampled from AWS docs) are
the fixtures; the writer should parse them and land rows in the
suppression table.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.transactional.sns_writer import (
    handle_lambda_event,
    handle_sns_message,
)
from cq_server.transactional.suppression import check_suppression


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "sns_writer.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


# Sampled from
# https://docs.aws.amazon.com/ses/latest/dg/notification-contents.html
PERMANENT_BOUNCE_ENVELOPE = json.dumps(
    {
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [
                {"emailAddress": "permanent-bounce@example.com"}
            ],
            "timestamp": "2026-05-19T14:32:00.000Z",
        },
    }
)

TRANSIENT_BOUNCE_ENVELOPE = json.dumps(
    {
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": "Transient",
            "bouncedRecipients": [{"emailAddress": "soft-bounce@example.com"}],
            "timestamp": "2026-05-19T14:32:00.000Z",
        },
    }
)

COMPLAINT_ENVELOPE = json.dumps(
    {
        "notificationType": "Complaint",
        "complaint": {
            "complainedRecipients": [{"emailAddress": "annoyed@example.com"}],
            "timestamp": "2026-05-19T14:32:00.000Z",
        },
    }
)


def test_permanent_bounce_records_suppression(client: TestClient) -> None:
    store = _get_store()
    n = handle_sns_message(PERMANENT_BOUNCE_ENVELOPE, store, event_id="sns-evt-1")
    assert n == 1
    entry = check_suppression(store, "permanent-bounce@example.com")
    assert entry is not None
    assert entry.reason == "hard_bounce_2026-05-19"
    assert entry.source_event_id == "sns-evt-1"


def test_transient_bounce_is_skipped(client: TestClient) -> None:
    store = _get_store()
    n = handle_sns_message(TRANSIENT_BOUNCE_ENVELOPE, store)
    assert n == 0
    assert check_suppression(store, "soft-bounce@example.com") is None


def test_complaint_records_suppression(client: TestClient) -> None:
    store = _get_store()
    n = handle_sns_message(COMPLAINT_ENVELOPE, store)
    assert n == 1
    entry = check_suppression(store, "annoyed@example.com")
    assert entry is not None
    assert entry.reason == "complaint_2026-05-19"


def test_non_json_payload_returns_zero(client: TestClient) -> None:
    store = _get_store()
    assert handle_sns_message("not json", store) == 0


def test_unknown_notification_type_returns_zero(client: TestClient) -> None:
    store = _get_store()
    envelope = json.dumps({"notificationType": "DeliveryDelay"})
    assert handle_sns_message(envelope, store) == 0


def test_lambda_event_wraps_multiple_records(client: TestClient) -> None:
    store = _get_store()
    event = {
        "Records": [
            {"Sns": {"Message": PERMANENT_BOUNCE_ENVELOPE, "MessageId": "m-1"}},
            {"Sns": {"Message": COMPLAINT_ENVELOPE, "MessageId": "m-2"}},
            {"Sns": {"Message": TRANSIENT_BOUNCE_ENVELOPE, "MessageId": "m-3"}},
        ]
    }
    result = handle_lambda_event(event, store)
    assert result == {"recorded": 2, "skipped": 1}
    # Both perma + complaint addresses now suppressed.
    assert check_suppression(store, "permanent-bounce@example.com") is not None
    assert check_suppression(store, "annoyed@example.com") is not None
