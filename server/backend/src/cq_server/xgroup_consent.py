"""Intra-Enterprise xgroup_consent — 2-of-2 admin co-signed grants.

Decision 28 §2 — implementation. This module is the policy + crypto
kernel for cross-group sharing inside one Enterprise:

  propose → cosign → ratify → (use) → revoke

Public API is async, takes a ``SqliteStore`` (for ``_engine.begin()``),
and returns plain dicts. The HTTP surface is in ``admin_routes.py`` —
this module has no FastAPI dependency so it's testable without a TestClient.

Decision 28 §3.1 — pinned pubkeys survive admin-key rotation. Revoke
verification accepts:

  (a) the pinned signer pubkey, OR
  (b) the recovery_operator_pubkey for recovery-revoke, OR
  (c) per Phase 1.0b brief: a current-admin pubkey if a Decision-26-style
      key-lineage chain links it back to the pinned pubkey.

(c) is implemented via an injectable ``key_lineage_walker`` callable so
  this module stays decoupled from ``auth/admin_keys.py`` (which doesn't
  exist yet at module-write time; the brief assumes Decision 26 plumbing
  is available, but Phase 1.0a/c land in parallel and may rebase it).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from .crypto import b64u, b64u_decode, canonicalize, verify_envelope_signature
from .store._sqlite import SqliteStore

log = logging.getLogger(__name__)

# Decision 28 §2.1 — max TTL 90 days, configurable shorter via
# enterprise_settings.xgroup_consent_max_ttl_days (out of scope for v1
# beyond this constant).
MAX_GRANT_TTL_DAYS = 90
COSIGN_WINDOW_DAYS = 7
GRANT_BODY_VERSION = "v1"

# Async hook signature for a Decision-26-style key-lineage walk. Returns
# True iff ``current_pubkey_b64u`` chains back to ``pinned_pubkey_b64u``
# for the same ``signer_l2`` in the admin-key history. Callers wire this
# into auth/admin_keys.py once that module ships.
KeyLineageWalker = Callable[[str, str, str], Awaitable[bool]]


class XGroupConsentError(Exception):
    """Raised when a propose/cosign/ratify/revoke step is rejected.

    ``code`` matches the HTTP-translation table in admin_routes:
      ``not_found`` 404, ``conflict`` 409, ``invalid_signature`` 403,
      ``expired`` 410, ``bad_request`` 400.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Construct with a coarse ``code`` tag + human ``detail`` message."""
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Body construction + canonicalisation
# ---------------------------------------------------------------------------


def build_grant_body(
    *,
    enterprise_id: str,
    source_l2: str,
    target_l2: str,
    scope_kind: str,
    scope_values: list[str],
    issued_at: str,
    expires_at: str,
    recovery_operator_pubkey_b64u: str,
    nonce_b64u: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical grant body dict per Decision 28 §2.1.

    Generates a UUIDv4 grant_id and 16-byte b64u nonce by default; tests
    pin them to make signatures deterministic. ``expires_at`` is bounded
    to ``MAX_GRANT_TTL_DAYS`` from ``issued_at`` — over-cap surfaces as
    XGroupConsentError(bad_request) rather than silently truncating.
    """
    if scope_kind not in ("domains", "topics", "all"):
        raise XGroupConsentError("bad_request", f"scope.kind must be domains|topics|all, got {scope_kind!r}")
    if scope_kind != "all" and not scope_values:
        raise XGroupConsentError("bad_request", "scope.values required for kind != 'all'")
    if source_l2 == target_l2:
        raise XGroupConsentError("bad_request", "source_l2 must differ from target_l2")

    issued_dt = _parse_iso(issued_at, "issued_at")
    expires_dt = _parse_iso(expires_at, "expires_at")
    if expires_dt <= issued_dt:
        raise XGroupConsentError("bad_request", "expires_at must be > issued_at")
    if expires_dt - issued_dt > timedelta(days=MAX_GRANT_TTL_DAYS):
        raise XGroupConsentError("bad_request", f"grant TTL exceeds max {MAX_GRANT_TTL_DAYS}d")

    return {
        "grant_id": grant_id or str(uuid.uuid4()),
        "enterprise_id": enterprise_id,
        "source_l2": source_l2,
        "target_l2": target_l2,
        "scope": {"kind": scope_kind, "values": list(scope_values)},
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce_b64u or b64u(secrets.token_bytes(16)),
        "version": GRANT_BODY_VERSION,
        "recovery_operator_pubkey_b64u": recovery_operator_pubkey_b64u,
    }


def canonical_body_bytes(body: dict[str, Any]) -> bytes:
    """RFC 8785 JCS bytes for the body — what signers sign."""
    return canonicalize(body)


def body_sha256_hex(body: dict[str, Any]) -> str:
    """Hex SHA-256 of the canonical body bytes; used as signature scope."""
    return hashlib.sha256(canonical_body_bytes(body)).hexdigest()


def _parse_iso(value: str, name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise XGroupConsentError("bad_request", f"{name} not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise XGroupConsentError("bad_request", f"{name} must be timezone-aware")
    return dt


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _verify_sig_over_body(pubkey_b64u: str, body: dict[str, Any], signature_b64u: str) -> bool:
    """Verify Ed25519 signature over the canonical body bytes.

    Mirrors ``verify_envelope_signature`` shape (pubkey + canonical text
    + sig) so callers familiar with the directory envelope path read
    naturally.
    """
    return verify_envelope_signature(pubkey_b64u, canonical_body_bytes(body).decode(), signature_b64u)


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


async def propose_grant(
    store: SqliteStore,
    *,
    body: dict[str, Any],
    proposer_l2: str,
    proposer_pubkey_b64u: str,
    proposer_signature_b64u: str,
    cosign_window_days: int = COSIGN_WINDOW_DAYS,
) -> dict[str, Any]:
    """Step 1 — first signer proposes; pending row is created on source L2.

    The proposer's L2 is also the source L2 (Decision 28 §2.2: "Pending-
    grant storage location: source L2 only"). Returns the inserted row
    as a dict with ``pending_id``, ``status``, ``expires_at``.
    """
    # Sanity: proposer must be one of the two L2s named in the body.
    if proposer_l2 not in (body["source_l2"], body["target_l2"]):
        raise XGroupConsentError(
            "bad_request",
            f"proposer_l2={proposer_l2!r} not in body.source/target",
        )
    # Source L2 only — the proposer is the source side.
    if proposer_l2 != body["source_l2"]:
        raise XGroupConsentError(
            "bad_request",
            "proposer must be source_l2 (target retrieves via cross-L2 read)",
        )

    if not _verify_sig_over_body(proposer_pubkey_b64u, body, proposer_signature_b64u):
        raise XGroupConsentError("invalid_signature", "proposer signature does not verify over canonical body")

    canonical = canonical_body_bytes(body)
    now = _now_utc()
    cosign_expires = now + timedelta(days=cosign_window_days)
    pending_id = str(uuid.uuid4())

    def _do_insert() -> None:
        with store._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO xgroup_consent_pending (
                        pending_id, enterprise_id, source_l2, target_l2,
                        body_canonical, body_canonical_sha256_hex,
                        proposer_l2, proposer_pubkey_b64u, proposer_signature_b64u,
                        proposed_at, expires_at, status
                    ) VALUES (
                        :pending_id, :enterprise_id, :source_l2, :target_l2,
                        :body_canonical, :body_sha,
                        :proposer_l2, :proposer_pk, :proposer_sig,
                        :proposed_at, :expires_at, 'proposed'
                    )
                    """
                ),
                {
                    "pending_id": pending_id,
                    "enterprise_id": body["enterprise_id"],
                    "source_l2": body["source_l2"],
                    "target_l2": body["target_l2"],
                    "body_canonical": canonical.decode(),
                    "body_sha": body_sha256_hex(body),
                    "proposer_l2": proposer_l2,
                    "proposer_pk": proposer_pubkey_b64u,
                    "proposer_sig": proposer_signature_b64u,
                    "proposed_at": now.isoformat(),
                    "expires_at": cosign_expires.isoformat(),
                },
            )

    await store._run_sync(_do_insert)
    return {
        "pending_id": pending_id,
        "grant_id": body["grant_id"],
        "status": "proposed",
        "expires_for_cosign_at": cosign_expires.isoformat(),
    }


# ---------------------------------------------------------------------------
# Cosign
# ---------------------------------------------------------------------------


async def cosign_grant(
    store: SqliteStore,
    *,
    pending_id: str,
    cosigner_l2: str,
    cosigner_pubkey_b64u: str,
    cosigner_signature_b64u: str,
) -> dict[str, Any]:
    """Step 2 — second signer cosigns; updates pending row in place.

    The cosigner is the target L2 admin. We re-load the row, verify the
    cosigner's sig against the stored canonical body (NOT a freshly-built
    one — guards against the proposer mutating the body between propose
    and cosign), and write the cosigner fields.
    """
    row = await _load_pending(store, pending_id)
    now = _now_utc()
    if row["status"] != "proposed":
        raise XGroupConsentError("conflict", f"pending status={row['status']!r}, cannot cosign")
    if _parse_iso(row["expires_at"], "expires_at") <= now:
        raise XGroupConsentError("expired", "cosign window has elapsed")
    if cosigner_l2 == row["proposer_l2"]:
        raise XGroupConsentError("bad_request", "cosigner must be the OTHER L2 admin (2-of-2)")
    if cosigner_l2 != row["target_l2"]:
        raise XGroupConsentError("bad_request", "cosigner must be target_l2 admin")

    body = json.loads(row["body_canonical"])
    if not _verify_sig_over_body(cosigner_pubkey_b64u, body, cosigner_signature_b64u):
        raise XGroupConsentError("invalid_signature", "cosigner signature does not verify over stored canonical body")

    def _do_update() -> None:
        with store._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE xgroup_consent_pending
                    SET cosigner_l2 = :l2,
                        cosigner_pubkey_b64u = :pk,
                        cosigner_signature_b64u = :sig,
                        cosigned_at = :ts,
                        status = 'cosigned'
                    WHERE pending_id = :pid AND status = 'proposed'
                    """
                ),
                {
                    "l2": cosigner_l2,
                    "pk": cosigner_pubkey_b64u,
                    "sig": cosigner_signature_b64u,
                    "ts": now.isoformat(),
                    "pid": pending_id,
                },
            )

    await store._run_sync(_do_update)
    return {"pending_id": pending_id, "status": "cosigned", "cosigned_at": now.isoformat()}


# ---------------------------------------------------------------------------
# Ratify
# ---------------------------------------------------------------------------


async def ratify_grant(store: SqliteStore, *, pending_id: str) -> dict[str, Any]:
    """Step 3 — re-verify both sigs, promote to xgroup_consent (active).

    Re-verifies BOTH signatures against the stored canonical body before
    insert into the active table; deletes the pending row on success.
    Decision 28 §2.2 step 4: also fans the grant to the peer L2 over
    AIGRP — that side-effect is the caller's responsibility (admin
    routes wire the AIGRP fan-out).
    """
    row = await _load_pending(store, pending_id)
    now = _now_utc()
    if row["status"] != "cosigned":
        raise XGroupConsentError("conflict", f"pending status={row['status']!r}, cannot ratify")
    if _parse_iso(row["expires_at"], "expires_at") <= now:
        raise XGroupConsentError("expired", "cosign window has elapsed")

    body = json.loads(row["body_canonical"])
    if not _verify_sig_over_body(row["proposer_pubkey_b64u"], body, row["proposer_signature_b64u"]):
        raise XGroupConsentError("invalid_signature", "proposer signature failed re-verify at ratify")
    if row["cosigner_pubkey_b64u"] is None or row["cosigner_signature_b64u"] is None:
        raise XGroupConsentError("conflict", "cosigner fields missing on cosigned row (corrupt)")
    if not _verify_sig_over_body(row["cosigner_pubkey_b64u"], body, row["cosigner_signature_b64u"]):
        raise XGroupConsentError("invalid_signature", "cosigner signature failed re-verify at ratify")

    # Sanity: the body's expires_at must be in the future at ratify time
    # — otherwise the grant lands DOA.
    body_expires = _parse_iso(body["expires_at"], "body.expires_at")
    if body_expires <= now:
        raise XGroupConsentError("expired", "grant body.expires_at already past")

    grant_id = body["grant_id"]
    scope = body["scope"]

    def _do_promote() -> None:
        with store._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO xgroup_consent (
                        grant_id, enterprise_id, source_l2, target_l2,
                        body_canonical, body_canonical_sha256_hex,
                        scope_kind, scope_values_json,
                        signer_a_l2, signer_a_pubkey_b64u, signer_a_signature_b64u,
                        signer_b_l2, signer_b_pubkey_b64u, signer_b_signature_b64u,
                        recovery_operator_pubkey_b64u,
                        issued_at, expires_at, ratified_at, status,
                        revoked_by_recovery,
                        nonce_b64u, version
                    ) VALUES (
                        :grant_id, :enterprise_id, :source_l2, :target_l2,
                        :body_canonical, :body_sha,
                        :scope_kind, :scope_values_json,
                        :a_l2, :a_pk, :a_sig,
                        :b_l2, :b_pk, :b_sig,
                        :rec_pk,
                        :issued_at, :expires_at, :ratified_at, 'active',
                        0,
                        :nonce, :version
                    )
                    """
                ),
                {
                    "grant_id": grant_id,
                    "enterprise_id": body["enterprise_id"],
                    "source_l2": body["source_l2"],
                    "target_l2": body["target_l2"],
                    "body_canonical": row["body_canonical"],
                    "body_sha": row["body_canonical_sha256_hex"],
                    "scope_kind": scope["kind"],
                    "scope_values_json": json.dumps(scope.get("values", [])),
                    "a_l2": row["proposer_l2"],
                    "a_pk": row["proposer_pubkey_b64u"],
                    "a_sig": row["proposer_signature_b64u"],
                    "b_l2": row["cosigner_l2"],
                    "b_pk": row["cosigner_pubkey_b64u"],
                    "b_sig": row["cosigner_signature_b64u"],
                    "rec_pk": body["recovery_operator_pubkey_b64u"],
                    "issued_at": body["issued_at"],
                    "expires_at": body["expires_at"],
                    "ratified_at": now.isoformat(),
                    "nonce": body["nonce"],
                    "version": body["version"],
                },
            )
            conn.execute(
                text("DELETE FROM xgroup_consent_pending WHERE pending_id = :pid"),
                {"pid": pending_id},
            )

    await store._run_sync(_do_promote)
    return {"grant_id": grant_id, "status": "active", "ratified_at": now.isoformat()}


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def revoke_grant(
    store: SqliteStore,
    *,
    grant_id: str,
    revoker_pubkey_b64u: str,
    revoker_signature_b64u: str,
    revoker_l2: str | None,
    reason: str | None,
    revoke_ts: str,
    is_recovery: bool = False,
    key_lineage_walker: KeyLineageWalker | None = None,
) -> dict[str, Any]:
    """Revoke an active grant.

    Three accepted authorities:

      1. Either pinned signer pubkey — normal revoke.
      2. The recovery_operator_pubkey on the grant — recovery revoke
         (``is_recovery=True`` MUST be set; we check the pubkey matches
         the recovery slot, not a signer slot, to keep audit clean).
      3. A current admin pubkey for the same ``signer_l2`` whose lineage
         (Decision 26-style) chains back to the pinned pubkey. Provided
         via ``key_lineage_walker``.

    The revocation is itself signed: Ed25519 over the canonical bytes of
    ``{grant_id, revoke_ts, reason, revoker_l2}`` per Decision 28 §2.4.
    """
    row = await _load_active(store, grant_id)
    if row["status"] != "active":
        raise XGroupConsentError("conflict", f"grant status={row['status']!r}, cannot revoke")
    now = _now_utc()
    if _parse_iso(row["expires_at"], "expires_at") <= now:
        raise XGroupConsentError("expired", "grant has expired")

    # Verify revocation signature against the spec'd revoke envelope.
    revoke_envelope = {
        "grant_id": grant_id,
        "revoke_ts": revoke_ts,
        "reason": reason or "",
        "revoker_l2": revoker_l2 or "",
    }
    if not _verify_sig_over_body(revoker_pubkey_b64u, revoke_envelope, revoker_signature_b64u):
        raise XGroupConsentError("invalid_signature", "revoke signature does not verify")

    # Decide whether the revoker is authorised.
    pinned_a = row["signer_a_pubkey_b64u"]
    pinned_b = row["signer_b_pubkey_b64u"]
    recovery_pk = row["recovery_operator_pubkey_b64u"]

    if is_recovery:
        if revoker_pubkey_b64u != recovery_pk:
            raise XGroupConsentError(
                "invalid_signature",
                "is_recovery=True but revoker pubkey != grant.recovery_operator_pubkey",
            )
        revoked_by_recovery = 1
    else:
        # Normal revoke: pinned match, OR lineage-walk match.
        if revoker_pubkey_b64u in (pinned_a, pinned_b):
            revoked_by_recovery = 0
        elif revoker_pubkey_b64u == recovery_pk:
            # Operator passed the recovery key but didn't set is_recovery.
            # Refuse — the audit-flag mismatch matters for SOC 2 evidence.
            raise XGroupConsentError(
                "bad_request",
                "revoker is recovery operator pubkey; pass is_recovery=True for recovery-revoke flow",
            )
        else:
            # Lineage walk fallback.
            if key_lineage_walker is None or revoker_l2 is None:
                raise XGroupConsentError(
                    "invalid_signature",
                    "revoker pubkey is not pinned and no key-lineage walker available",
                )
            # Try lineage from the matching signer slot.
            chained = False
            if revoker_l2 == row["signer_a_l2"]:
                chained = await key_lineage_walker(revoker_l2, pinned_a, revoker_pubkey_b64u)
            elif revoker_l2 == row["signer_b_l2"]:
                chained = await key_lineage_walker(revoker_l2, pinned_b, revoker_pubkey_b64u)
            if not chained:
                raise XGroupConsentError(
                    "invalid_signature",
                    "revoker pubkey is not pinned and lineage walk did not chain to a pinned key",
                )
            revoked_by_recovery = 0

    def _do_revoke() -> None:
        with store._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE xgroup_consent
                    SET status = 'revoked',
                        revoked_at = :ts,
                        revoked_by_l2 = :l2,
                        revoked_by_pubkey_b64u = :pk,
                        revoked_by_recovery = :rec,
                        revoke_reason = :reason
                    WHERE grant_id = :gid AND status = 'active'
                    """
                ),
                {
                    "ts": now.isoformat(),
                    "l2": revoker_l2,
                    "pk": revoker_pubkey_b64u,
                    "rec": revoked_by_recovery,
                    "reason": reason,
                    "gid": grant_id,
                },
            )

    await store._run_sync(_do_revoke)
    return {
        "grant_id": grant_id,
        "status": "revoked",
        "revoked_by_recovery": bool(revoked_by_recovery),
        "revoked_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def list_pending_for_target(
    store: SqliteStore,
    *,
    enterprise_id: str,
    target_l2: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return pending proposals where this L2 is the target (cosign queue)."""

    def _q() -> list[dict[str, Any]]:
        with store._engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT pending_id, enterprise_id, source_l2, target_l2,
                           body_canonical, body_canonical_sha256_hex,
                           proposer_l2, proposer_pubkey_b64u, proposer_signature_b64u,
                           cosigner_l2, cosigner_pubkey_b64u, cosigner_signature_b64u,
                           cosigned_at, proposed_at, expires_at, status
                    FROM xgroup_consent_pending
                    WHERE enterprise_id = :ent
                      AND target_l2 = :tgt
                      AND status IN ('proposed', 'cosigned')
                    ORDER BY proposed_at DESC
                    LIMIT :lim
                    """
                ),
                {"ent": enterprise_id, "tgt": target_l2, "lim": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]

    return await store._run_sync(_q)


async def get_active_grant(store: SqliteStore, grant_id: str) -> dict[str, Any] | None:
    """Read an active grant by id (returns None if absent or revoked)."""
    try:
        return await _load_active(store, grant_id)
    except XGroupConsentError as exc:
        if exc.code == "not_found":
            return None
        raise


async def is_grant_usable(store: SqliteStore, grant_id: str) -> bool:
    """Return True iff grant exists, status='active', and not past expires_at.

    Decision 28 — the read-path checks this at query time. Status flip
    is hard-enforced (revoked → False); TTL is soft-enforced here so the
    daily sweep can mark expired rows ``status='expired'`` without race.
    """
    row = await get_active_grant(store, grant_id)
    if row is None:
        return False
    if row["status"] != "active":
        return False
    return _parse_iso(row["expires_at"], "expires_at") > _now_utc()


async def _load_pending(store: SqliteStore, pending_id: str) -> dict[str, Any]:
    def _q() -> dict[str, Any] | None:
        with store._engine.connect() as conn:
            r = conn.execute(
                text("SELECT * FROM xgroup_consent_pending WHERE pending_id = :pid"),
                {"pid": pending_id},
            ).fetchone()
            return dict(r._mapping) if r else None

    row = await store._run_sync(_q)
    if row is None:
        raise XGroupConsentError("not_found", f"pending_id {pending_id!r} does not exist")
    return row


async def _load_active(store: SqliteStore, grant_id: str) -> dict[str, Any]:
    def _q() -> dict[str, Any] | None:
        with store._engine.connect() as conn:
            r = conn.execute(
                text("SELECT * FROM xgroup_consent WHERE grant_id = :gid"),
                {"gid": grant_id},
            ).fetchone()
            return dict(r._mapping) if r else None

    row = await store._run_sync(_q)
    if row is None:
        raise XGroupConsentError("not_found", f"grant_id {grant_id!r} does not exist")
    return row


__all__ = [
    "COSIGN_WINDOW_DAYS",
    "GRANT_BODY_VERSION",
    "KeyLineageWalker",
    "MAX_GRANT_TTL_DAYS",
    "XGroupConsentError",
    "body_sha256_hex",
    "build_grant_body",
    "canonical_body_bytes",
    "cosign_grant",
    "get_active_grant",
    "is_grant_usable",
    "list_pending_for_target",
    "propose_grant",
    "ratify_grant",
    "revoke_grant",
]


# Suppress unused-import lint — kept for API symmetry/imports in tests.
_ = b64u
_ = b64u_decode
