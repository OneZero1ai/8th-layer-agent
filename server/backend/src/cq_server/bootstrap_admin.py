"""First-admin bootstrap for fresh-from-marketplace L2s (P2.5, task #218).

When a marketplace L2 first boots there are zero users. The signup
wizard collected ``admin_email`` from the founder, the provisioning
service passes it through as ``AdminEmail`` → CFN parameter →
``CQ_INITIAL_ADMIN_EMAIL`` env on this task. On the very first start
(users table empty) we mint a single magic-link invite for that
address with role=enterprise_admin, surfacing the bearer token in the
CloudWatch logs so the operator can hand it to the founder.

Why not SES-send from here:
  * SES today is sandbox-mode in 8th-layer-app (#208). The L2 lives
    in the customer's AWS account, which has its own SES posture we
    don't control. SES from inside the L2 isn't viable for V1.
  * The wizard already has admin_email + the magic link is plain
    JSON in CloudWatch — the operator-side workflow can grab it and
    deliver via whatever channel they like for the first admin.

The flow this enables (P2 demo path):
  1. Founder fills wizard, hits Provision.
  2. Provisioning service stands up the L2.
  3. THIS HOOK on first boot: creates 'system' user (no password / no
     passkey — never logs in), mints invite for AdminEmail, and logs ONLY
     that an invite was minted (invite_id + expiry) — NOT the token
     (security audit 2026-06-01: the magic-link embeds a founder-identity
     bearer; CloudWatch read is too broad an audience for it).
  4. The link is delivered to the founder by email (provisioning phase 5,
     SES) and surfaced on the wizard "you're done" screen (job result
     magic_link_url). It is no longer recoverable from CloudWatch.
  5. Founder clicks → /invite/<token> → registers passkey → lands
     in admin UI → goes to Invites tab → sends teammate #2 invite.

Idempotency: skipped when users table has any row OR
``CQ_INITIAL_ADMIN_EMAIL`` is unset. Safe to re-run on every boot
because it short-circuits before any state change.
"""

from __future__ import annotations

import logging
import os

from .invites import mint_invite
from .store._sqlite import SqliteStore

log = logging.getLogger("bootstrap_admin")

_SYSTEM_USERNAME = "_bootstrap_system"
# Bcrypt-style disabled marker: not a valid hash, so no password can
# ever match. We never log this user in — the row exists only so the
# invites.issued_by FK has a target.
_DISABLED_PASSWORD = "!disabled-bootstrap-row!"

_INVITE_TTL_HOURS = 24 * 7  # 7 days for the first admin; longer than
# the default 24h because the operator may need a window to hand the
# URL off, and the bearer is single-use anyway.

# Decision 42 / W2 — the dedicated agent user that owns the Liaison Server's
# service key. Like _bootstrap_system it is a marker row, excluded from
# _users_exist so it can never make the founder/password admin bootstrap think
# real users already exist (regardless of lifespan call order).
_LIAISON_USERNAME = "_liaison_service"
_DEFAULT_LIAISON_TTL = "365d"  # the system's MAX_TTL (ttl.parse_ttl caps at 365d).
# The Quick sidecar key therefore needs annual rotation — the provisioning
# worker re-seeds it on redeploy, and an expiry monitor is an M3 follow-up.


async def bootstrap_first_admin_if_needed(store: SqliteStore) -> None:
    """If no users exist and env is set, mint the founder's invite.

    Logs the magic-link URL at WARNING so it surfaces in CloudWatch
    log groups operators are already watching. Never raises — boot
    must not fail because the founder forgot to set the env.
    """
    admin_email = os.environ.get("CQ_INITIAL_ADMIN_EMAIL", "").strip()
    if not admin_email:
        return

    try:
        if await _users_exist(store):
            log.debug("bootstrap_first_admin: skipped — users already present")
            return
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_first_admin: user-count probe failed; skipping")
        return

    try:
        await store.create_user(
            username=_SYSTEM_USERNAME,
            password_hash=_DISABLED_PASSWORD,
        )
        system_user = await store.get_user(_SYSTEM_USERNAME)
        if system_user is None:
            log.error("bootstrap_first_admin: system user create returned None")
            return
        system_user_id = int(system_user["id"])
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_first_admin: system user creation failed; skipping")
        return

    try:
        invite, _token = mint_invite(  # _token: link delivered via email/wizard, never logged
            store,
            email=admin_email,
            role="enterprise_admin",
            target_l2_id=None,
            issued_by=system_user_id,
            ttl_hours=_INVITE_TTL_HOURS,
        )
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_first_admin: mint_invite failed for %s", admin_email)
        return

    # SECURITY (audit 2026-06-01): do NOT log the full magic-link — its URL embeds
    # a single-use bearer token that grants the founder identity for the invite
    # TTL (7d), and CloudWatch read is a much broader audience than the founder.
    # The link is now delivered to the founder via email (provisioning phase 5,
    # SES) and surfaced on the wizard done-screen (job result magic_link_url), so
    # the CloudWatch copy was pure exposure with no remaining delivery role. We
    # log only that an invite was minted (greppable sentinel + invite_id), never
    # the token.
    log.warning(
        "[BOOTSTRAP_ADMIN] First-admin invite minted for %s (invite_id=%d, expires=%s) — "
        "magic-link delivered via email + wizard done-screen; token NOT logged.",
        admin_email,
        invite.id,
        invite.expires_at,
    )


_DEFAULT_ADMIN_USERNAME = "admin"


async def bootstrap_password_admin_if_needed(store: SqliteStore) -> None:
    """Seed a password-login admin on first boot from an SSM-backed env.

    The operator onboarding path (agent#165): an operator sets an SSM
    parameter holding the initial admin password *before* deploying the
    L2 stack; the task definition mounts it as ``CQ_INITIAL_ADMIN_PASSWORD``
    via the ECS ``secrets:`` integration. On the very first boot — users
    table empty — this seeds a real ``admin`` user with role=admin so the
    operator can log in, mint an agent API key, and plant it. This
    replaces the manual ``aws ecs execute-command`` + ``seed-users.py``
    dance that blocked smoke-check #5 during the TeamDW standup.

    Alternative to :func:`bootstrap_first_admin_if_needed` (the founder
    path, ``CQ_INITIAL_ADMIN_EMAIL`` → magic-link invite → passkey) — an
    L2 should set one or the other. If both are set the email path wins
    (it runs first; this function detects its ``_bootstrap_system``
    marker row and defers), so an L2 never gets two admin principals.

    Idempotency: skipped when ``CQ_INITIAL_ADMIN_PASSWORD`` is unset,
    when any real (non-system) user already exists, OR when the email
    path's ``_bootstrap_system`` row is present. Once a real admin
    exists — including one rotated by a later manual edit — re-deploying
    with the env still set is a no-op, so a password rotation can never
    re-seed over a live admin.

    Never raises — boot must not fail because of a bootstrap hiccup.
    """
    password = os.environ.get("CQ_INITIAL_ADMIN_PASSWORD", "")
    if not password:
        return
    username = os.environ.get("CQ_INITIAL_ADMIN_USERNAME", "").strip() or _DEFAULT_ADMIN_USERNAME
    # Informational only — the SSM path, never the password, is logged.
    ssm_path = os.environ.get("CQ_INITIAL_ADMIN_PASSWORD_SSM_PATH", "").strip()

    try:
        if await _users_exist(store):
            log.debug("bootstrap_password_admin: skipped — users already present")
            return
        # Mutual exclusion with the email/magic-link path. If
        # bootstrap_first_admin_if_needed already ran it left the
        # _bootstrap_system row behind (and a pending founder invite) —
        # _users_exist excludes that row, so without this guard the
        # password path would *also* seed an admin, leaving the L2 with
        # two independent admin principals on one tenancy. The two
        # bootstraps are alternatives, not complements: the email path
        # ran first (app.py lifespan order), so it wins; defer to it.
        if await _system_row_exists(store):
            log.warning(
                "[BOOTSTRAP_ADMIN] password-admin bootstrap skipped — the email "
                "first-admin bootstrap already claimed this L2. Set only one of "
                "CQ_INITIAL_ADMIN_EMAIL / CQ_INITIAL_ADMIN_PASSWORD."
            )
            return
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_password_admin: user-count probe failed; skipping")
        return

    # Pin the admin to the L2's own tenancy. A user left on the
    # 'default-enterprise'/'default-group' server_default cannot transact
    # on the real tenancy — cross-Enterprise consults 403 with a
    # forwarder mismatch (see scripts/seed-users.py). Pin only when BOTH
    # are set; a partial config falls back to defaults — warn so the
    # operator notices the half-wired tenancy rather than discovering it
    # via empty tenancy-scoped reads later.
    ent_raw = os.environ.get("CQ_ENTERPRISE", "").strip()
    grp_raw = os.environ.get("CQ_GROUP", "").strip()
    if bool(ent_raw) != bool(grp_raw):
        log.warning(
            "[BOOTSTRAP_ADMIN] only one of CQ_ENTERPRISE/CQ_GROUP is set "
            "(enterprise=%r group=%r); seeding admin on the default tenancy. "
            "Set both or neither.",
            ent_raw,
            grp_raw,
        )
    enterprise_id: str | None = ent_raw or None
    group_id: str | None = grp_raw or None
    if enterprise_id is None or group_id is None:
        enterprise_id = group_id = None

    from .auth import hash_password

    try:
        await store.create_user(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            enterprise_id=enterprise_id,
            group_id=group_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_password_admin: admin user creation failed; skipping")
        return

    # Bracketed sentinel — greppable in CloudWatch Logs Insights.
    # The password is NEVER logged; only the SSM path it came from.
    source = f"SSM {ssm_path}" if ssm_path else "CQ_INITIAL_ADMIN_PASSWORD env"
    log.warning(
        "[BOOTSTRAP_ADMIN] admin user '%s' (role=admin, enterprise=%s, group=%s) seeded from %s",
        username,
        enterprise_id or "default-enterprise",
        group_id or "default-group",
        source,
    )


async def bootstrap_liaison_key_if_needed(store: SqliteStore) -> None:
    """Seed the Liaison Server's agent API key on first boot (decision 42, W2).

    The Quick-enablement stack co-deploys a Liaison Server (LS) sidecar that must
    authenticate to THIS L2 with a ``cqa.v1.*`` bearer. In the founder onboarding
    path there is NO admin principal at cold boot, so the LS can neither mint
    itself a key via the admin route (``require_admin``) nor consume the founder's
    one-time magic-link. Instead the provisioning worker generates the bearer ONCE
    and seeds it symmetrically: into the LS (as ``L2_AUTH_TOKEN``) and into THIS L2
    via ``CQ_INITIAL_LIAISON_KEY``. On first boot we insert that exact token as a
    full-capability agent key owned by a dedicated ``_liaison_service`` agent user
    — mirroring ``agent_key_routes.mint_agent_key`` but from a PROVIDED token.

    Unlike the admin bootstraps this is NOT mutually exclusive with the founder
    path — it is a service key, not an admin, so both run. Pins the agent to the
    L2's own tenancy (``CQ_ENTERPRISE``/``CQ_GROUP``) like the password-admin path.

    Idempotency: skipped when ``CQ_INITIAL_LIAISON_KEY`` is unset or the
    ``_liaison_service`` user already exists. Never raises — boot must not fail.
    """
    token = os.environ.get("CQ_INITIAL_LIAISON_KEY", "").strip()
    if not token:
        return
    pepper = os.environ.get("CQ_API_KEY_PEPPER", "")
    if not pepper:
        log.error(
            "[BOOTSTRAP_LIAISON] CQ_INITIAL_LIAISON_KEY set but CQ_API_KEY_PEPPER "
            "missing — cannot hash the key; skipping"
        )
        return

    try:
        if await store.get_user(_LIAISON_USERNAME) is not None:
            log.debug("bootstrap_liaison_key: skipped — _liaison_service already present")
            return
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_liaison_key: user probe failed; skipping")
        return

    import uuid
    from datetime import UTC, datetime

    from .api_keys import decode_token, hash_secret, secret_prefix
    from .auth import hash_password
    from .ttl import parse_ttl

    try:
        key_id, secret = decode_token(token)
    except Exception:  # noqa: BLE001
        log.error("[BOOTSTRAP_LIAISON] CQ_INITIAL_LIAISON_KEY is not a valid cqa.v1.* token; skipping")
        return

    # Pin to the L2's tenancy only when BOTH are set (matches password-admin).
    ent_raw = os.environ.get("CQ_ENTERPRISE", "").strip()
    grp_raw = os.environ.get("CQ_GROUP", "").strip()
    enterprise_id: str | None = ent_raw or None
    group_id: str | None = grp_raw or None
    if enterprise_id is None or group_id is None:
        enterprise_id = group_id = None

    ttl = os.environ.get("CQ_INITIAL_LIAISON_KEY_TTL", "").strip() or _DEFAULT_LIAISON_TTL
    try:
        expires_at = (datetime.now(UTC) + parse_ttl(ttl)).isoformat()
    except Exception:  # noqa: BLE001
        ttl = _DEFAULT_LIAISON_TTL
        expires_at = (datetime.now(UTC) + parse_ttl(ttl)).isoformat()

    try:
        await store.create_user(
            _LIAISON_USERNAME,
            hash_password(uuid.uuid4().hex),  # unusable password — bearer-only
            role="user",
            enterprise_id=enterprise_id,
            group_id=group_id,
        )
        new_user = await store.get_user(_LIAISON_USERNAME)
        if new_user is None:
            log.error("bootstrap_liaison_key: service user create returned None")
            return
        await store.upsert_persona_assignment(
            username=_LIAISON_USERNAME,
            persona="agent",
            assigned_at=datetime.now(UTC).isoformat(),
            assigned_by=_LIAISON_USERNAME,
            audit_action="CREATED",
            audit_old_persona=None,
        )
        await store.create_api_key(
            key_id=key_id.hex,
            user_id=int(new_user["id"]),
            name="liaison-server",
            labels=["harness:liaison", "persona:agent", "managed-by:provision"],
            key_prefix=secret_prefix(secret),
            key_hash=hash_secret(secret, pepper=pepper),
            ttl=ttl,
            expires_at=expires_at,
        )
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_liaison_key: seeding failed; skipping")
        return

    log.warning(
        "[BOOTSTRAP_LIAISON] Liaison service agent key seeded (key_id=%s, "
        "enterprise=%s, group=%s, ttl=%s)",
        key_id.hex,
        enterprise_id or "default-enterprise",
        group_id or "default-group",
        ttl,
    )


async def _users_exist(store: SqliteStore) -> bool:
    """Return True iff a real user exists (excluding marker rows).

    Excludes both ``_bootstrap_system`` and ``_liaison_service`` — neither is a
    real principal, so neither must make the founder/password admin bootstrap
    think the L2 is already onboarded. We let the SqliteStore expose this via raw
    text since there's no count-users helper today and this is one query at boot.
    """
    from sqlalchemy import text

    def _count() -> int:
        with store._engine.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username NOT IN (:sys, :liaison)"),
                {"sys": _SYSTEM_USERNAME, "liaison": _LIAISON_USERNAME},
            ).first()
            return int(row[0]) if row else 0

    import asyncio

    count = await asyncio.get_event_loop().run_in_executor(None, _count)
    return count > 0


async def _system_row_exists(store: SqliteStore) -> bool:
    """Return True iff the ``_bootstrap_system`` marker row exists.

    Its presence means :func:`bootstrap_first_admin_if_needed` already
    ran on this L2 — used by the password path to defer to the email
    path when both are configured.
    """
    from sqlalchemy import text

    def _count() -> int:
        with store._engine.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username = :sys"),
                {"sys": _SYSTEM_USERNAME},
            ).first()
            return int(row[0]) if row else 0

    import asyncio

    count = await asyncio.get_event_loop().run_in_executor(None, _count)
    return count > 0
