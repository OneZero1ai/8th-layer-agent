"""AS-1: Personas tab — admin routes for Human persona management.

Endpoints (all gated on ``require_admin``):

  GET  /admin/personas          — paginated list of Humans + persona assignment
  POST /admin/personas          — create Human (email) + assign initial persona;
                                   fires magic-link invite via email_sender
  PATCH /admin/personas/{username} — change persona for an existing Human
  POST  /admin/personas/{username}/disable — soft-disable (sets disabled_at)

Auth: ``require_admin`` dependency (existing FO-1c session cookie + aud=admin
discriminant). Every endpoint chains the same ``require_admin`` dep already used
throughout the codebase (e.g. invite_routes, admin_routes).

The four valid personas for v1: admin, viewer, agent, external-collaborator.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .auth import require_admin
from .deps import get_store
from .email_sender import EmailSender, MockEmailSender
from .invite_routes import get_email_sender  # dep re-used so overrides apply in tests
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

PersonaEnum = Literal["admin", "viewer", "agent", "external-collaborator"]

router = APIRouter(prefix="/admin/personas", tags=["admin", "personas"])


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class PersonaAssignment(BaseModel):
    """Public view of a persona assignment row."""

    username: str
    email: str | None
    persona: str
    assigned_at: str
    assigned_by: str
    disabled_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None


class PersonaListResponse(BaseModel):
    """Paginated list of persona assignments."""

    items: list[PersonaAssignment]
    total: int
    limit: int
    offset: int


class CreatePersonaRequest(BaseModel):
    """POST /admin/personas — create a Human and assign initial persona.

    ``email`` is used to send the magic-link invite (reuses invite_routes
    email flow). ``username`` is the desired username for the new user.
    """

    email: str = Field(min_length=1, max_length=320)
    username: str = Field(min_length=1, max_length=64)
    persona: PersonaEnum
    enterprise_name: str = Field(default="8th-Layer.ai")

    @field_validator("email")
    @classmethod
    def _basic_email_shape(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("invalid email address")
        return value.strip()


class CreatePersonaResponse(BaseModel):
    """Response for POST /admin/personas."""

    username: str
    email: str
    persona: str
    assigned_at: str
    assigned_by: str
    invite_sent: bool


class PatchPersonaRequest(BaseModel):
    """PATCH /admin/personas/{username} — change persona."""

    persona: PersonaEnum


class PatchPersonaResponse(BaseModel):
    """Response for PATCH /admin/personas/{username}."""

    username: str
    persona: str
    assigned_at: str
    assigned_by: str


class DisableResponse(BaseModel):
    """Response for POST /admin/personas/{username}/disable."""

    username: str
    disabled_at: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=PersonaListResponse)
async def list_personas(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> PersonaListResponse:
    """List all Humans with their current persona assignment (paginated).

    Returns users who have a persona assignment row. Users without an
    assignment are not returned (they have not been onboarded to the
    persona system yet).
    """
    rows, total = await store.list_persona_assignments(limit=limit, offset=offset)
    items = [
        PersonaAssignment(
            username=r["username"],
            email=r.get("email"),
            persona=r["persona"],
            assigned_at=r["assigned_at"],
            assigned_by=r["assigned_by"],
            disabled_at=r.get("disabled_at"),
        )
        for r in rows
    ]
    return PersonaListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=CreatePersonaResponse, status_code=201)
async def create_persona(
    req: CreatePersonaRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
    email_sender: EmailSender | MockEmailSender = Depends(get_email_sender),
) -> CreatePersonaResponse:
    """Create a new Human with an initial persona assignment.

    Fires a magic-link invite email via the existing EmailSender. The
    persona assignment row is written before the invite is sent; if the
    send fails the assignment persists (admin can resend via invite flow).

    409 when a persona assignment already exists for this username.
    422 when the email is malformed.
    """
    from .email_sender import EmailSender
    from .invite_routes import get_email_sender

    # Check for an existing assignment.
    existing = await store.get_persona_assignment(req.username)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"persona assignment already exists for username={req.username!r}",
        )

    # Ensure the user row exists (or create it).  We create a stub user
    # without a password hash — the invite claim flow will set the password.
    from .auth import hash_password

    user = await store.get_user(req.username)
    if user is None:
        # Create a stub user so the FK on persona_assignments resolves.
        stub_hash = hash_password(uuid.uuid4().hex)  # random, never usable directly
        await store.create_user(req.username, stub_hash)
        # Persist email on the user row so /auth/me can surface it.
        await store.set_user_email(req.username, req.email)

    now = datetime.now(UTC).isoformat()
    await store.upsert_persona_assignment(
        username=req.username,
        persona=req.persona,
        assigned_at=now,
        assigned_by=admin,
    )

    # Fire invite email (best-effort — same pattern as invite_routes).
    invite_sent = False
    try:
        from .invites import mint_invite

        admin_user = await store.get_user(admin)
        issued_by_id = int(admin_user["id"]) if admin_user else 0
        # Use enterprise_admin role so target_l2_id is not required.
        # The actual access level is governed by the persona assignment, not
        # the invite role — the invite is purely the delivery mechanism.
        _invite, token = mint_invite(
            store,
            email=req.email,
            role="enterprise_admin",
            target_l2_id=None,
            issued_by=issued_by_id,
        )
        expiry = datetime.fromisoformat(_invite.expires_at)
        email_sender.send_invite(
            to=req.email,
            jwt=token,
            inviter_name=admin,
            enterprise_name=req.enterprise_name,
            expiry=expiry,
        )
        invite_sent = True
    except Exception:  # noqa: BLE001
        log.exception("invite send failed for persona create username=%s", req.username)
        # Do not raise — assignment was written; admin can use invite UI to resend.

    return CreatePersonaResponse(
        username=req.username,
        email=req.email,
        persona=req.persona,
        assigned_at=now,
        assigned_by=admin,
        invite_sent=invite_sent,
    )


@router.patch("/{username}", response_model=PatchPersonaResponse)
async def patch_persona(
    username: str,
    req: PatchPersonaRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> PatchPersonaResponse:
    """Change the persona for an existing Human.

    404 when the username has no assignment row.
    """
    existing = await store.get_persona_assignment(username)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no persona assignment for {username!r}")

    now = datetime.now(UTC).isoformat()
    await store.upsert_persona_assignment(
        username=username,
        persona=req.persona,
        assigned_at=now,
        assigned_by=admin,
    )
    return PatchPersonaResponse(
        username=username,
        persona=req.persona,
        assigned_at=now,
        assigned_by=admin,
    )


@router.post("/{username}/disable", response_model=DisableResponse)
async def disable_persona(
    username: str,
    admin: str = Depends(require_admin),  # noqa: ARG001 — auth gate only
    store: SqliteStore = Depends(get_store),
) -> DisableResponse:
    """Soft-disable a Human's persona assignment.

    Sets ``disabled_at`` to now. The users row is NOT deleted — audit trail
    is preserved. Re-enabling is done via PATCH (which clears disabled_at).

    404 when no assignment row exists.
    409 when the assignment is already disabled.
    """
    existing = await store.get_persona_assignment(username)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no persona assignment for {username!r}")
    if existing.get("disabled_at") is not None:
        raise HTTPException(status_code=409, detail=f"{username!r} is already disabled")

    now = datetime.now(UTC).isoformat()
    await store.disable_persona_assignment(username=username, disabled_at=now)
    return DisableResponse(username=username, disabled_at=now)
