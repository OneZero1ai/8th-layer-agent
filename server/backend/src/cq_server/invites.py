"""Invite minting / validation / claim — FO-1b.

The bearer is a signed JWT (HS256, signed with ``CQ_JWT_SECRET`` —
the same secret as login JWTs). The token carries:

  {
    "sub": email,
    "role": role,
    "target_l2_id": ... | None,
    "iss": "8th-layer.ai",
    "aud": "invite",
    "iat": ...,
    "exp": ...,
    "jti": uuid4().hex
  }

``aud="invite"`` is the discriminant that prevents an invite token from
being accepted at the ``/auth/me`` (session-aud) gate. Login tokens use
``aud=self_l2_id()`` per ``auth.create_token``; invites use the
constant ``"invite"`` so the verifier rejects mismatches structurally.

Single-use enforcement layers: (a) ``UNIQUE(jti)`` index on
``invites.jti`` blocks duplicate inserts at mint; (b) the claim path
runs an atomic ``UPDATE invites SET claimed_at = ?, claimed_by = ?
WHERE id = ? AND claimed_at IS NULL AND revoked_at IS NULL`` and
treats ``rowcount == 0`` as "already-claimed/revoked → 409".

Note: ``aud="invite"`` is intentionally NOT one of the per-L2 ids the
login path uses. Cross-L2 invite verification is a non-goal for v1
(invites are minted on the same L2 they're redeemed on); the
``"invite"`` audience-as-purpose discriminant is the simplest way to
keep the two surfaces from leaking into each other.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from sqlalchemy import text

from .auth import _get_jwt_secret, hash_password
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

INVITE_ISSUER = "8th-layer.ai"
INVITE_AUDIENCE = "invite"
DEFAULT_TTL_HOURS = 72

InviteRole = Literal["enterprise_admin", "l2_admin", "user"]
InviteStatus = Literal["pending", "claimed", "expired", "revoked"]


def _ttl_hours() -> int:
    raw = os.environ.get("CQ_INVITE_TTL_HOURS")
    if not raw:
        return DEFAULT_TTL_HOURS
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid CQ_INVITE_TTL_HOURS=%r; falling back to default", raw)
        return DEFAULT_TTL_HOURS


@dataclass
class Invite:
    """In-memory representation of one row in ``invites``."""

    id: int
    jti: str
    email: str
    role: str
    target_l2_id: str | None
    issued_by: int
    issued_at: str
    expires_at: str
    claimed_at: str | None
    claimed_by: int | None
    revoked_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> Invite:
        """Build an ``Invite`` from a SQLAlchemy row tuple in ``_SELECT_COLUMNS`` order."""
        return cls(
            id=int(row[0]),
            jti=row[1],
            email=row[2],
            role=row[3],
            target_l2_id=row[4],
            issued_by=int(row[5]),
            issued_at=row[6],
            expires_at=row[7],
            claimed_at=row[8],
            claimed_by=int(row[9]) if row[9] is not None else None,
            revoked_at=row[10],
        )

    @property
    def status(self) -> InviteStatus:
        """Lifecycle classification — ``pending`` / ``claimed`` / ``expired`` / ``revoked``."""
        if self.revoked_at is not None:
            return "revoked"
        if self.claimed_at is not None:
            return "claimed"
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return "pending"
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return "expired"
        return "pending"


_SELECT_COLUMNS = (
    "id, jti, email, role, target_l2_id, issued_by, issued_at, "
    "expires_at, claimed_at, claimed_by, revoked_at"
)


def _encode_jwt(
    *,
    email: str,
    role: str,
    target_l2_id: str | None,
    issued_at: datetime,
    expires_at: datetime,
    jti: str,
) -> str:
    """Sign the invite JWT with the server's JWT secret."""
    payload: dict[str, Any] = {
        "sub": email,
        "role": role,
        "target_l2_id": target_l2_id,
        "iss": INVITE_ISSUER,
        "aud": INVITE_AUDIENCE,
        "iat": issued_at,
        "exp": expires_at,
        "jti": jti,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def _decode_jwt(token: str) -> dict[str, Any]:
    """Verify signature + expiry + iss/aud structure. Raises ``jwt.PyJWTError``."""
    return jwt.decode(
        token,
        _get_jwt_secret(),
        algorithms=["HS256"],
        audience=INVITE_AUDIENCE,
        issuer=INVITE_ISSUER,
        options={"require": ["iss", "aud", "sub", "exp", "jti"]},
    )


def mint_invite(
    store: SqliteStore,
    *,
    email: str,
    role: InviteRole,
    target_l2_id: str | None,
    issued_by: int,
    ttl_hours: int | None = None,
) -> tuple[Invite, str]:
    """Create an invite row + return (Invite, signed JWT bearer).

    The JWT is the only place the bearer ever exists in plaintext —
    callers must email it directly and never persist it. The DB only
    holds ``jti`` (for single-use enforcement) and lifecycle metadata.
    """
    if role == "enterprise_admin" and target_l2_id is not None:
        # An enterprise-admin invite is bound to the Enterprise, not a
        # specific L2 — defensive nudge so the API can't write a
        # contradictory row even if an admin builds the request wrong.
        target_l2_id = None
    if role != "enterprise_admin" and target_l2_id is None:
        raise ValueError("target_l2_id is required for non-enterprise_admin roles")

    now = datetime.now(UTC)
    ttl = ttl_hours if ttl_hours is not None else _ttl_hours()
    expires_at = now + timedelta(hours=ttl)
    jti = uuid.uuid4().hex
    token = _encode_jwt(
        email=email,
        role=role,
        target_l2_id=target_l2_id,
        issued_at=now,
        expires_at=expires_at,
        jti=jti,
    )

    issued_at_iso = now.isoformat()
    expires_at_iso = expires_at.isoformat()
    with store._engine.begin() as conn:  # noqa: SLF001
        result = conn.execute(
            text(
                "INSERT INTO invites "
                "(jti, email, role, target_l2_id, issued_by, issued_at, expires_at) "
                "VALUES (:jti, :email, :role, :target_l2_id, :issued_by, :issued_at, :expires_at)"
            ),
            {
                "jti": jti,
                "email": email,
                "role": role,
                "target_l2_id": target_l2_id,
                "issued_by": issued_by,
                "issued_at": issued_at_iso,
                "expires_at": expires_at_iso,
            },
        )
        invite_id = int(result.lastrowid or 0)

    invite = Invite(
        id=invite_id,
        jti=jti,
        email=email,
        role=role,
        target_l2_id=target_l2_id,
        issued_by=issued_by,
        issued_at=issued_at_iso,
        expires_at=expires_at_iso,
        claimed_at=None,
        claimed_by=None,
        revoked_at=None,
    )
    return invite, token


def validate_invite_jwt(token: str, store: SqliteStore) -> Invite | None:
    """Validate signature/expiry/iss/aud + DB lifecycle. Returns ``None`` on any failure.

    A return of ``None`` means "do not surface this invite to the
    caller" — callers map to 410/404 as appropriate. Distinguish
    expired vs revoked vs claimed via the row status when you need
    granular HTTP status codes.
    """
    try:
        payload = _decode_jwt(token)
    except jwt.PyJWTError:
        return None
    jti = payload.get("jti")
    if not jti:
        return None
    invite = _get_by_jti(store, jti)
    if invite is None:
        return None
    if invite.status != "pending":
        return None
    return invite


def _get_by_jti(store: SqliteStore, jti: str) -> Invite | None:
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM invites WHERE jti = :jti"),
            {"jti": jti},
        ).fetchone()
    if row is None:
        return None
    return Invite.from_row(row)


def _get_by_id(store: SqliteStore, invite_id: int) -> Invite | None:
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM invites WHERE id = :id"),
            {"id": invite_id},
        ).fetchone()
    if row is None:
        return None
    return Invite.from_row(row)


@dataclass
class ClaimOutcome:
    """Discriminated result for ``claim_invite``.

    Used by the route layer to map to the right HTTP status:
      * ``ok`` → 200, with ``invite`` populated
      * ``not_found`` → 404 (signature mismatch / unknown jti)
      * ``expired`` → 410
      * ``revoked`` → 410
      * ``already_claimed`` → 409
    """

    kind: Literal["ok", "not_found", "expired", "revoked", "already_claimed"]
    invite: Invite | None = None


def claim_invite(
    store: SqliteStore,
    *,
    token: str,
    claiming_user_id: int,
) -> ClaimOutcome:
    """Atomically mark an invite ``claimed_at = now`` if pending.

    The ``UPDATE … WHERE claimed_at IS NULL AND revoked_at IS NULL``
    pattern is the single-use guarantee: two concurrent claims race to
    the UPDATE; whichever lands first wins via row-lock; the second
    sees ``rowcount == 0`` and is rejected.
    """
    try:
        payload = _decode_jwt(token)
    except jwt.ExpiredSignatureError:
        return ClaimOutcome(kind="expired")
    except jwt.PyJWTError:
        return ClaimOutcome(kind="not_found")

    jti = payload.get("jti")
    if not jti:
        return ClaimOutcome(kind="not_found")

    invite = _get_by_jti(store, jti)
    if invite is None:
        return ClaimOutcome(kind="not_found")

    # Status checks BEFORE the atomic UPDATE so we can return the right
    # HTTP code; the UPDATE is still the authority.
    status_at_read = invite.status
    if status_at_read == "claimed":
        return ClaimOutcome(kind="already_claimed", invite=invite)
    if status_at_read == "revoked":
        return ClaimOutcome(kind="revoked", invite=invite)
    if status_at_read == "expired":
        return ClaimOutcome(kind="expired", invite=invite)

    now_iso = datetime.now(UTC).isoformat()
    with store._engine.begin() as conn:  # noqa: SLF001
        result = conn.execute(
            text(
                "UPDATE invites SET claimed_at = :now, claimed_by = :user_id "
                "WHERE id = :id AND claimed_at IS NULL AND revoked_at IS NULL"
            ),
            {"now": now_iso, "user_id": claiming_user_id, "id": invite.id},
        )
        if (result.rowcount or 0) == 0:
            # Lost the race — re-read to surface the now-current state.
            current = _get_by_id(store, invite.id)
            if current is None:
                return ClaimOutcome(kind="not_found")
            if current.revoked_at is not None:
                return ClaimOutcome(kind="revoked", invite=current)
            if current.claimed_at is not None:
                return ClaimOutcome(kind="already_claimed", invite=current)
            return ClaimOutcome(kind="not_found", invite=current)

    refreshed = _get_by_id(store, invite.id)
    return ClaimOutcome(kind="ok", invite=refreshed or invite)


def list_invites(
    store: SqliteStore,
    *,
    status: InviteStatus | None = None,
) -> list[Invite]:
    """Return invites filtered by lifecycle status (None → all)."""
    with store._engine.connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM invites ORDER BY issued_at DESC"
            )
        ).fetchall()
    invites = [Invite.from_row(r) for r in rows]
    if status is None:
        return invites
    return [inv for inv in invites if inv.status == status]


def revoke_invite(
    store: SqliteStore,
    *,
    invite_id: int,
    by_user_id: int,
) -> Invite | None:
    """Mark the invite ``revoked_at = now``. Returns the updated row.

    Returns ``None`` when the row doesn't exist. Idempotent — repeated
    revokes leave ``revoked_at`` at the first revoke time so the audit
    record points at the original action.
    """
    # ``by_user_id`` is captured for activity-log audit trails — kept
    # in the signature even though the column isn't denormalised on the
    # invites row (the activity_log table is the source of truth for
    # who did what).
    del by_user_id

    invite = _get_by_id(store, invite_id)
    if invite is None:
        return None
    if invite.revoked_at is not None:
        return invite

    now_iso = datetime.now(UTC).isoformat()
    with store._engine.begin() as conn:  # noqa: SLF001
        conn.execute(
            text(
                "UPDATE invites SET revoked_at = :now WHERE id = :id AND revoked_at IS NULL"
            ),
            {"now": now_iso, "id": invite_id},
        )
    return _get_by_id(store, invite_id)


# ---------------------------------------------------------------------------
# User creation helper for the claim path.
# ---------------------------------------------------------------------------


async def ensure_user(
    store: SqliteStore,
    *,
    username: str,
    password: str,
    email: str,
) -> int:
    """Create-or-fetch the user record for an invite-claimer.

    If a user with this ``username`` already exists, the existing row's
    id is returned (no password rotation, no email mutation — that's
    out of scope for FO-1b). Otherwise we hash the password and insert,
    then return the new user's id.

    The ``email`` argument is currently unused by the create path
    (FO-1a's ``users.email`` column is additive but ``create_user``
    hasn't been extended to write it yet); the parameter is in the
    signature so FO-1c can wire it without touching every caller.
    """
    del email

    existing = await store.get_user(username)
    if existing is not None:
        return int(existing["id"])
    await store.create_user(username, hash_password(password))
    fresh = await store.get_user(username)
    if fresh is None:  # pragma: no cover — race that breaks the world
        raise RuntimeError("user creation succeeded but lookup returned None")
    return int(fresh["id"])
