"""Server-side batch-reflect endpoints (#67).

Implements the wire surface frozen in
``crosstalk-enterprise/docs/specs/batch-reflect-contract.md`` v1
(2026-04-30):

  - ``POST /reflect/submit`` — enqueue a reflection job
  - ``GET  /reflect/status`` — query a single submission's state
  - ``GET  /reflect/last`` — most-recent submission for a session

The router is mounted at ``/api/v1/reflect`` from ``app.py`` (and at
``/reflect`` for SDK-compat parity with the rest of the app router).

This PR ships **only** the contract surface. The Anthropic Batch
dispatch worker that consumes the ``reflect_submissions`` queue and
drives the state machine through ``batching → polling → complete``
lives in a separate container per the contract's implementation notes
and lands in a follow-up PR.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .deps import get_store, require_api_key
from .store._sqlite import SqliteStore

# Contract caps. ``CONTEXT_MAX_BYTES`` is the 1 MB hard cap from the
# contract §"Request" table; ``RATE_LIMIT_HOURS`` defaults to 4 and is
# operator-tunable via env (per-Enterprise overrides are an open
# question in the spec, deferred). ``DEDUP_WINDOW_MINUTES`` is fixed
# at 30 by the contract.
CONTEXT_MAX_CHARS = 1_000_000
DEDUP_WINDOW_MINUTES = 30
RATE_LIMIT_HOURS_ENV = "REFLECT_RATE_LIMIT_PER_HOURS"
DEFAULT_RATE_LIMIT_HOURS = 4
EXPECTED_COMPLETE_MINUTES = 30

SESSION_ID_REGEX = r"^[A-Za-z0-9_./-]+$"

# Locked enums from the contract §"State machine" + §"Error codes".
_VALID_STATES = {"queued", "batching", "polling", "complete", "failed"}


router = APIRouter()


# --- Pydantic models -------------------------------------------------------


class ReflectSubmitRequest(BaseModel):
    """Request body for ``POST /reflect/submit``.

    ``session_id`` is a free-form opaque label (max 128 chars, regex
    locked); the server treats it as the rate-limit key together with
    the authenticated session-key. ``context`` is the UTF-8 blob that
    the worker will hand to Anthropic Batch.
    """

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_REGEX)
    context: str = Field(min_length=1)
    since_ts: str | None = None
    mode: str = Field(default="nightly")
    max_candidates: int = Field(default=10, ge=1)


class ReflectSubmitResponse(BaseModel):
    """Wire shape for the 202 path of ``POST /reflect/submit``.

    ``deduped_to`` is non-null when the submission collided with an
    earlier one within the 30-minute window — in that case the
    ``submission_id`` is the *original* (echoed back unchanged) rather
    than a freshly minted ULID.
    """

    submission_id: str
    queued_at: str
    expected_complete_by: str
    deduped_to: str | None = None


class ReflectStatusResponse(BaseModel):
    """Wire shape for ``GET /reflect/status`` (and ``GET /reflect/last``).

    Counts are non-null (default 0); token counts and ``model`` are
    nullable per the contract until the worker has dispatched/finished
    the batch.
    """

    submission_id: str
    session_id: str
    state: str
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    candidates_proposed: int = 0
    candidates_confirmed: int = 0
    candidates_excluded: int = 0
    candidates_deduped: int = 0
    error: str | None = None


class ReflectLastEmptyResponse(BaseModel):
    """Empty shape returned by ``GET /reflect/last`` when no submission exists.

    Branch-free for client badge code: response always parses with
    ``submission_id`` either populated (full row) or null (this shape).
    """

    submission_id: None = None
    session_id: str
    state: None = None


# --- Helpers ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """Emit a UTC ISO-8601 string with explicit ``Z`` suffix.

    The contract pins ``Z`` suffix (not ``+00:00``) for every emitted
    timestamp; ``datetime.isoformat`` on an aware UTC datetime emits
    ``+00:00`` so we fix it up.
    """
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _generate_submission_id() -> str:
    """Produce a ``sub_<26-char-ULID>`` id.

    Lexicographically sortable: 10-char Crockford-base32 timestamp
    (millis since epoch) + 16 chars of cryptographic randomness. Same
    shape `python-ulid` produces; inlined to avoid a dependency for one
    helper.
    """
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret
    millis = int(time.time() * 1000)
    ts_chars: list[str] = []
    for _ in range(10):
        ts_chars.append(alphabet[millis & 0x1F])
        millis >>= 5
    ts_part = "".join(reversed(ts_chars))
    rand_part = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"sub_{ts_part}{rand_part}"


def _context_hash(context: str) -> str:
    """sha256(context.utf8) truncated to first 16 hex chars (contract §Identifiers)."""
    return hashlib.sha256(context.encode("utf-8")).hexdigest()[:16]


def _rate_limit_hours() -> int:
    raw = os.environ.get(RATE_LIMIT_HOURS_ENV, "")
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_RATE_LIMIT_HOURS
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_HOURS


def _row_to_status_response(row: dict) -> ReflectStatusResponse:
    return ReflectStatusResponse(
        submission_id=row["id"],
        session_id=row["session_id"],
        state=row["state"],
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        candidates_proposed=row["candidates_proposed"],
        candidates_confirmed=row["candidates_confirmed"],
        candidates_excluded=row["candidates_excluded"],
        candidates_deduped=row["candidates_deduped"],
        error=row["error"],
    )


def _ensure_unrecognised_state_safe(state: str) -> str:
    """Defensive guard against stored enum drift.

    The DB column is plain TEXT (SQLite has no enum type), so a future
    migration / accidental write could land an unexpected value. Map
    the unknown value to ``failed`` on read rather than handing the
    client an out-of-contract state. Logged-once-per-process would be
    nice; deferred — bug is sufficiently rare that we eat the silence.
    """
    return state if state in _VALID_STATES else "failed"


# --- Endpoints -------------------------------------------------------------


@router.post("/submit", status_code=202, response_model=None)
async def submit_reflection(
    body: ReflectSubmitRequest,
    username: str = Depends(require_api_key),
    store: SqliteStore = Depends(get_store),
) -> ReflectSubmitResponse | JSONResponse:
    """Enqueue a reflection job. See contract §"POST /api/v1/reflect/submit"."""
    # Reject ``mode == "hourly"`` per contract — reserved syntax in v1.
    if body.mode != "nightly":
        raise HTTPException(
            status_code=422,
            detail=f"unsupported mode {body.mode!r}; only 'nightly' is allowed in v1",
        )

    # 1 MB cap. FastAPI's max-bytes guard could be wired at ASGI level
    # but keeping the check here keeps the error message contract-shaped.
    if len(body.context) > CONTEXT_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"context exceeds {CONTEXT_MAX_CHARS} chars",
        )

    # Clamp ``max_candidates`` to the [1, 25] band per contract; we
    # don't reject overshoot.
    max_candidates = min(body.max_candidates, 25)

    user = await store.get_user(username)
    if user is None:
        # Auth dep returned a username that no longer resolves to a
        # user row — race with revoke + delete. 401 is the honest code.
        raise HTTPException(status_code=401, detail="User not found")

    now = _now()
    ctx_hash = _context_hash(body.context)
    dedup_since = _iso(now - timedelta(minutes=DEDUP_WINDOW_MINUTES))

    # Dedup short-circuit: matching (session_id, context_hash) within
    # the last 30 min returns the original submission_id rather than
    # enqueueing a fresh job. Run BEFORE the rate-limit check — a
    # dedup hit isn't a new submission and shouldn't burn the quota.
    existing = await store.find_recent_reflect_dedup(
        session_id=body.session_id,
        context_hash=ctx_hash,
        window_start_iso=dedup_since,
    )
    if existing is not None:
        return ReflectSubmitResponse(
            submission_id=existing["id"],
            queued_at=existing["submitted_at"],
            expected_complete_by=_iso(
                datetime.fromisoformat(existing["submitted_at"].replace("Z", "+00:00"))
                + timedelta(minutes=EXPECTED_COMPLETE_MINUTES)
            ),
            deduped_to=existing["id"],
        )

    # Per-session rate limit (contract §"Rate limiting"). Default 1/4h.
    rate_hours = _rate_limit_hours()
    rate_since_dt = now - timedelta(hours=rate_hours)
    rate_since_iso = _iso(rate_since_dt)
    recent_count = await store.count_recent_reflect_submissions(
        session_id=body.session_id,
        window_start_iso=rate_since_iso,
    )
    if recent_count >= 1:
        # Compute Retry-After: seconds until the OLDEST in-window
        # submission ages out. Contract spec literal example uses the
        # full window length (14400 for 4h) but the field is documented
        # as "seconds until the operation could be retried" so a tighter
        # estimate is correct and friendlier to clients.
        oldest_iso = await store.get_oldest_reflect_in_window(
            session_id=body.session_id,
            window_start_iso=rate_since_iso,
        )
        retry_after_seconds = rate_hours * 3600
        if oldest_iso is not None:
            try:
                oldest_dt = datetime.fromisoformat(oldest_iso.replace("Z", "+00:00"))
                ages_out_at = oldest_dt + timedelta(hours=rate_hours)
                retry_after_seconds = max(1, int((ages_out_at - now).total_seconds()))
            except ValueError:
                pass
        # The contract pins a flat body shape (not FastAPI's standard
        # ``{"detail": ...}`` envelope), so we return a JSONResponse
        # rather than raising HTTPException.
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate_limit_exceeded",
                "retry_after_seconds": retry_after_seconds,
                "limit": f"1 submission per {rate_hours}h per session-key",
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )

    submission_id = _generate_submission_id()
    submitted_at = _iso(now)
    await store.create_reflect_submission(
        submission_id=submission_id,
        session_id=body.session_id,
        user_id=int(user["id"]),
        enterprise_id=user["enterprise_id"],
        group_id=user.get("group_id"),
        context_hash=ctx_hash,
        mode=body.mode,
        max_candidates=max_candidates,
        since_ts=body.since_ts,
        submitted_at=submitted_at,
    )
    return ReflectSubmitResponse(
        submission_id=submission_id,
        queued_at=submitted_at,
        expected_complete_by=_iso(now + timedelta(minutes=EXPECTED_COMPLETE_MINUTES)),
        deduped_to=None,
    )


@router.get("/status")
async def get_status(
    submission_id: Annotated[str, Query(min_length=1)],
    username: str = Depends(require_api_key),
    store: SqliteStore = Depends(get_store),
) -> ReflectStatusResponse:
    """Return state for one submission. 404 when unknown.

    Cross-Enterprise isolation: a caller can only read submissions
    inside their own Enterprise. We treat a wrong-Enterprise lookup as
    a 404 (not 403) so probes can't fingerprint submission existence
    across tenants.
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    row = await store.get_reflect_submission(submission_id)
    if row is None or row["enterprise_id"] != user["enterprise_id"]:
        raise HTTPException(status_code=404, detail="submission not found")
    row["state"] = _ensure_unrecognised_state_safe(row["state"])
    return _row_to_status_response(row)


@router.get(
    "/last",
    responses={
        200: {
            "model": ReflectStatusResponse,
            "description": "most-recent submission for the session, or null shape when none exists",
        }
    },
)
async def get_last(
    session_id: Annotated[str, Query(min_length=1, max_length=128, pattern=SESSION_ID_REGEX)],
    username: str = Depends(require_api_key),
    store: SqliteStore = Depends(get_store),
) -> ReflectStatusResponse | ReflectLastEmptyResponse:
    """Most-recent submission for a session-id within the caller's Enterprise.

    Returns 200-with-null shape when the session has never submitted —
    the muxtop badge expects branch-free decoding (contract §"GET
    /api/v1/reflect/last").
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    row = await store.get_last_reflect_for_session(
        session_id=session_id,
        enterprise_id=user["enterprise_id"],
    )
    if row is None:
        return ReflectLastEmptyResponse(session_id=session_id)
    row["state"] = _ensure_unrecognised_state_safe(row["state"])
    return _row_to_status_response(row)
