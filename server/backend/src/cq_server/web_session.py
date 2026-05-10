"""Cookie-bound web-session bearer (FO-1c, #191).

A small, deliberately-stateless layer on top of ``auth.create_token`` /
``auth.verify_token``. Two helpers:

* ``mint_session_cookie(response, *, username, ...)`` — sets a JWT
  (``aud="session"``) on the response as an ``HttpOnly; Secure;
  SameSite=Lax`` cookie named ``cq_session``. The JWT is the same shape
  ``/auth/login`` returns in the body, so existing bearer-token clients
  keep working unchanged.

* ``read_session_from_cookie(request) -> WebSession | None`` — reads
  the cookie and returns the verified claims. Strict ``aud="session"``
  validation; an invite token (``aud="invite"``) at this surface is
  rejected by the underlying ``verify_token``.

# Why a cookie

The bearer-token-in-localStorage shape forces every protected fetch to
attach an ``Authorization`` header from JS, which means the token has
to live somewhere the JS can read — i.e. localStorage, which is XSS-
exposed. The cookie variant moves the token out of JS reach
(``HttpOnly``) and lets the browser attach it automatically on
same-site navigations, which is what FO-1d's React shell wants.

# Why two paths instead of one merged dep

The existing ``get_current_user`` accepts both a JWT *and* an API key
(``cqa.v1.*``). API keys are agent identities — they aren't JWTs and
won't ever be cookie-bound. Folding cookie-reading into that dep would
either tangle the dispatch logic or weaken the validation. We keep
the two surfaces separate: cookie auth for browser sessions (this
module's ``get_current_user_from_session``), bearer auth for the
agent API (``auth.get_current_user``). They both terminate in the
same JWT verification but on different audiences if/when we tighten.

# Out of scope (V1)

* Persistent revocation table — the cookie's TTL is the only revoke
  mechanism. Server-side cookie-revoke (logout that survives across
  replicas) is a future epic; same caveat as FO-1a's per-process
  challenge cache.
* Cross-subdomain CSRF defense beyond ``SameSite=Lax``. The site is
  served from one origin in V1; multi-subdomain SSO is post-V1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request, Response

from .auth import (
    SESSION_AUDIENCE,
    SESSION_ISSUER,
    _get_jwt_secret,
    create_token,
    verify_token,
)
from .passkey import rp_id

# Cookie name. Stable across releases — flipping this would log every
# user out, and there's no migration value in renaming it.
COOKIE_NAME = "cq_session"

# TTL default in hours. Configurable via ``CQ_SESSION_TTL_HOURS``.
# 24h matches ``auth.create_token``'s default and the
# operator-mental-model "session lasts a workday" expectation.
DEFAULT_TTL_HOURS = 24


def _ttl_hours() -> int:
    raw = os.environ.get("CQ_SESSION_TTL_HOURS")
    if not raw:
        return DEFAULT_TTL_HOURS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_TTL_HOURS
    if parsed <= 0:
        return DEFAULT_TTL_HOURS
    return parsed


def _cookie_domain() -> str | None:
    """Return the ``Domain`` attribute for the session cookie.

    Derived from the WebAuthn ``rp_id`` so the cookie scope and the
    passkey scope move together — operators set one env
    (``CQ_WEBAUTHN_RP_ID``) and both the auth surfaces follow. Returns
    ``None`` when the rp_id is ``localhost`` (browsers reject explicit
    ``Domain=localhost``; they implicitly scope to the host).
    """
    host = rp_id()
    if host == "localhost":
        return None
    # Leading-dot Domain attribute scopes the cookie to the apex + all
    # subdomains. RFC 6265 says modern browsers ignore the leading dot
    # but it's the conventional spelling and matches what most server
    # frameworks emit. Without the dot it'd scope to *only* that host.
    return f".{host}" if not host.startswith(".") else host


def _cookie_secure() -> bool:
    """Return whether the cookie should set the ``Secure`` flag.

    On in non-dev (``CQ_ENV != "dev"``); off in dev so the local
    plaintext-http harness can still set + read the cookie. Mirrors
    ``passkey.rp_origin``'s https-only enforcement.
    """
    return os.environ.get("CQ_ENV", "dev").lower() != "dev"


@dataclass
class WebSession:
    """The verified payload behind a valid ``cq_session`` cookie.

    Mirrors the JWT claims we care about. ``aud`` and ``iss`` are
    pinned in the verifier; this struct is what callers actually use.
    """

    username: str
    issued_at: int
    expires_at: int
    raw: dict[str, Any]


def mint_session_cookie(
    response: Response,
    *,
    username: str,
    ttl_hours: int | None = None,
) -> str:
    """Mint a session JWT and attach it to ``response`` as a cookie.

    Returns the JWT bearer string for callers that also want to surface
    it in the response body (the existing ``LoginResponse`` /
    ``ClaimResponse`` shapes). Setting both the cookie *and* the body
    bearer keeps backward compat with API clients that read
    ``response.json()["token"]`` while letting browsers use the cookie.
    """
    ttl = ttl_hours if ttl_hours is not None else _ttl_hours()
    token = create_token(
        username,
        secret=_get_jwt_secret(),
        ttl_hours=ttl,
        aud=SESSION_AUDIENCE,
    )
    cookie_kwargs: dict[str, Any] = {
        "key": COOKIE_NAME,
        "value": token,
        "max_age": ttl * 3600,
        "httponly": True,
        "secure": _cookie_secure(),
        "samesite": "lax",
        "path": "/",
    }
    domain = _cookie_domain()
    if domain is not None:
        cookie_kwargs["domain"] = domain
    response.set_cookie(**cookie_kwargs)
    return token


def clear_session_cookie(response: Response) -> None:
    """Remove the ``cq_session`` cookie (logout).

    Setting an empty value with ``max_age=0`` is the standard recipe
    for browsers to forget the cookie. The Domain/Path attributes must
    match the ones used at mint time or the browser keeps the original.
    """
    cookie_kwargs: dict[str, Any] = {
        "key": COOKIE_NAME,
        "path": "/",
    }
    domain = _cookie_domain()
    if domain is not None:
        cookie_kwargs["domain"] = domain
    response.delete_cookie(**cookie_kwargs)


def read_session_from_cookie(request: Request) -> WebSession | None:
    """Decode the ``cq_session`` cookie. Returns ``None`` on any failure.

    No exceptions surface from this function — every failure mode
    (missing cookie, bad signature, expired, wrong aud) collapses to
    ``None`` so callers can produce a uniform 401 without leaking the
    failure reason in the response.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = verify_token(
            raw,
            secret=_get_jwt_secret(),
            expected_aud=SESSION_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    iat = payload.get("iat")
    exp = payload.get("exp")
    return WebSession(
        username=sub,
        issued_at=int(iat) if iat is not None else 0,
        expires_at=int(exp) if exp is not None else 0,
        raw=payload,
    )


async def get_current_user_from_session(request: Request) -> str:
    """FastAPI dependency — authenticate via the ``cq_session`` cookie.

    Strictly cookie-only; does NOT fall back to ``Authorization`` headers
    (that's ``auth.get_current_user``'s job for the agent api-key path).
    Keeping the two deps separate is deliberate — see this module's
    docstring for the rationale.

    Raises:
        HTTPException: 401 when the cookie is missing, expired, has the
            wrong audience (e.g. an invite token), or fails signature.
    """
    session = read_session_from_cookie(request)
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid session cookie",
        )
    return session.username


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_TTL_HOURS",
    "SESSION_AUDIENCE",
    "SESSION_ISSUER",
    "WebSession",
    "clear_session_cookie",
    "get_current_user_from_session",
    "mint_session_cookie",
    "read_session_from_cookie",
]
