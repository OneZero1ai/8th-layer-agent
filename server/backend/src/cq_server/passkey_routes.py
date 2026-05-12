"""FastAPI router for passkey enrollment + login (FO-1a, #191).

Four endpoints, paired begin/finish per ceremony:

* ``POST /auth/passkey/enroll/begin``  — authenticated. Returns the
  WebAuthn registration options the browser passes to
  ``navigator.credentials.create``.
* ``POST /auth/passkey/enroll/finish`` — authenticated. Verifies the
  attestation response, persists the credential row.
* ``POST /auth/passkey/login/begin``   — anonymous. Caller supplies a
  username (or, post-FO-1b, an email); returns assertion options.
* ``POST /auth/passkey/login/finish``  — anonymous. Verifies the
  assertion, increments ``sign_count``, mints a JWT via
  ``auth.create_token`` (FO-1c will swap that for a cookie-bound
  session token).

Out of scope here (deferred to FO-1b/c):
* Self-service signup / first-credential enrollment for users with no
  password row — FO-2 owns the ``/signup`` shape.
* Email-based account discovery for ``login/begin`` — FO-1b lands the
  invite + magic-link path; this PR keeps things username-keyed.
* Replacing JWT with a cookie-bound session — FO-1c.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)

from . import passkey
from .auth import get_current_user
from .deps import get_store
from .store._sqlite import SqliteStore
from .web_session import mint_session_cookie

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/passkey", tags=["auth", "passkey"])


# --- Request / response shapes -------------------------------------------


class EnrollFinishRequest(BaseModel):
    """Browser-side WebAuthn ``AttestationResponse`` plus optional metadata."""

    credential: dict[str, Any]
    name: str | None = Field(default=None, max_length=64)
    transports: list[str] | None = Field(default=None, max_length=8)


class EnrollFinishResponse(BaseModel):
    """Response from a successful enrollment finish."""

    credential_db_id: int
    credential_id_b64u: str
    sign_count: int


class LoginBeginRequest(BaseModel):
    """Username for which to generate assertion options."""

    username: str = Field(min_length=1, max_length=128)


class LoginFinishRequest(BaseModel):
    """Browser-side WebAuthn ``AssertionResponse`` plus the asserted username."""

    username: str = Field(min_length=1, max_length=128)
    credential: dict[str, Any]


class LoginFinishResponse(BaseModel):
    """JWT bearer token + the new sign_count after a successful assertion."""

    token: str
    username: str
    sign_count: int


# --- Helpers --------------------------------------------------------------


def _user_id_bytes(user_id: int) -> bytes:
    """Encode the integer user.id as bytes for WebAuthn's ``user.id`` field.

    WebAuthn requires opaque bytes, max 64. We use a fixed-width 8-byte
    big-endian encoding so the same row produces the same WebAuthn id
    every time (important for ``excludeCredentials`` parity).
    """
    return int(user_id).to_bytes(8, "big", signed=False)


# --- Routes: enrollment ---------------------------------------------------


@router.post("/enroll/begin")
async def enroll_begin(
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    """Generate registration options for the authenticated caller.

    The caller is identified by the existing JWT/API-key auth; we look
    up the user row, build options including any already-enrolled
    credential ids in ``excludeCredentials``, and cache the challenge.
    The credential ``name`` is supplied at finish time (in
    ``EnrollFinishRequest``), not begin — begin takes no body.
    """
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = int(user["id"])
    existing = await store.list_webauthn_credentials_for_user(user_id)
    return passkey.begin_registration(
        username=username,
        user_id_bytes=_user_id_bytes(user_id),
        display_name=username,
        existing_credential_ids=[row["credential_id"] for row in existing],
    )


@router.post("/enroll/finish")
async def enroll_finish(
    request: EnrollFinishRequest,
    username: str = Depends(get_current_user),
    store: SqliteStore = Depends(get_store),
) -> EnrollFinishResponse:
    """Verify the attestation response and persist the credential."""
    user = await store.get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        verified = passkey.finish_registration(
            username=username,
            credential=request.credential,
            transports=request.transports,
        )
    except ValueError as exc:
        # ValueError comes from our own `passkey.finish_registration` for
        # cache-miss / TTL conditions — message is operator-controlled,
        # safe to surface.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (
        InvalidRegistrationResponse,
        InvalidAuthenticationResponse,
        InvalidJSONStructure,
    ) as exc:
        # py_webauthn-internal failure messages can leak parsed-CBOR
        # fragments and authenticator-data fields; log server-side, return
        # a generic 400 to the client.
        logger.info("passkey enrollment verification failed for %s: %s", username, exc)
        raise HTTPException(status_code=400, detail="passkey verification failed") from None

    transports_csv = ",".join(verified.transports) if verified.transports else None
    row = await store.insert_webauthn_credential(
        user_id=int(user["id"]),
        credential_id=verified.credential_id,
        public_key=verified.public_key,
        sign_count=verified.sign_count,
        transports=transports_csv,
        aaguid=verified.aaguid,
        name=request.name,
    )
    from webauthn.helpers import bytes_to_base64url

    return EnrollFinishResponse(
        credential_db_id=int(row["id"]),
        credential_id_b64u=bytes_to_base64url(verified.credential_id),
        sign_count=verified.sign_count,
    )


# --- Routes: authentication ----------------------------------------------


@router.post("/login/begin")
async def login_begin(
    request: LoginBeginRequest,
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    """Generate assertion options for a user with at least one credential.

    Anonymous endpoint: the caller is asserting an identity, not yet
    proving it. We still want to refuse fast on unknown usernames /
    no-credentials so the frontend can render an error rather than
    spinning up the browser ceremony.
    """
    user = await store.get_user(request.username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    creds = await store.list_webauthn_credentials_for_user(int(user["id"]))
    if not creds:
        raise HTTPException(status_code=404, detail="No passkeys enrolled for user")
    return passkey.begin_authentication(
        username=request.username,
        allow_credential_ids=[row["credential_id"] for row in creds],
    )


@router.post("/login/finish")
async def login_finish(
    request: LoginFinishRequest,
    response: Response,
    store: SqliteStore = Depends(get_store),
) -> LoginFinishResponse:
    """Verify the assertion, advance ``sign_count``, mint a JWT.

    ``sign_count`` is enforced by py_webauthn (strictly greater than the
    stored value); we re-raise any error as 400 so the client surfaces
    "this passkey was just used elsewhere — re-authenticate" rather than
    a generic 500.
    """
    user = await store.get_user(request.username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # H-1: refuse session minting when the user's persona is soft-disabled.
    assignment = await store.get_persona_assignment(request.username)
    if assignment is not None and assignment.get("disabled_at") is not None:
        raise HTTPException(status_code=403, detail="user is disabled")
    # Look up the credential the browser claims to have used. The
    # rawId in the credential dict is the same bytes we stored at
    # enroll time, so this is a single equality lookup.
    raw_id = request.credential.get("rawId") or request.credential.get("raw_id")
    if isinstance(raw_id, str):
        from webauthn.helpers import base64url_to_bytes

        cid_bytes = base64url_to_bytes(raw_id)
    elif isinstance(raw_id, bytes):
        cid_bytes = raw_id
    else:
        raise HTTPException(status_code=400, detail="credential rawId missing")
    cred_row = await store.get_webauthn_credential_by_id(cid_bytes)
    if cred_row is None or cred_row["user_id"] != int(user["id"]):
        raise HTTPException(status_code=404, detail="Credential not found for user")

    try:
        verified = passkey.finish_authentication(
            username=request.username,
            credential=request.credential,
            public_key=cred_row["public_key"],
            current_sign_count=int(cred_row["sign_count"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (
        InvalidRegistrationResponse,
        InvalidAuthenticationResponse,
        InvalidJSONStructure,
    ) as exc:
        # py_webauthn raises InvalidAuthenticationResponse for the
        # replay-attack / clone-detection path. Don't leak its message.
        logger.info("passkey login verification failed for %s: %s", request.username, exc)
        raise HTTPException(status_code=400, detail="passkey verification failed") from None

    await store.update_webauthn_sign_count(
        credential_id=verified.credential_id,
        new_sign_count=verified.new_sign_count,
        last_used_at=datetime.now(UTC).isoformat(),
    )
    # FO-1c: mint a session JWT (aud="session") and set it as the
    # cq_session cookie so the browser can navigate to authenticated
    # pages without JS passing the bearer. Token is also returned in
    # the body for backward compat with API clients reading
    # ``response.json()["token"]``.
    token = mint_session_cookie(response, username=request.username)
    return LoginFinishResponse(
        token=token,
        username=request.username,
        sign_count=verified.new_sign_count,
    )
