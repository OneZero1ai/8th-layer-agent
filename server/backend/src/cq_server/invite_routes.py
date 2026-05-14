"""Invite HTTP routes — FO-1b magic-link surface.

Two route groups under one router:

* ``/admin/invites/*`` — admin-gated mint / list / revoke.
* ``/invites/{jwt}/...`` — public; signature + lifecycle gated.

Auth shape:

* The admin endpoints chain ``require_admin`` (existing dep). The body
  carries the mint params; the response intentionally omits the JWT so
  the only path the bearer reaches is the email channel — see Decision
  doc on FO-1 spec on issue #191.
* The public endpoints take the JWT in the URL path. ``/invites/{jwt}``
  returns claim-page metadata; ``/invites/{jwt}/claim`` consumes the
  invite and provisions a session bearer.

# Why no JWT in the response

A canonical anti-pattern is "mint endpoint returns the bearer; admin
saves it; admin sends it themselves." Two failure modes follow: (a) the
bearer leaks through admin-side logs / screenshots; (b) the admin side
becomes a second send-channel that has to be hardened. Returning only
metadata pushes both off the platform — the email channel is the only
delivery path, so SES + DKIM + bounces are the failure surface, not the
admin UI.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from .auth import _get_jwt_secret, require_admin
from .deps import get_store
from .email_sender import EmailSender, MockEmailSender
from .invites import (
    Invite,
    InviteRole,
    claim_invite,
    ensure_user,
    list_invites,
    mint_invite,
    revoke_invite,
    validate_invite_jwt,
)
from .store._sqlite import SqliteStore
from .web_session import mint_session_cookie

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email-sender dependency — mockable per-app.
# ---------------------------------------------------------------------------


_email_sender: EmailSender | MockEmailSender | None = None


def get_email_sender() -> EmailSender | MockEmailSender:
    """FastAPI dependency → resolves the process-wide email sender.

    Tests override this dep with ``app.dependency_overrides`` to swap
    in a ``MockEmailSender`` instance and assert on captured sends.
    """
    global _email_sender
    if _email_sender is None:
        _email_sender = EmailSender()
    return _email_sender


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class CreateInviteRequest(BaseModel):
    """Admin → mint body. Issue #191 §FO-1b spec."""

    email: str
    role: Literal["enterprise_admin", "l2_admin", "user"]
    target_l2_id: str | None = Field(
        default=None,
        description="Required for non-enterprise_admin roles; ignored for enterprise_admin.",
    )
    enterprise_name: str = Field(
        default="8th-Layer.ai",
        description="Display name rendered in the invite email subject + body.",
    )

    @field_validator("email")
    @classmethod
    def _basic_email_shape(cls, value: str) -> str:
        # Minimal sanity check — full RFC 5322 is overkill. We rely on
        # SES to reject mis-routed mail; the goal here is to catch
        # obvious typos at the API edge before we burn a JWT mint.
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("invalid email address")
        return value.strip()


class InvitePublic(BaseModel):
    """Public/admin view of an invite row — never includes the JWT bearer."""

    id: int
    email: str
    role: str
    target_l2_id: str | None
    issued_by: int
    issued_at: str
    expires_at: str
    claimed_at: str | None
    claimed_by: int | None
    revoked_at: str | None
    status: str


class InvitesPublic(BaseModel):
    """Collection wrapper, mirroring ``ApiKeysPublic``."""

    data: list[InvitePublic]
    count: int


class ClaimMetadata(BaseModel):
    """Public-facing claim-page payload — what the invite acceptor sees."""

    email: str
    role: str
    target_l2_id: str | None
    inviter_username: str
    expires_at: str


class ClaimRequest(BaseModel):
    """Body for ``POST /invites/{jwt}/claim``.

    V1 only: password. Username is server-determined from the invite's
    email so password managers can't silently override it (agent#249).
    Passkey enrollment is a follow-up step in FO-1d. The session bearer
    returned here lets the user reach FO-1d's enrollment endpoint.

    ``username`` is accepted but IGNORED — kept for backward compat
    with any caller that still sends it; the server always sets
    ``username = invite.email``.
    """

    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class ClaimResponse(BaseModel):
    """Response body — session bearer + identity."""

    token: str
    username: str


def _to_public(invite: Invite) -> InvitePublic:
    return InvitePublic(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        target_l2_id=invite.target_l2_id,
        issued_by=invite.issued_by,
        issued_at=invite.issued_at,
        expires_at=invite.expires_at,
        claimed_at=invite.claimed_at,
        claimed_by=invite.claimed_by,
        revoked_at=invite.revoked_at,
        status=invite.status,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(tags=["invites"])


@router.post("/admin/invites", status_code=201, response_model=InvitePublic)
async def create_invite_route(
    request: CreateInviteRequest,
    fastapi_request: Request,  # noqa: ARG001 — reserved for future host-derivation
    admin_username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
    email_sender: EmailSender | MockEmailSender = Depends(get_email_sender),
) -> InvitePublic:
    """Mint an invite + send the email. Returns metadata; never the JWT.

    Validation:
      * ``role != "enterprise_admin"`` → ``target_l2_id`` is required.
      * Caller must be admin (``require_admin``).
    """
    admin = await store.get_user(admin_username)
    if admin is None:
        raise HTTPException(status_code=404, detail="Admin user not found")

    if request.role != "enterprise_admin" and not request.target_l2_id:
        raise HTTPException(
            status_code=422,
            detail="target_l2_id is required for non-enterprise_admin roles",
        )

    try:
        invite, token = mint_invite(
            store,
            email=request.email,
            role=request.role,  # type: ignore[arg-type]
            target_l2_id=request.target_l2_id,
            issued_by=int(admin["id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    expiry = datetime.fromisoformat(invite.expires_at)
    try:
        email_sender.send_invite(
            to=str(request.email),
            jwt=token,
            inviter_name=admin_username,
            enterprise_name=request.enterprise_name,
            expiry=expiry,
        )
    except Exception:  # noqa: BLE001
        # Mint succeeded but email failed — we keep the row and surface
        # the failure. Admin can revoke + retry; we do NOT roll back
        # because the JWT is already minted and a parallel SES retry
        # could deliver the original token if we re-mint with a new jti.
        log.exception("invite email send failed for invite_id=%s", invite.id)
        raise HTTPException(
            status_code=502,
            detail="invite minted but email delivery failed; revoke + retry",
        ) from None

    return _to_public(invite)


@router.get("/admin/invites", response_model=InvitesPublic)
async def list_invites_route(
    status: Literal["pending", "claimed", "expired", "revoked"] | None = Query(default=None),
    _admin_username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> InvitesPublic:
    """List invites, optionally filtered by lifecycle ``status``."""
    invites = list_invites(store, status=status)
    return InvitesPublic(
        data=[_to_public(inv) for inv in invites],
        count=len(invites),
    )


@router.delete("/admin/invites/{invite_id}", response_model=InvitePublic)
async def revoke_invite_route(
    invite_id: int,
    admin_username: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> InvitePublic:
    """Revoke an invite. Idempotent — repeated revokes return the same row."""
    admin = await store.get_user(admin_username)
    if admin is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    invite = revoke_invite(store, invite_id=invite_id, by_user_id=int(admin["id"]))
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    return _to_public(invite)


@router.get("/invites/{token}", response_model=ClaimMetadata)
async def get_invite_metadata_route(
    token: str,
    store: SqliteStore = Depends(get_store),
) -> ClaimMetadata:
    """Public — render the claim page using the JWT bearer in the URL.

    Returns ``410 Gone`` for any non-pending invite (revoked / expired
    / claimed) so the claim page renders a single, unambiguous "this
    link is no longer valid" state. ``404`` is reserved for "the JWT
    has no matching DB row" — i.e. forgery / unknown jti.
    """
    invite = validate_invite_jwt(token, store)
    if invite is None:
        # Distinguish forgery from lifecycle for the right status code.
        # Re-decode locally to see if the bearer is at least signed.
        import jwt as pyjwt

        try:
            payload = pyjwt.decode(
                token,
                _get_jwt_secret(),
                algorithms=["HS256"],
                audience="invite",
                issuer="8th-layer.ai",
                options={"require": ["jti"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=410, detail="invite expired") from exc
        except pyjwt.PyJWTError as exc:
            raise HTTPException(status_code=404, detail="invite not found") from exc
        # Signed + non-expired but DB lookup or status said no — likely
        # claimed/revoked. Re-fetch by jti for the right discriminant.
        from .invites import _get_by_jti

        row = _get_by_jti(store, payload["jti"])
        if row is None:
            raise HTTPException(status_code=404, detail="invite not found")
        if row.revoked_at is not None:
            raise HTTPException(status_code=410, detail="invite revoked")
        if row.claimed_at is not None:
            raise HTTPException(status_code=410, detail="invite already claimed")
        raise HTTPException(status_code=410, detail="invite expired")

    inviter_username = _lookup_username(store, invite.issued_by)
    return ClaimMetadata(
        email=invite.email,
        role=invite.role,
        target_l2_id=invite.target_l2_id,
        inviter_username=inviter_username or "admin",
        expires_at=invite.expires_at,
    )


def _lookup_username(store: SqliteStore, user_id: int) -> str | None:
    """Sync helper — fetch ``users.username`` by ``users.id`` directly."""
    from sqlalchemy import text

    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text("SELECT username FROM users WHERE id = :id"),
            {"id": user_id},
        ).fetchone()
    return row[0] if row else None


@router.post("/invites/{token}/claim", response_model=ClaimResponse)
async def claim_invite_route(
    token: str,
    request: ClaimRequest,
    response: Response,
    store: SqliteStore = Depends(get_store),
) -> ClaimResponse:
    """Public — accept the invite, provision the user, return a session bearer.

    Single-use enforcement is in ``invites.claim_invite``; this route
    only translates the discriminated outcome into the right HTTP code.
    """
    # Validate the JWT shape early so we can return the user-friendly
    # status before doing any user creation work.
    metadata = validate_invite_jwt(token, store)
    if metadata is None:
        # Surface the same per-status mapping as the GET handler.
        import jwt as pyjwt

        try:
            payload = pyjwt.decode(
                token,
                _get_jwt_secret(),
                algorithms=["HS256"],
                audience="invite",
                issuer="8th-layer.ai",
                options={"require": ["jti"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=410, detail="invite expired") from exc
        except pyjwt.PyJWTError as exc:
            raise HTTPException(status_code=404, detail="invite not found") from exc
        from .invites import _get_by_jti

        row = _get_by_jti(store, payload["jti"])
        if row is None:
            raise HTTPException(status_code=404, detail="invite not found")
        if row.revoked_at is not None:
            raise HTTPException(status_code=410, detail="invite revoked")
        if row.claimed_at is not None:
            raise HTTPException(status_code=409, detail="invite already claimed")
        raise HTTPException(status_code=410, detail="invite expired")

    # agent#249: username is always the invite email — server-determined,
    # not form-submitted. This kills the password-manager-overrides-username
    # failure mode where claim succeeds but the user can't log in afterward
    # because they don't know what got auto-filled.
    canonical_username = metadata.email

    user_id = await ensure_user(
        store,
        username=canonical_username,
        password=request.password,
        email=metadata.email,
        role=metadata.role,
    )

    outcome = claim_invite(store, token=token, claiming_user_id=user_id)
    if outcome.kind == "ok":
        # H-1: refuse session minting when this user's persona is soft-disabled.
        # An invite issued before disable shouldn't let the user back in.
        assignment = await store.get_persona_assignment(canonical_username)
        if assignment is not None and assignment.get("disabled_at") is not None:
            raise HTTPException(status_code=403, detail="user is disabled")
        # FO-1c: set the session cookie + return the bearer in the body.
        # The HTML claim page navigates to "/" after this; the browser
        # sends the cookie along, so the user lands authenticated.
        session = mint_session_cookie(response, username=canonical_username)
        return ClaimResponse(token=session, username=canonical_username)
    if outcome.kind == "already_claimed":
        raise HTTPException(status_code=409, detail="invite already claimed")
    if outcome.kind == "revoked":
        raise HTTPException(status_code=410, detail="invite revoked")
    if outcome.kind == "expired":
        raise HTTPException(status_code=410, detail="invite expired")
    raise HTTPException(status_code=404, detail="invite not found")


# Re-export role type for routers that consume the same vocabulary.
__all__ = [
    "InviteRole",
    "InvitesPublic",
    "InvitePublic",
    "router",
    "get_email_sender",
]
