"""Single source of truth for write-path tenancy resolution (agent#339).

Three bugs of one shape kept surfacing — a write path that doesn't consult
the L2's configured ``CQ_ENTERPRISE`` / ``CQ_GROUP`` env when the caller row
carries no real tenancy, so it silently writes ``default-enterprise`` /
``default-group`` on a *configured* L2:

  * #324 (fixed) — ``/propose`` stamped KU rows default-*.
  * #333 (fixed via #384) — invite-claim ``ensure_user`` stamped admin users default-*.
  * #335 — ``activity_log`` rows stamped ``tenant_enterprise=default-enterprise``.

Every write that needs an ``(enterprise_id, group_id)`` should resolve it
HERE so the bug class can't re-emerge as new writers are added. Callers that
must be strict (e.g. ``/propose``) inspect the returned ``source`` and reject
on ``"default"``; best-effort callers (e.g. activity logging) just use the
resolved values — this function NEVER raises.
"""

from __future__ import annotations

import logging
import os

from .tables import DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID

log = logging.getLogger(__name__)

# What ``source`` the resolution came from — lets strict callers branch.
TenancySource = str  # "row" | "env" | "default"


def resolve_tenancy(
    user: dict | None,
    *,
    context: str = "",
) -> tuple[str, str, TenancySource]:
    """Resolve ``(enterprise_id, group_id, source)`` for a write.

    Priority:
      1. ``"row"``     — the caller's row tenancy when it is non-default and
                         fully populated (the common case: a principal minted
                         on a configured L2 inherited that L2's tenancy).
      2. ``"env"``     — ``CQ_ENTERPRISE`` + ``CQ_GROUP`` when BOTH are set
                         (the configured L2's own identity). This is what
                         rescues a default-* row on a configured L2 — the
                         exact #324/#333/#335 bug.
      3. ``"default"`` — neither row nor env carry real tenancy: a fully
                         default-but-populated row (an unconfigured dev L2),
                         or the schema constants when the row is empty too.

    Runtime guard (agent#339): a *fully* configured L2 (both env vars set)
    can NEVER resolve to ``default-*`` here — branch 2 returns env first. The
    only way ``source == "default"`` arises with env present is a PARTIAL env
    (one var set, the other not), which is a misconfiguration we warn on
    loudly. Routing every write through this function therefore eliminates
    the silent-default class; strict callers additionally reject ``source ==
    "default"`` so a misconfigured L2 fails loud instead of mis-attributing.

    Never raises — see module docstring.
    """
    row_ent = ((user or {}).get("enterprise_id") or "").strip()
    row_grp = ((user or {}).get("group_id") or "").strip()
    row_is_default = (
        row_ent in ("", DEFAULT_ENTERPRISE_ID) and row_grp in ("", DEFAULT_GROUP_ID)
    )
    if not row_is_default and row_ent and row_grp:
        return row_ent, row_grp, "row"

    env_ent = os.environ.get("CQ_ENTERPRISE", "").strip()
    env_grp = os.environ.get("CQ_GROUP", "").strip()
    if env_ent and env_grp:
        return env_ent, env_grp, "env"
    if bool(env_ent) != bool(env_grp):
        # Partial config is the dangerous case — a configured-looking L2 that
        # silently defaults. Warn loudly (agent#339 "log loudly").
        log.warning(
            "resolve_tenancy[%s]: partial env (CQ_ENTERPRISE=%r CQ_GROUP=%r) — "
            "falling back to default tenancy. Set BOTH or NEITHER.",
            context,
            env_ent,
            env_grp,
        )

    # Unconfigured dev L2: the row carries the non-empty schema defaults and
    # the operator opted into that by not setting env. Keep local dev working.
    if row_ent and row_grp:
        return row_ent, row_grp, "default"
    return DEFAULT_ENTERPRISE_ID, DEFAULT_GROUP_ID, "default"
