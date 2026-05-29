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
    can NEVER resolve to ``default-*`` here — neither in ``source`` NOR in the
    returned value, because a real-or-partial-default row falls through to the
    env branch (see the per-field real check above). The only way
    ``source == "default"`` arises with env present is a PARTIAL env (one var
    set, the other not), a misconfiguration we warn on loudly. Strict callers
    additionally reject ``source == "default"`` so a misconfigured L2 fails
    loud instead of mis-attributing.

    SCOPE: routing a write through this function eliminates the silent-default
    class FOR THAT path. As of #339 slice 1 the migrated paths are: KU propose
    (``_resolve_write_tenancy``), agent-key mint (``_resolve_admin_tenancy``),
    and activity-log (``log_activity`` — see the #335 fix narrative in
    ``activity_logger.py``'s tenancy block). Still carrying ad-hoc default literals
    and tracked for migration (#339):
      * ``consults._self_identity`` — WRITE-ADJACENT (stamps the ``from_l2_id``
        on outbound consult envelopes); migrate together with the
        cross-Enterprise gate at consults.py's ``self_enterprise`` compare so
        the two stay consistent (gating is security-sensitive — do them as one
        change, not piecemeal).
      * cross-enterprise consents, raw api_keys, and the ``auth.scope_filter``
        read-path.

    Never raises — see module docstring.
    """
    row_ent = ((user or {}).get("enterprise_id") or "").strip()
    row_grp = ((user or {}).get("group_id") or "").strip()
    # Per-field real check (8l-reviewer HIGH on PR #390): the row is
    # authoritative only when BOTH fields carry a real (non-empty,
    # non-default) value. A PARTIAL-default row — e.g. enterprise_id="acme",
    # group_id="default-group" (bootstrap_admin.py's independent per-column
    # `or "default-…"` fallbacks can produce these) — must NOT be returned
    # as "row", or a configured L2 would write the literal "default-group"
    # value (source="row" hides it) — the exact #324/#333/#335 leak with a
    # half-default row. Such rows fall through to env below.
    row_ent_real = bool(row_ent) and row_ent != DEFAULT_ENTERPRISE_ID
    row_grp_real = bool(row_grp) and row_grp != DEFAULT_GROUP_ID
    if row_ent_real and row_grp_real:
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
