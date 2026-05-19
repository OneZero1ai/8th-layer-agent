"""FO-4: Self-service Add Agent — admin routes for minting agent keys.

Endpoints (all gated on ``require_admin``, mounted under ``/api/v1``):

  POST /admin/agent-keys   — mint a new agent persona + cqa.v1.* key
  GET  /admin/agent-keys   — list every agent-persona key on this L2

An "agent" here is a stub ``users`` row carrying a ``persona_assignments``
row at ``persona='agent'`` plus an ``api_keys`` row — the exact shape a
Human persona uses (see ``persona_routes.create_persona``), minus the
magic-link invite. The plaintext ``cqa.v1.*`` token is returned exactly
once in the mint response and is never recoverable afterwards.

Per Decision 33: FO-4 V1 mints full-capability keys — there is no
per-key permission-scope selector because no scope-enforcement mechanism
exists yet (``api_keys.labels`` is free-form; ``auth.scope_filter``
scopes only by tenancy). Scope enforcement is a tracked follow-up.

Auth: ``require_admin`` — the same FO-1c session-cookie gate used by
``persona_routes`` and ``l2_provision_routes``.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_keys import encode_token, generate_secret, hash_secret, secret_prefix
from .auth import ApiKeyPublic, _to_public, hash_password, require_admin
from .deps import get_api_key_pepper, get_store
from .store._sqlite import SqliteStore
from .tables import DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID
from .ttl import parse_ttl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/agent-keys", tags=["admin", "agent-keys"])

HarnessEnum = Literal["claude-code", "claude-desktop", "openclaw", "other"]

DEFAULT_AGENT_TTL = "60d"

# An agent username is ``agent-<slug>`` so agent stub users never collide
# with Human usernames (which are email-derived).
_AGENT_USERNAME_PREFIX = "agent-"
_SLUG_MAX = 48


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class MintAgentKeyRequest(BaseModel):
    """Request body for minting an agent key."""

    agent_name: str = Field(min_length=1, max_length=64)
    harness: HarnessEnum
    ttl: str = Field(default=DEFAULT_AGENT_TTL, min_length=1, max_length=16)


class AgentInstallPaths(BaseModel):
    """The pieces the admin UI renders into install paths.

    The backend ships the fully-assembled ``join_command``; the UI builds
    the plugin-install command and the QR payload from the scalar fields
    so a marketplace-slug change never needs a backend redeploy.
    """

    join_command: str
    enterprise_id: str
    l2: str
    persona: str = "agent"


class AgentKeyPublic(ApiKeyPublic):
    """Public view of an agent key — api-key fields plus the owning agent."""

    agent_username: str


class MintAgentKeyResponse(AgentKeyPublic):
    """Mint response. ``token`` is the plaintext key, returned exactly once."""

    token: str
    install: AgentInstallPaths


class AgentKeyListResponse(BaseModel):
    """Collection wrapper for the agent-key admin table."""

    data: list[AgentKeyPublic]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify_agent_name(agent_name: str) -> str:
    """Derive a URL/username-safe slug from a free-text agent name.

    Lowercases, maps every run of non-alphanumerics to a single hyphen,
    trims leading/trailing hyphens, and caps length. Raises 422 when the
    name has no usable characters (e.g. all punctuation).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.strip().lower()).strip("-")
    slug = slug[:_SLUG_MAX].strip("-")
    if not slug:
        raise HTTPException(
            status_code=422,
            detail="agent_name must contain at least one letter or digit",
        )
    return slug


def _build_join_command(*, enterprise_id: str, l2: str, token: str) -> str:
    """Assemble the ``8l join`` one-liner an agent operator copy-pastes."""
    return f"8l join --enterprise {enterprise_id} --l2 {l2} --persona agent --api-key {token} --non-interactive"


def _to_agent_public(row: dict, agent_username: str) -> AgentKeyPublic:
    """Build the agent-key public view from an api-key row."""
    base = _to_public(row)
    return AgentKeyPublic(**base.model_dump(), agent_username=agent_username)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=MintAgentKeyResponse, status_code=201)
async def mint_agent_key(
    req: MintAgentKeyRequest,
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
    pepper: str = Depends(get_api_key_pepper),
) -> MintAgentKeyResponse:
    """Mint a new agent persona and its ``cqa.v1.*`` key.

    Creates a stub ``users`` row, a ``persona_assignments`` row at
    ``persona='agent'``, and an ``api_keys`` row in that order. The new
    agent inherits the minting admin's ``enterprise_id`` / ``group_id``
    so it lands in the same L2 tenancy. The plaintext token is returned
    once; only its HMAC hash + 8-char prefix are persisted.

    Raises:
        HTTPException: 422 on an unusable agent_name or bad TTL,
            409 when the derived agent username already exists.
    """
    try:
        duration = parse_ttl(req.ttl)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    username = _AGENT_USERNAME_PREFIX + _slugify_agent_name(req.agent_name)

    if await store.get_user(username) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"an agent named {req.agent_name!r} already exists (username {username!r}) — pick a different name",
        )

    # Inherit the minting admin's tenancy so the agent lands in this L2.
    admin_user = await store.get_user(admin)
    enterprise_id = (admin_user or {}).get("enterprise_id") or DEFAULT_ENTERPRISE_ID
    group_id = (admin_user or {}).get("group_id") or DEFAULT_GROUP_ID

    # 1. Stub user — random password hash, never usable for login. The
    #    agent authenticates only via its cqa.v1.* bearer key.
    stub_hash = hash_password(uuid.uuid4().hex)
    await store.create_user(
        username,
        stub_hash,
        role="user",
        enterprise_id=enterprise_id,
        group_id=group_id,
    )

    # 2. Persona assignment — marks the stub user as an agent.
    now = datetime.now(UTC).isoformat()
    await store.upsert_persona_assignment(
        username=username,
        persona="agent",
        assigned_at=now,
        assigned_by=admin,
        audit_action="CREATED",
        audit_old_persona=None,
    )

    # 3. API key owned by the agent stub user.
    new_user = await store.get_user(username)
    if new_user is None:  # pragma: no cover — created two statements ago
        raise HTTPException(status_code=500, detail="agent user vanished after creation")
    user_id = int(new_user["id"])

    key_id = uuid.uuid4()
    secret = generate_secret()
    plaintext = encode_token(key_id=key_id, secret=secret)
    expires_at = (datetime.now(UTC) + duration).isoformat()
    row = await store.create_api_key(
        key_id=key_id.hex,
        user_id=user_id,
        name=req.agent_name,
        labels=[f"harness:{req.harness}", "persona:agent"],
        key_prefix=secret_prefix(secret),
        key_hash=hash_secret(secret, pepper=pepper),
        ttl=req.ttl,
        expires_at=expires_at,
    )

    log.info(
        "FO-4: admin %r minted agent key for %r (harness=%s, l2=%s/%s)",
        admin,
        username,
        req.harness,
        enterprise_id,
        group_id,
    )

    public = _to_agent_public(row, agent_username=username)
    install = AgentInstallPaths(
        join_command=_build_join_command(enterprise_id=enterprise_id, l2=group_id, token=plaintext),
        enterprise_id=enterprise_id,
        l2=group_id,
    )
    return MintAgentKeyResponse(**public.model_dump(), token=plaintext, install=install)


@router.get("", response_model=AgentKeyListResponse)
async def list_agent_keys(
    admin: str = Depends(require_admin),
    store: SqliteStore = Depends(get_store),
) -> AgentKeyListResponse:
    """List every agent-persona key on this L2 (never returns plaintext).

    Revoked and expired keys are included with ``is_active: false`` so the
    admin table doubles as a revocation-history audit.
    """
    rows = await store.list_agent_api_keys()
    data = [_to_agent_public(row, agent_username=row["agent_username"]) for row in rows]
    return AgentKeyListResponse(data=data, count=len(data))
