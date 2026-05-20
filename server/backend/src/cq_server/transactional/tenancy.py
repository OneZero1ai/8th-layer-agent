"""Tenancy enforcement for the central transactional-mail service.

Decision 34 §"Tenancy enforcement" — every send must have a ``to``
that the calling L2 is permitted to address:

* Categories 1, 2, 3 (``invite_magic_link`` / ``password_reset`` /
  ``two_factor``): the ``to`` must be (a) a known user of the
  caller's tenancy, OR (b) a pending-invitee for the caller's
  tenancy.
* Categories 4, 5 (``account_event`` / ``security_alert``): the
  ``to`` must be a known user of the caller's tenancy.

The control plane's ``users`` and ``invites`` tables are the truth.
A pending-invitee is an ``invites`` row where ``claimed_at IS NULL``
and ``revoked_at IS NULL``.

# Caveat — pending-invitee tenancy

``invites`` has no direct ``enterprise_id`` / ``group_id`` columns.
For pending invitees we look the issuer up in ``users`` and compare
the issuer's tenancy to the caller's. This is the same join the
``invites.py`` admin-list paths do.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)

INVITE_CATEGORIES = {"invite_magic_link", "password_reset", "two_factor"}
USER_ONLY_CATEGORIES = {"account_event", "security_alert"}


def enforce_tenancy(
    *,
    store: Any,
    enterprise_id: str,
    group_id: str,
    to: str,
    category: str,
) -> bool:
    """Return True iff the caller may send to ``to`` for this category.

    Returns False on every form of "not allowed" — the caller maps to
    a 403. We deliberately do not distinguish "unknown user" from
    "user belongs to a different tenancy", to avoid the response
    becoming a user-existence oracle for a malicious L2.
    """
    address = to.lower()
    engine = store._engine  # noqa: SLF001

    with engine.connect() as conn:
        # Step 1 — is the address a known user of this tenancy?
        user_row = conn.execute(
            text(
                "SELECT enterprise_id, group_id FROM users "
                "WHERE LOWER(email) = :e"
            ),
            {"e": address},
        ).fetchone()
        if user_row is not None:
            user_ent, user_grp = user_row[0], user_row[1]
            if user_ent == enterprise_id and user_grp == group_id:
                return True
            # Address belongs to a *different* tenancy. Categories 4/5
            # require a known user; categories 1-3 fall through to the
            # invite check (a cross-tenant user with a pending invite
            # for THIS tenancy is still a valid recipient for the
            # invite). The invite check is keyed by email + tenancy of
            # the inviter, which excludes the foreign-tenancy user.
            if category in USER_ONLY_CATEGORIES:
                return False

        # Step 2 — categories 4/5 reject if no user matched.
        if category in USER_ONLY_CATEGORIES:
            return False

        # Step 3 — categories 1/2/3 also allow pending-invitee.
        if category in INVITE_CATEGORIES:
            invite_row = conn.execute(
                text(
                    "SELECT i.id FROM invites i "
                    "JOIN users u ON u.id = i.issued_by "
                    "WHERE LOWER(i.email) = :e "
                    "  AND u.enterprise_id = :ent "
                    "  AND u.group_id = :grp "
                    "  AND i.claimed_at IS NULL "
                    "  AND i.revoked_at IS NULL "
                    "LIMIT 1"
                ),
                {"e": address, "ent": enterprise_id, "grp": group_id},
            ).fetchone()
            if invite_row is not None:
                return True

    # Unknown category or no matching tenancy/invite — fail closed.
    return False
