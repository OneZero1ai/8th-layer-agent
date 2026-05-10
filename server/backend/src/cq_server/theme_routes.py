"""FastAPI router for ``GET /api/v1/theme`` (FO-1d, Decision 30).

Anonymous endpoint — the L2 login screen needs the Enterprise + L2
brand BEFORE auth so the user lands on a chrome that already looks
right (Decision 30 §"Why same login screen for user and admin"). Same
reason the platform-mark "Powered by 8th-Layer.ai" is rendered without
auth: brand identity is a first-impression contract.

Cache-Control: ``public, max-age=300`` — clients revalidate every 5
minutes. Matches the cadence we expect from the Theming admin form
(AS-5) re-publishing changes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from .deps import get_store
from .store._sqlite import SqliteStore
from .theme import ThemeResolver

router = APIRouter(tags=["theme"])

_resolver = ThemeResolver()


@router.get("/theme")
async def get_theme(
    response: Response,
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    """Return the resolved 3-tier theme JSON for the current host.

    Shape per Decision 30:

    ```json
    {
      "platform": {"name": ..., "version": ..., "tokens": {...}},
      "enterprise": {"id": ..., "display_name": ..., "logo_url": ...,
                      "accent_hex": ..., "dark_mode_only": ...},
      "l2": {"id": ..., "label": ..., "subaccent_hex": ...,
              "hero_motif": ...}
    }
    ```

    The FE applies ``--brand-primary`` (from ``enterprise.accent_hex``,
    falling back to platform cyan) and ``--brand-secondary`` (from
    ``l2.subaccent_hex``, falling back to ``enterprise.accent_hex``).
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    return await _resolver.resolve(store)
