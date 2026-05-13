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
     passkey — never logs in), mints invite for AdminEmail, logs
     ``[BOOTSTRAP_ADMIN] magic_link=https://<slug>.8th-layer.ai/invite/<token>``.
  4. Operator copies that URL from CloudWatch and emails it (or
     in-process the wizard could surface it directly on the
     "you're done" screen — that's a follow-up).
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
        invite, token = mint_invite(
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

    public_url_base = os.environ.get("CQ_PUBLIC_BASE_URL", "").rstrip("/")
    if public_url_base:
        magic_link = f"{public_url_base}/invite/{token}"
    else:
        magic_link = f"/invite/{token}  (set CQ_PUBLIC_BASE_URL to surface the absolute URL)"

    # Bracketed sentinel makes it greppable in CloudWatch Logs Insights.
    log.warning(
        "[BOOTSTRAP_ADMIN] First-admin invite minted for %s (invite_id=%d, expires=%s). magic_link=%s",
        admin_email,
        invite.id,
        invite.expires_at,
        magic_link,
    )


async def _users_exist(store: SqliteStore) -> bool:
    """Return True iff at least one row in users (excluding the system row).

    We let the SqliteStore expose this via raw text since there's no
    count-users helper today and this is one query at boot.
    """
    from sqlalchemy import text

    def _count() -> int:
        with store._engine.connect() as conn:  # noqa: SLF001
            row = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username != :sys"),
                {"sys": _SYSTEM_USERNAME},
            ).first()
            return int(row[0]) if row else 0

    import asyncio

    count = await asyncio.get_event_loop().run_in_executor(None, _count)
    return count > 0
