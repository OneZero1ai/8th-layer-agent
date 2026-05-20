"""``POST /api/v1/transactional/send`` — central transactional-mail service.

Implements Decision 34 (agent#348). The route lives on the control-plane
cq-server deployment (the one fronted by ``directory.8th-layer.ai``);
every L2 routes its outbound transactional mail here instead of calling
SES directly.

# Request flow

1. Read raw body bytes (needed for HMAC verification — Pydantic parsing
   happens *after* the digest check).
2. Resolve the per-L2 HMAC key from ``X-8L-L2-Id`` header.
3. Verify ``X-8L-Signature`` over the raw body. Mismatch → 401.
4. (Optional) Check ``Idempotency-Key`` header — return cached response
   if seen within 60s.
5. Pydantic-parse the body into ``TransactionalSendRequest``.
6. Tenancy check: ``to`` must be a known user / pending-invitee of the
   caller's tenant. Cross-tenant → 403.
7. Suppression check: ``to`` in ``transactional_suppression`` → 409.
8. SES dispatch through :class:`SesDispatcher`. Return 202 with handle.

# Why dependency injection on the dispatcher / resolver / cache

The route's three external collaborators (SES, HMAC key resolver,
idempotency cache) all need to be swappable in tests. FastAPI deps
give us that for free, and the production wiring is one
``app.dependency_overrides`` line. The MockSesDispatcher case
exercises every code path without touching AWS.

# audit_ref

The L2 may pass an ``audit_ref`` in the body (free-form string —
typically a provisioning job id, an invite jti, etc.). It's echoed
into the log line and used to deduplicate when ``Idempotency-Key`` is
omitted — see :func:`_derive_idempotency_key`. Surfaces in
operator-side debugging without forcing the L2 to think about HTTP
header conventions.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..deps import get_store
from ..store._sqlite import SqliteStore
from .auth import HmacKeyResolver, StaticKeyResolver, verify_hmac_signature
from .dispatcher import PERSONA_SENDERS, MockSesDispatcher, SesDispatcher
from .idempotency import IdempotencyStore
from .suppression import check_suppression
from .tenancy import enforce_tenancy

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class TransactionalSendRequest(BaseModel):
    """``POST /api/v1/transactional/send`` body."""

    from_persona: Literal["invites", "auth", "notifications"]
    # ``to`` is a plain str with a minimal shape check — full RFC 5322
    # would require email-validator. SES itself is the authoritative
    # parser; we just guard against obvious garbage at the boundary.
    to: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=998)  # RFC 5322 §2.1.1
    text: str = Field(min_length=1)
    html: str | None = None
    category: Literal[
        "invite_magic_link",
        "password_reset",
        "two_factor",
        "account_event",
        "security_alert",
    ]
    audit_ref: str | None = Field(default=None, max_length=128)

    @field_validator("to")
    @classmethod
    def _lower_to(cls, value: str) -> str:
        # Suppression keying is case-insensitive; lowercase at the
        # boundary so every downstream comparison sees one canonical
        # form. SES itself accepts mixed-case, so this is a normalise,
        # not a constraint. The minimal "@" guard catches obvious
        # garbage; SES is the authoritative parser.
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("invalid email shape")
        return value


class TransactionalSendResponse(BaseModel):
    """``202`` happy path."""

    delivery_handle: str
    ses_message_id: str | None
    suppression_check: Literal["passed"]


class TransactionalSuppressedResponse(BaseModel):
    """``409`` body — address is suppressed; no dispatch."""

    delivery_handle: None = None
    suppression_check: Literal["blocked"] = "blocked"
    reason: str


# ---------------------------------------------------------------------------
# Dependency wiring — process-wide singletons, overridden in tests.
# ---------------------------------------------------------------------------


_dispatcher: SesDispatcher | MockSesDispatcher | None = None
_resolver: HmacKeyResolver | None = None
_idempotency: IdempotencyStore | None = None


def get_dispatcher() -> SesDispatcher | MockSesDispatcher:
    """Resolve the process-wide SES dispatcher. Tests override.

    Lazy singleton so import time is cheap and tests that never call
    ``/transactional/send`` don't pay the boto3 import cost.
    """
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = SesDispatcher()
    return _dispatcher


def get_resolver() -> HmacKeyResolver:
    """Resolve the process-wide HMAC key resolver. Tests override.

    Production wiring (set in :func:`cq_server.app.lifespan` when the
    control-plane deployment boots) is :class:`SsmKeyResolver`. In its
    absence we fall back to an empty static map — that fails-closed
    (every request is 401), which is the right behaviour pre-config.
    """
    global _resolver
    if _resolver is None:
        _resolver = StaticKeyResolver(keys={})
    return _resolver


def get_idempotency_store() -> IdempotencyStore:
    """Process-wide 60s dedup cache."""
    global _idempotency
    if _idempotency is None:
        _idempotency = IdempotencyStore()
    return _idempotency


def _set_dispatcher(dispatcher: SesDispatcher | MockSesDispatcher) -> None:
    """Override the singleton — used by tests + app startup."""
    global _dispatcher
    _dispatcher = dispatcher


def _set_resolver(resolver: HmacKeyResolver) -> None:
    global _resolver
    _resolver = resolver


def _set_idempotency_store(store: IdempotencyStore) -> None:
    global _idempotency
    _idempotency = store


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/transactional", tags=["transactional"])


def _new_delivery_handle() -> str:
    """ULID-ish opaque handle. Format isn't load-bearing — opaque to L2s.

    32 random hex chars is plenty unique; the ``tx_`` prefix makes
    grep-friendly distinction from other ids in operator logs.
    """
    return f"tx_{secrets.token_hex(16)}"


def _derive_idempotency_key(
    header_key: str | None,
    l2_id: str,
    audit_ref: str | None,
) -> str | None:
    """Decide which idempotency key (if any) to dedup against.

    Order:

    * Explicit ``Idempotency-Key`` header — highest precedence.
    * Otherwise, fall back to ``{l2_id}:{audit_ref}`` if the body
      provided ``audit_ref``. This is the Decision-34 specified
      behaviour: "keyed off ``audit_ref`` if the L2 provides one".
    * Otherwise None — no dedup. The default for L2s that don't care
      about retry semantics.
    """
    if header_key:
        return f"{l2_id}:hdr:{header_key}"
    if audit_ref:
        return f"{l2_id}:ref:{audit_ref}"
    return None


@router.post("/send", status_code=202)
async def send_transactional(
    request: Request,
    response: Response,
    x_8l_l2_id: str | None = Header(default=None, alias="X-8L-L2-Id"),
    x_8l_signature: str | None = Header(default=None, alias="X-8L-Signature"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    store: SqliteStore = Depends(get_store),
    dispatcher: SesDispatcher | MockSesDispatcher = Depends(get_dispatcher),
    resolver: HmacKeyResolver = Depends(get_resolver),
    idempotency: IdempotencyStore = Depends(get_idempotency_store),
) -> dict[str, Any]:
    """Central transactional-send endpoint — see module docstring."""

    # ---- 1. Raw body for HMAC verification --------------------------------
    raw_body = await request.body()

    # ---- 2/3. Auth: L2 id + signature ------------------------------------
    if not x_8l_l2_id:
        raise HTTPException(status_code=401, detail="missing X-8L-L2-Id header")
    if "/" not in x_8l_l2_id:
        # The l2_id MUST be ``<enterprise>/<group>``. Reject other shapes
        # at the boundary so the tenancy check can rely on a clean split.
        raise HTTPException(status_code=401, detail="malformed X-8L-L2-Id (expected enterprise/group)")
    if not verify_hmac_signature(
        body=raw_body,
        signature_header=x_8l_signature,
        l2_id=x_8l_l2_id,
        resolver=resolver,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    enterprise_id, group_id = x_8l_l2_id.split("/", 1)

    # ---- 5. Parse body ----------------------------------------------------
    # (4 is below — we need the parsed audit_ref to compute the fallback key)
    try:
        payload = TransactionalSendRequest.model_validate_json(raw_body)
    except Exception as exc:
        # Pydantic's own error envelope leaks field-shape info that an
        # attacker could probe; we keep the 422 path but trim the body.
        log.info("transactional/send rejected as 422: %s", exc)
        raise HTTPException(status_code=422, detail="invalid request body") from exc

    # ---- 4. Idempotency check --------------------------------------------
    dedup_key = _derive_idempotency_key(idempotency_key, x_8l_l2_id, payload.audit_ref)
    if dedup_key is not None:
        cached = idempotency.get(dedup_key)
        if cached is not None:
            log.info(
                "transactional/send idempotent replay l2_id=%s key=%s",
                x_8l_l2_id,
                dedup_key,
            )
            # Replay the original status code as well — a 409 replay
            # must NOT come back as 202.
            response.status_code = cached.get("_status_code", 202)
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    # ---- 6. Tenancy enforcement ------------------------------------------
    if not enforce_tenancy(
        store=store,
        enterprise_id=enterprise_id,
        group_id=group_id,
        to=payload.to,
        category=payload.category,
    ):
        log.warning(
            "transactional/send tenancy_violation l2_id=%s to=%s category=%s audit_ref=%s",
            x_8l_l2_id,
            payload.to,
            payload.category,
            payload.audit_ref,
        )
        raise HTTPException(status_code=403, detail="recipient outside caller tenancy")

    # ---- 7. Suppression check --------------------------------------------
    suppression = check_suppression(store, payload.to)
    if suppression is not None:
        log.info(
            "transactional/send blocked l2_id=%s to=%s reason=%s audit_ref=%s",
            x_8l_l2_id,
            payload.to,
            suppression.reason,
            payload.audit_ref,
        )
        suppressed = {
            "delivery_handle": None,
            "suppression_check": "blocked",
            "reason": suppression.reason,
        }
        if dedup_key is not None:
            idempotency.put(dedup_key, {**suppressed, "_status_code": 409})
        # Use HTTPException so FastAPI emits the right Content-Type
        # and the body shape matches the success path's JSON envelope.
        raise HTTPException(status_code=409, detail=suppressed)

    # ---- 8. Dispatch ------------------------------------------------------
    try:
        ses_resp = dispatcher.send(
            from_persona=payload.from_persona,
            to=payload.to,
            subject=payload.subject,
            text_body=payload.text,
            html_body=payload.html,
        )
    except Exception as exc:
        # SES errors translate to 502 — the caller's request was valid,
        # we couldn't deliver. Don't surface boto3 internals.
        log.exception("transactional/send SES dispatch failed l2_id=%s", x_8l_l2_id)
        raise HTTPException(status_code=502, detail="downstream SES error") from exc

    handle = _new_delivery_handle()
    ses_message_id = ses_resp.get("MessageId") if isinstance(ses_resp, dict) else None
    log.info(
        "transactional/send ok l2_id=%s to=%s category=%s persona=%s handle=%s ses_id=%s audit_ref=%s",
        x_8l_l2_id,
        payload.to,
        payload.category,
        payload.from_persona,
        handle,
        ses_message_id,
        payload.audit_ref,
    )
    body = {
        "delivery_handle": handle,
        "ses_message_id": ses_message_id,
        "suppression_check": "passed",
    }
    if dedup_key is not None:
        idempotency.put(dedup_key, {**body, "_status_code": 202})
    return body


# Re-export the persona map so external callers (tests, ops scripts)
# can introspect the persona → sender wiring without importing
# dispatcher directly.
__all__ = [
    "PERSONA_SENDERS",
    "TransactionalSendRequest",
    "TransactionalSendResponse",
    "TransactionalSuppressedResponse",
    "_set_dispatcher",
    "_set_idempotency_store",
    "_set_resolver",
    "get_dispatcher",
    "get_idempotency_store",
    "get_resolver",
    "router",
]
