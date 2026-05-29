"""Non-blocking activity-log writer for #108 Stage 2 instrumentation.

Stage 2 wraps Stage 1's substrate (``cq_server.activity`` +
``store.append_activity``) into a *single* helper every write-path
handler can hand to FastAPI's ``BackgroundTasks``. The helper:

* Resolves the caller's tenancy (``enterprise_id`` / ``group_id``) from
  the user row at task-execution time. Doing the resolution inside the
  background task — not inside the request handler — keeps the response
  path off the database for the audit write. Cost: one extra
  ``SELECT users WHERE username = ?`` per logged event. That cost is
  bounded; on the order of microseconds, and the request has already
  been sent.
* Swallows every failure path. The audit log is fire-and-forget by
  design (#108: "if the activity log write fails, the response still
  succeeds — log a warning, don't error"). Schema-engineer's
  ``store.append_activity`` raises ``ValueError`` on unknown event_type
  and ``IntegrityError`` on CHECK violation; both are caught here.
* Mirrors the system-event shape from the schema sketch — when the
  caller is None / the user row vanished, ``persona`` and ``human``
  fall through as ``None`` and the row records under
  ``aigrp.enterprise()`` / ``aigrp.group()`` (this L2's own identity)
  rather than failing.

The helper is intentionally a free function rather than a method on
``SqliteStore``: it composes Store + activity-id generation + tenancy
resolution, none of which are store-internal concerns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .activity import EVENT_TYPES, generate_activity_id, now_iso_z
from .tenancy import resolve_tenancy

if TYPE_CHECKING:
    from .store._sqlite import SqliteStore

__all__ = [
    "log_activity",
    "summary_first_60",
]

logger = logging.getLogger(__name__)


def summary_first_60(text_value: str | None) -> str:
    """Return at most the first 60 chars of ``text_value``.

    Used to clamp KU summaries before they land in ``payload`` —
    enforces the #108 schema sketch's ``summary_first_60_chars`` shape
    and keeps the audit row from holding the entire KU body (which
    already lives in ``knowledge_units.data``).
    """
    if not text_value:
        return ""
    return text_value[:60]


async def log_activity(
    store: SqliteStore,
    *,
    username: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    thread_or_chain_id: str | None = None,
) -> None:
    """Append one ``activity_log`` row; swallow any failure.

    Designed to be scheduled via ``background_tasks.add_task(...)`` from
    every write-path handler. The response is already sent by the time
    this runs; any exception here is logged at WARNING level and never
    propagates back to the client.

    ``username`` may be ``None`` for system-emitted events (e.g. the
    retention sweeper, AIGRP convergence hooks). When set, the user row
    drives the row's ``tenant_enterprise`` / ``tenant_group`` /
    ``persona``. When unset, the row is filed under this L2's own
    Enterprise/Group with ``persona=None`` and ``human=None``.

    ``event_type`` must be one of ``cq_server.activity.EVENT_TYPES``;
    a typo here would otherwise hit the CHECK constraint at write time.
    Validating in Python turns it into a logged warning rather than an
    SQL exception silently swallowed by the background runner.
    """
    if event_type not in EVENT_TYPES:
        logger.warning(
            "activity log: refusing to append unknown event_type %r (expected one of %s)",
            event_type,
            sorted(EVENT_TYPES),
        )
        return

    try:
        tenant_enterprise: str
        tenant_group: str | None
        persona: str | None
        # agent#335 — resolve tenancy through the single resolver (agent#339)
        # so a default-* user row on a CONFIGURED L2 stamps the env tenancy
        # instead of the literal "default-enterprise". The old code did
        # ``user.get("enterprise_id") or aigrp.enterprise()`` — but a
        # "default-enterprise" row value is TRUTHY, so the ``or`` never
        # consulted env and the audit row drifted to default-* even though
        # the KU row (which already used the resolver) was stamped correctly.
        if username is None:
            ent, grp, _ = resolve_tenancy(None, context="activity_log")
            tenant_enterprise, tenant_group, persona = ent, grp, None
        else:
            # ``user`` may be None (deletion race between auth and this
            # background task) — resolve_tenancy(None) then falls to env,
            # same as the system-event path. ``persona`` is still the
            # username the auth layer accepted.
            user = await store.get_user(username)
            ent, grp, _ = resolve_tenancy(user, context="activity_log")
            tenant_enterprise, tenant_group = ent, grp
            persona = username

        await store.append_activity(
            activity_id=generate_activity_id(),
            ts=now_iso_z(),
            tenant_enterprise=tenant_enterprise,
            tenant_group=tenant_group,
            persona=persona,
            # ``human`` is the operator-mapped human identity. The
            # human-to-persona mapping landed in #98 plans but isn't
            # wired into the user row yet; leaving NULL until that
            # mapping ships keeps this row truthful rather than fake-
            # filling with the username.
            human=None,
            event_type=event_type,
            payload=payload,
            result_summary=result_summary,
            thread_or_chain_id=thread_or_chain_id,
        )
    except Exception:  # noqa: BLE001 — fire-and-forget by design
        logger.warning(
            "activity log append failed: event_type=%s persona=%s thread=%s",
            event_type,
            username,
            thread_or_chain_id,
            exc_info=True,
        )
