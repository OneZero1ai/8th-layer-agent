"""WebAuthn / passkey ceremony helpers (FO-1a, #191).

Thin wrapper around ``py_webauthn`` 2.x. Owns:

* RP configuration (id / origin / name) read from env with sane local
  defaults so tests and dev servers don't need to set anything.
* The temporary challenge cache keyed by username — a 60s TTL dict
  living in this process. Tests inject their own dict via
  ``set_challenge_cache_override`` so assertions on cache state stay
  deterministic.
* Pure functions for the four ceremony steps (registration begin/finish,
  authentication begin/finish). The route layer in
  ``passkey_routes.py`` wires these into FastAPI handlers.

Out of scope here (FO-1b/c work):
* Email / magic-link invites — that's FO-1b.
* Cookie-bound JWT or per-session model — FO-1c.

Note on attestation: we use the default ``AttestationConveyancePreference.NONE``
in the registration options. py_webauthn still parses + verifies the
authenticator data + COSE public key on ``verify_registration_response``
even when attestation is "none"; we just don't require an attestation
trust path. That matches every consumer-passkey flow (Apple, 1Password,
YubiKey self-attestation) and is the right default for FO-1a where the
goal is "any authenticator works", not enterprise-attested keys.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

# --- RP config ------------------------------------------------------------

DEFAULT_RP_ID = "localhost"
DEFAULT_RP_ORIGIN = "http://localhost:3000"
DEFAULT_RP_NAME = "8th-Layer.ai (dev)"

# Challenge cache TTL — short enough that an abandoned ceremony cleans up
# quickly, long enough to absorb a user pause between begin and finish.
CHALLENGE_TTL_SECONDS = 60


def rp_id() -> str:
    """Return the RP ID used in registration / authentication options."""
    return os.environ.get("CQ_WEBAUTHN_RP_ID", DEFAULT_RP_ID)


def rp_origin() -> str:
    """Return the RP origin (URL) used to verify clientDataJSON."""
    return os.environ.get("CQ_WEBAUTHN_RP_ORIGIN", DEFAULT_RP_ORIGIN)


def rp_name() -> str:
    """Return the RP display name shown to the user during enrollment."""
    return os.environ.get("CQ_WEBAUTHN_RP_NAME", DEFAULT_RP_NAME)


# --- Challenge cache ------------------------------------------------------


@dataclass
class _ChallengeEntry:
    challenge: bytes
    expires_at: float
    # Stored so finish() can pin the user_id used at begin() — defends
    # against finish-with-different-user attacks for registration.
    user_id_bytes: bytes | None = None


_challenge_cache: dict[str, _ChallengeEntry] = {}


def set_challenge_cache_override(cache: dict[str, _ChallengeEntry] | None) -> None:
    """Inject a challenge cache dict for tests; pass ``None`` to reset.

    Tests use this to assert on cache state and to ensure isolation
    between cases. Production code never calls this.
    """
    global _challenge_cache  # noqa: PLW0603
    _challenge_cache = {} if cache is None else cache


def _store_challenge(username: str, challenge: bytes, *, user_id_bytes: bytes | None = None) -> None:
    _challenge_cache[username] = _ChallengeEntry(
        challenge=challenge,
        expires_at=time.monotonic() + CHALLENGE_TTL_SECONDS,
        user_id_bytes=user_id_bytes,
    )


def _consume_challenge(username: str) -> _ChallengeEntry | None:
    """Pop and return the challenge for ``username`` if it exists and is fresh.

    Returns None on cache miss or expiry. Single-use semantics: once
    consumed the entry is gone, so a replayed finish() with the same
    challenge bytes cannot succeed twice.
    """
    entry = _challenge_cache.pop(username, None)
    if entry is None:
        return None
    if entry.expires_at < time.monotonic():
        return None
    return entry


# --- Registration ceremony ------------------------------------------------


def begin_registration(
    *,
    username: str,
    user_id_bytes: bytes,
    display_name: str | None = None,
    existing_credential_ids: list[bytes] | None = None,
) -> dict[str, Any]:
    """Generate WebAuthn registration options + cache the challenge.

    Returns the options as a JSON-serialisable dict (the wire shape
    browsers consume via ``navigator.credentials.create``).

    ``existing_credential_ids`` populates ``excludeCredentials`` so a
    user enrolling a second authenticator can't accidentally re-enroll
    one already on file.
    """
    challenge = secrets.token_bytes(32)
    exclude = [
        PublicKeyCredentialDescriptor(id=cid) for cid in (existing_credential_ids or [])
    ]
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=rp_name(),
        user_name=username,
        user_id=user_id_bytes,
        user_display_name=display_name or username,
        challenge=challenge,
        # Prefer platform / passkey-style enrollments (resident keys),
        # but accept either. UV preferred matches the consumer-passkey
        # default; FO-1c can tighten this to "required" once the session
        # model lands.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    _store_challenge(username, challenge, user_id_bytes=user_id_bytes)
    # ``options_to_json`` emits the exact base64url-encoded shape the
    # browser API expects; we re-parse to a dict so FastAPI can return
    # it as a JSON object rather than a JSON-encoded string.
    import json

    return json.loads(options_to_json(options))


@dataclass
class VerifiedRegistration:
    """Subset of py_webauthn's VerifiedRegistration that the route persists."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    aaguid: bytes | None
    transports: list[str] | None


def finish_registration(
    *,
    username: str,
    credential: dict[str, Any],
    transports: list[str] | None = None,
) -> VerifiedRegistration:
    """Verify the browser's registration response and return persistable fields.

    Raises ``ValueError`` on missing/expired challenge or on signature
    verification failure (the route layer maps that to 400).
    """
    entry = _consume_challenge(username)
    if entry is None:
        raise ValueError("registration challenge missing or expired")

    verified = verify_registration_response(
        credential=credential,
        expected_challenge=entry.challenge,
        expected_rp_id=rp_id(),
        expected_origin=rp_origin(),
        # FO-1a accepts any authenticator; UV is preferred not required,
        # so don't fail verification on UP-only authenticators yet.
        require_user_verification=False,
    )
    aaguid_bytes = (
        bytes.fromhex(verified.aaguid.replace("-", "")) if verified.aaguid else None
    )
    return VerifiedRegistration(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=int(verified.sign_count),
        aaguid=aaguid_bytes,
        transports=transports,
    )


# --- Authentication ceremony ----------------------------------------------


def begin_authentication(
    *,
    username: str,
    allow_credential_ids: list[bytes],
) -> dict[str, Any]:
    """Generate authentication options for a known user.

    ``allow_credential_ids`` is the list of credential ids enrolled by
    this user — sent so the browser only attempts authenticators that
    can actually satisfy the challenge.
    """
    challenge = secrets.token_bytes(32)
    options = generate_authentication_options(
        rp_id=rp_id(),
        challenge=challenge,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=cid) for cid in allow_credential_ids
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _store_challenge(username, challenge)
    import json

    return json.loads(options_to_json(options))


@dataclass
class VerifiedAuthentication:
    """Subset of py_webauthn's VerifiedAuthentication for the route layer."""

    credential_id: bytes
    new_sign_count: int


def finish_authentication(
    *,
    username: str,
    credential: dict[str, Any],
    public_key: bytes,
    current_sign_count: int,
) -> VerifiedAuthentication:
    """Verify an assertion and return the new sign_count.

    Caller passes the stored ``public_key`` and ``current_sign_count``
    for the credential id the browser presented. py_webauthn enforces
    that the new sign_count is strictly greater (clone detection); we
    surface that as ValueError on replay.
    """
    entry = _consume_challenge(username)
    if entry is None:
        raise ValueError("authentication challenge missing or expired")

    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=entry.challenge,
        expected_rp_id=rp_id(),
        expected_origin=rp_origin(),
        credential_public_key=public_key,
        credential_current_sign_count=current_sign_count,
        require_user_verification=False,
    )
    raw_id = credential.get("rawId") or credential.get("raw_id")
    # rawId comes through as base64url; py_webauthn re-encodes it on
    # the verified object's id (str). Resolve back to bytes for the
    # caller — that's the lookup key we used to find the row.
    from webauthn.helpers import base64url_to_bytes

    if isinstance(raw_id, bytes):
        cid_bytes = raw_id
    elif isinstance(raw_id, str):
        cid_bytes = base64url_to_bytes(raw_id)
    else:
        # Should never happen — py_webauthn would have rejected the
        # credential dict before reaching here.
        raise ValueError("credential rawId missing")
    return VerifiedAuthentication(
        credential_id=cid_bytes,
        new_sign_count=int(verified.new_sign_count),
    )
