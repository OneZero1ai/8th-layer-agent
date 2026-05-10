"""Theme resolver — 3-tier brand hierarchy (FO-1d, Decision 30).

Anonymous endpoint substrate. Resolves the platform / Enterprise / L2
JSON the React shell consumes via ``GET /api/v1/theme`` to apply CSS
custom properties at runtime.

# Tier shape

* **platform** — hardcoded constants. The 8th-Layer.ai mark and the
  cyan/violet/emerald/gold/rose token names. Customers cannot override.
* **enterprise** — display name + optional logo URL + optional accent
  hex. V1 stub: returns the env-pinned ``CQ_ENTERPRISE`` as the display
  name with null logo and null accent. Future: read from the directory
  record (sprint 3 client) once the Enterprise overrides land in
  ``directory_client.py``'s announce/peering payload.
* **l2** — short label + optional sub-accent + optional hero motif.
  Read from the single-row ``l2_brand`` table (migration 0020).
  Defaults to the env-pinned ``CQ_GROUP`` if no row exists.

# Caching

The endpoint emits ``Cache-Control: public, max-age=300``. The browser
revalidates every 5 minutes, which matches the "Theming admin form
re-publishes" cadence we expect from AS-5. No server-side cache yet —
the resolver does at most one SQLite read per call which is cheap.
"""

from __future__ import annotations

from typing import Any

from . import aigrp
from .store._sqlite import SqliteStore

# --- Platform constants ---------------------------------------------------
#
# Mirrors ``server/frontend/src/index.css``'s ``data-theme="8th-layer"``
# token block. If a value diverges between this dict and the CSS, the CSS
# wins for actual rendering — this dict is purely for the JSON contract
# the API surfaces (so external tools, e.g. a marketing-site theme
# reader, can pull the platform palette without parsing CSS).

PLATFORM_NAME = "8th-Layer.ai"
PLATFORM_VERSION = "1.0.0"
PLATFORM_TOKENS: dict[str, str] = {
    "cyan": "#5bd0ff",
    "violet": "#a685ff",
    "emerald": "#10b981",
    "gold": "#fcd34d",
    "rose": "#ff5c7c",
    "ink": "#e6e6e6",
    "bg-from": "#0a0612",
    "bg-via": "#07070b",
    "bg-to": "#040810",
}

# --- Enterprise stub ------------------------------------------------------
#
# V1: derive from the env-pinned ``CQ_ENTERPRISE``. When the directory
# record carries display-name / logo / accent overrides (post-sprint-3),
# this resolver will read them; for now it returns a structurally valid
# response with ``logo_url=None`` and ``accent_hex=None`` so the FE falls
# back to the platform default cyan via CSS.


def _resolve_enterprise() -> dict[str, Any]:
    """Return the Enterprise tier of the theme.

    V1 stub — display_name = enterprise id, logo and accent are None.
    The shape is stable so the FE can rely on these keys existing.
    """
    enterprise_id = aigrp.enterprise()
    return {
        "id": enterprise_id,
        "display_name": enterprise_id,
        "logo_url": None,
        "accent_hex": None,
        "dark_mode_only": True,
    }


# --- L2 resolver ----------------------------------------------------------


async def _resolve_l2(store: SqliteStore) -> dict[str, Any]:
    """Return the L2 tier of the theme.

    Reads the ``l2_brand`` single-row table; falls back to the env-pinned
    group id when no override row exists. Always emits a structurally
    valid object so the FE can rely on key presence.
    """
    enterprise_id = aigrp.enterprise()
    group_id = aigrp.group()
    row = await store.get_l2_brand()

    if row is None:
        return {
            "id": f"{enterprise_id}/{group_id}",
            "label": group_id,
            "subaccent_hex": None,
            "hero_motif": None,
        }
    return {
        "id": f"{enterprise_id}/{group_id}",
        "label": row["l2_label"] or group_id,
        "subaccent_hex": row["subaccent_hex"],
        "hero_motif": row["hero_motif"],
    }


# --- Top-level resolver ---------------------------------------------------


class ThemeResolver:
    """Compose the 3-tier theme JSON for the current host.

    Held as a class so the test surface can patch ``_resolve_enterprise``
    or ``_resolve_l2`` independently. The instance is otherwise stateless;
    ``app.state.store`` is passed in via ``resolve()``.
    """

    async def resolve(self, store: SqliteStore) -> dict[str, Any]:
        """Return the merged theme dict.

        Shape mirrors Decision 30's example block exactly so the FE
        TypeScript types can stay in lockstep with the spec.
        """
        return {
            "platform": {
                "name": PLATFORM_NAME,
                "version": PLATFORM_VERSION,
                "tokens": dict(PLATFORM_TOKENS),
            },
            "enterprise": _resolve_enterprise(),
            "l2": await _resolve_l2(store),
        }


__all__ = [
    "PLATFORM_NAME",
    "PLATFORM_TOKENS",
    "PLATFORM_VERSION",
    "ThemeResolver",
]
