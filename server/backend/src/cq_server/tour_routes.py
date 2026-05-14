"""Founder-tour persistence — per-user `tour_state` read/write.

Tiny endpoint pair behind ``/api/v1/users/me/tour-state``:

* ``GET``  → return the JSON blob (or an empty default if NULL).
* ``PUT``  → upsert the JSON blob. No partial updates — caller sends
             the full shape every time. Keeps the contract honest.

Authentication is the standard cookie/bearer dep (``get_current_user``).
Per-user — there is no admin-fetch-other-user surface here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from .auth import get_current_user
from .deps import get_store
from .store._sqlite import SqliteStore

log = logging.getLogger("tour_routes")

router = APIRouter(prefix="/users/me", tags=["tour"])


class TourState(BaseModel):
    """Shape of the per-user tour-completion state.

    All fields are optional — the frontend treats absence of
    ``completed_at`` as "tour has not been finished; auto-fire OK"
    unless ``dismissed_at`` is set (then replay only via the
    `?` launcher, never auto-fire).
    """

    completed_at: str | None = None
    dismissed_at: str | None = None
    current_step: int = 0


def _parse(raw: str | None) -> TourState:
    """Decode the TEXT column into a ``TourState``. Empty → defaults."""
    if not raw:
        return TourState()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Corrupt row — surface as "fresh" rather than 500ing the UI.
        log.warning("tour_state JSON decode failed; returning defaults")
        return TourState()
    if not isinstance(data, dict):
        return TourState()
    return TourState(
        completed_at=data.get("completed_at"),
        dismissed_at=data.get("dismissed_at"),
        current_step=int(data.get("current_step", 0)),
    )


def _serialize(state: TourState) -> str:
    """Encode for the TEXT column. Compact JSON, stable key order."""
    return json.dumps(
        {
            "completed_at": state.completed_at,
            "dismissed_at": state.dismissed_at,
            "current_step": state.current_step,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_sync(store: SqliteStore, username: str) -> str | None:
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text("SELECT tour_state FROM users WHERE username = :u"),
            {"u": username},
        ).fetchone()
    return row[0] if row is not None else None


def _write_sync(store: SqliteStore, username: str, blob: str) -> int:
    with store._engine.begin() as conn:  # noqa: SLF001
        result = conn.execute(
            text("UPDATE users SET tour_state = :s WHERE username = :u"),
            {"s": blob, "u": username},
        )
    return int(result.rowcount or 0)


@router.get("/tour-state", response_model=TourState)
async def get_tour_state(
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> TourState:
    """Return the caller's tour-state, or empty defaults if never set."""
    raw = await store._run_sync(_read_sync, store, username)  # noqa: SLF001
    return _parse(raw)


@router.put("/tour-state", response_model=TourState)
async def put_tour_state(
    payload: TourState,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> TourState:
    """Replace the caller's tour-state with the supplied blob.

    Server stamps ``completed_at`` / ``dismissed_at`` to now if the
    caller sent the literal string ``"now"`` — convenience so the
    frontend doesn't need to grab a clock.
    """
    now = datetime.now(UTC).isoformat()
    if payload.completed_at == "now":
        payload.completed_at = now
    if payload.dismissed_at == "now":
        payload.dismissed_at = now

    blob = _serialize(payload)
    rowcount = await store._run_sync(_write_sync, store, username, blob)  # noqa: SLF001
    if rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return payload
