"""Tests for ``transactional.suppression`` read/write helpers.

Exercises the SNS-fed writer path without touching SNS — direct
``record_suppression`` calls verify the table semantics.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cq_server.app import _get_store, app
from cq_server.transactional.suppression import (
    check_suppression,
    record_suppression,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "suppression.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    with TestClient(app) as c:
        yield c


def test_check_suppression_returns_none_for_unknown(client: TestClient) -> None:
    store = _get_store()
    assert check_suppression(store, "nobody@example.com") is None


def test_record_then_check(client: TestClient) -> None:
    store = _get_store()
    inserted = record_suppression(
        store,
        address="bounced@example.com",
        reason="hard_bounce_2026-05-19",
        source_event_id="sns-evt-1",
    )
    assert inserted is True
    entry = check_suppression(store, "bounced@example.com")
    assert entry is not None
    assert entry.address == "bounced@example.com"
    assert entry.reason == "hard_bounce_2026-05-19"
    assert entry.source_event_id == "sns-evt-1"
    # Lookup is case-insensitive.
    assert check_suppression(store, "BOUNCED@example.com") is not None


def test_record_is_idempotent(client: TestClient) -> None:
    store = _get_store()
    first = record_suppression(store, address="dup@example.com", reason="hard_bounce_2026-05-19")
    second = record_suppression(store, address="dup@example.com", reason="complaint_2026-05-20")
    assert first is True
    assert second is False  # PK conflict — no-op
    # First reason wins.
    entry = check_suppression(store, "dup@example.com")
    assert entry is not None
    assert entry.reason == "hard_bounce_2026-05-19"
