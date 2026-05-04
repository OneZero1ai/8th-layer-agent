"""Authentication: password hashing, JWT creation and validation.

SEC-MED M-4 — JWT iss/aud claims pin a token to the L2 that minted it.
Combined with H-6 (per-L2 CQ_JWT_SECRET in SSM) the prior single-
point-of-failure trust model — "leak any L2's secret, mint as anyone
on any L2" — collapses to "leak L2 X's secret, impersonate users on
L2 X only." Cross-L2 user-auth is gone; the only L2-to-L2 auth is the
per-Enterprise AIGRP peer key + Ed25519 forward signatures.

The aggregator's old _admin_service_jwt path used to mint a JWT under
its own secret and present it to fleet L2s — that worked only because
the secret was shared. With per-L2 secrets it can't work; replaced
with /aigrp/peers-active (peer-key gated) so the network proxy reads
presence over the same trust channel as the rest of /aigrp/*.
"""

import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import aigrp
from .api_keys import (
    TOKEN_NAMESPACE,
    TOKEN_VERSION,
    decode_token,
    encode_token,
    generate_secret,
    hash_secret,
    secret_prefix,
)
from .deps import get_api_key_pepper, get_store
from .store._sqlite import SqliteStore
from .ttl import parse_ttl

MAX_ACTIVE_API_KEYS_PER_USER = 20


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(username: str, *, secret: str, ttl_hours: int = 24) -> str:
    """Create a JWT token bound to this L2's identity.

    iss/aud both set to ``aigrp.self_l2_id()`` so a token minted on L2
    A is rejected by L2 B's ``verify_token`` even if the two share a
    secret. Combined with per-L2 secrets (H-6), this closes the
    cross-L2-impersonation surface the audit flagged.
    """
    now = datetime.now(UTC)
    self_l2 = aigrp.self_l2_id()
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(hours=ttl_hours),
        "iss": self_l2,
        "aud": self_l2,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_token(token: str, *, secret: str) -> dict[str, Any]:
    """Verify a JWT token and return its claims.

    Requires ``iss`` and ``aud`` to both match this L2's identity. A
    legacy token (no iss/aud) is rejected — the breaking change is
    deliberate (closes M-4 / H-6); existing users re-login within
    the 24h TTL anyway.
    """
    self_l2 = aigrp.self_l2_id()
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience=self_l2,
        issuer=self_l2,
        options={"require": ["iss", "aud", "sub", "exp"]},
    )


class LoginRequest(BaseModel):
    """Login request body."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Login response body."""

    token: str
    username: str


class MeResponse(BaseModel):
    """Current user response body — server's authoritative view of the caller.

    The shape is the same regardless of auth method (JWT or API key); the
    auth-method-specific fields (``api_key_id``, ``expires_at``,
    ``issued_at``) are populated only when the caller used an API key.
    """

    username: str
    created_at: str
    enterprise_id: str
    group_id: str
    l2_id: str
    role: str
    persona: str | None = None
    auth_kind: str
    api_key_id: str | None = None
    expires_at: str | None = None
    issued_at: str | None = None


class Message(BaseModel):
    """Generic message response body."""

    message: str


class CreateApiKeyRequest(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(min_length=1, max_length=64)
    ttl: str = Field(min_length=1, max_length=16)
    labels: list[str] = Field(default_factory=list, max_length=16)


class ApiKeyPublic(BaseModel):
    """Public view of an API key; never includes the plaintext or hash."""

    id: str
    name: str
    labels: list[str]
    prefix: str
    ttl: str
    expires_at: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    is_expired: bool
    is_active: bool


class CreateApiKeyResponse(ApiKeyPublic):
    """Create response; the plaintext ``token`` is returned exactly once."""

    token: str


class ApiKeysPublic(BaseModel):
    """Collection wrapper for API key listings.

    The envelope shape leaves room for pagination metadata (e.g. a
    ``next_cursor`` field) without breaking existing clients.
    """

    data: list[ApiKeyPublic]
    count: int


def _to_public(row: dict[str, Any]) -> ApiKeyPublic:
    """Build the public view of an API key row."""
    now = datetime.now(UTC)
    expires_at = datetime.fromisoformat(row["expires_at"])
    is_expired = expires_at <= now
    is_active = row["revoked_at"] is None and not is_expired
    return ApiKeyPublic(
        id=row["id"],
        name=row["name"],
        labels=list(row.get("labels") or []),
        prefix=row["key_prefix"],
        ttl=row["ttl"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
        is_expired=is_expired,
        is_active=is_active,
    )


def _normalise_labels(labels: list[str]) -> list[str]:
    """Trim, deduplicate, and drop empty labels while preserving order."""
    seen: dict[str, None] = {}
    for label in labels:
        cleaned = label.strip()
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen.keys())


def _get_jwt_secret() -> str:
    """Return the JWT secret, failing if unset.

    Returns:
        The value of the CQ_JWT_SECRET environment variable.

    Raises:
        RuntimeError: If the environment variable is not set.
    """
    secret = os.environ.get("CQ_JWT_SECRET")
    if not secret:
        raise RuntimeError("CQ_JWT_SECRET environment variable is required")
    return secret


def get_current_user(request: Request) -> str:
    """FastAPI dependency that extracts and validates the JWT from the Authorization header.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The username extracted from the validated token.

    Raises:
        HTTPException: With status 401 if the header is missing, malformed, or the token is invalid.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = auth_header.removeprefix("Bearer ")
    secret = _get_jwt_secret()
    try:
        payload = verify_token(token, secret=secret)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    return payload["sub"]


async def require_admin(
    request: Request,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> str:
    """FastAPI dependency: caller must be an authenticated admin user.

    Returns the username on success; 401 on missing/invalid JWT (raised
    by the chained ``get_current_user`` dep), 403 when the caller is
    authenticated but not an admin. Admin-ness is global in v1 — there
    is no per-Enterprise scoping yet (see plan doc, Lane D).
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return username


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(request: LoginRequest, store: SqliteStore = Depends(get_store)) -> LoginResponse:
    """Authenticate a user and return a JWT token.

    Args:
        request: Login credentials.
        store: The store dependency.

    Returns:
        A LoginResponse with a signed JWT and the username.

    Raises:
        HTTPException: With status 401 if credentials are invalid.
    """
    user = await store.get_user(request.username)
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(request.username, secret=_get_jwt_secret())
    return LoginResponse(token=token, username=request.username)


@dataclass
class _CallerIdentity:
    """Resolved caller identity, accepting either JWT or API-key tokens."""

    username: str
    auth_kind: str  # "jwt" | "api_key"
    api_key_id: str | None = None
    expires_at: str | None = None
    issued_at: str | None = None


async def _resolve_caller(request: Request, store: SqliteStore) -> _CallerIdentity:
    """Authenticate the caller via either bearer-token shape and return identity.

    Tokens prefixed with ``cqa.v1.`` are decoded as API keys; everything
    else is verified as a JWT. 401 on either path's failure.
    """
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = header.removeprefix("Bearer ")
    if token.startswith(f"{TOKEN_NAMESPACE}.{TOKEN_VERSION}."):
        try:
            key_id, secret = decode_token(token)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid API key") from exc
        pepper = get_api_key_pepper(request)
        row = await store.get_active_api_key_by_id(key_id.hex)
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if not hmac.compare_digest(row["key_hash"], hash_secret(secret, pepper=pepper)):
            raise HTTPException(status_code=401, detail="Invalid API key")
        return _CallerIdentity(
            username=row["username"],
            auth_kind="api_key",
            api_key_id=row["id"],
            expires_at=row["expires_at"],
            issued_at=row["created_at"],
        )
    secret = _get_jwt_secret()
    try:
        payload = verify_token(token, secret=secret)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    return _CallerIdentity(username=payload["sub"], auth_kind="jwt")


@router.get("/me")
async def me(request: Request, store: SqliteStore = Depends(get_store)) -> MeResponse:
    """Return the server's authoritative view of the caller's identity.

    Accepts either a JWT (from ``POST /auth/login``) or an API key (from
    ``POST /auth/api-keys``). The response shape is identical for both;
    the API-key-only fields (``api_key_id``, ``expires_at``,
    ``issued_at``) are ``None`` for JWT callers.
    """
    caller = await _resolve_caller(request, store)
    user = await store.get_user(caller.username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    enterprise_id = user.get("enterprise_id") or "default-enterprise"
    group_id = user.get("group_id") or "default-group"
    return MeResponse(
        username=user["username"],
        created_at=user["created_at"],
        enterprise_id=enterprise_id,
        group_id=group_id,
        l2_id=f"{enterprise_id}/{group_id}",
        role=user.get("role") or "user",
        persona=None,
        auth_kind=caller.auth_kind,
        api_key_id=caller.api_key_id,
        expires_at=caller.expires_at,
        issued_at=caller.issued_at,
    )


async def _require_user_id(store: SqliteStore, username: str) -> int:
    """Return the integer user id for the authenticated caller.

    Raises:
        HTTPException: 404 if the user record has been removed while the JWT remains valid.
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return int(user["id"])


@router.post("/api-keys", status_code=201)
async def create_api_key_route(
    request: CreateApiKeyRequest,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
    pepper: str = Depends(get_api_key_pepper),
) -> CreateApiKeyResponse:
    """Create a new API key owned by the authenticated user.

    The plaintext ``token`` is returned exactly once, in this response. It
    cannot be retrieved afterwards; if the caller loses it, they must revoke
    and create a new key.

    Raises:
        HTTPException: 422 if the TTL is invalid, 409 if the user already has
            the maximum number of active keys.
    """
    try:
        duration = parse_ttl(request.ttl)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user_id = await _require_user_id(store, username)
    if await store.count_active_api_keys_for_user(user_id) >= MAX_ACTIVE_API_KEYS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=f"Maximum of {MAX_ACTIVE_API_KEYS_PER_USER} active API keys per user",
        )
    key_id = uuid.uuid4()
    secret = generate_secret()
    plaintext = encode_token(key_id=key_id, secret=secret)
    expires_at = (datetime.now(UTC) + duration).isoformat()
    row = await store.create_api_key(
        key_id=key_id.hex,
        user_id=user_id,
        name=request.name,
        labels=_normalise_labels(request.labels),
        key_prefix=secret_prefix(secret),
        key_hash=hash_secret(secret, pepper=pepper),
        ttl=request.ttl,
        expires_at=expires_at,
    )
    public = _to_public(row)
    return CreateApiKeyResponse(**public.model_dump(), token=plaintext)


@router.get("/api-keys")
async def list_api_keys_route(
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> ApiKeysPublic:
    """Return the authenticated user's API keys. Never returns plaintext.

    Revoked keys are included with ``is_active: false`` so users can audit
    their own revocation history.
    """
    user_id = await _require_user_id(store, username)
    data = [_to_public(row) for row in await store.list_api_keys_for_user(user_id)]
    return ApiKeysPublic(data=data, count=len(data))


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key_route(
    key_id: str,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> Message:
    """Revoke the given API key if it belongs to the caller.

    Revocation is a state transition; the row is retained with
    ``revoked_at`` set. Repeated revocations are idempotent and succeed.
    A 404 is returned when the key does not exist or is owned by a
    different user (uniform response, no enumeration oracle).
    """
    user_id = await _require_user_id(store, username)
    if await store.get_api_key_for_user(user_id=user_id, key_id=key_id) is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await store.revoke_api_key(user_id=user_id, key_id=key_id)
    return Message(message="API key revoked.")
