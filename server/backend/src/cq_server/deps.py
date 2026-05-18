"""FastAPI dependencies shared across routers."""

import hmac
import os

from fastapi import BackgroundTasks, Depends, HTTPException, Request

from .api_keys import decode_token, hash_secret
from .store._sqlite import SqliteStore

API_KEY_PEPPER_ENV = "CQ_API_KEY_PEPPER"  # pragma: allowlist secret

# FO-3 (agent#193 / Decision 32) — central provisioning service base URL.
# The cq-server admin shell is a thin proxy onto the cq-directory
# provisioning service (`provision.8th-layer.ai`, 8th-layer-app account)
# which owns the L2-create job runner. cq-server holds no provisioning
# state; it forwards the wizard's create call and polls the job for SSE.
CQ_PROVISIONING_API_URL_ENV = "CQ_PROVISIONING_API_URL"
DEFAULT_PROVISIONING_API_URL = "https://provision.8th-layer.ai"


def get_provisioning_api_url() -> str:
    """Return the base URL of the cq-directory provisioning service.

    Reads ``CQ_PROVISIONING_API_URL`` (production default
    ``https://provision.8th-layer.ai``). Trailing slash stripped so
    callers can append ``/api/v1/...`` paths unconditionally.
    """
    return os.environ.get(CQ_PROVISIONING_API_URL_ENV, DEFAULT_PROVISIONING_API_URL).rstrip("/")


def get_store(request: Request) -> SqliteStore:
    """FastAPI dependency that returns the store from app state.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The Store instance attached to the application state.
    """
    return request.app.state.store


def get_api_key_pepper(request: Request) -> str:
    """Return the API key pepper from application state.

    Raises:
        HTTPException: 500 if the pepper has not been configured.
    """
    pepper = getattr(request.app.state, "api_key_pepper", None)
    if not pepper:
        raise HTTPException(status_code=500, detail="Server is misconfigured")
    return pepper


async def require_api_key(
    request: Request,
    background_tasks: BackgroundTasks,
    store: SqliteStore = Depends(get_store),
) -> str:
    """Authenticate an API key and return the owning user's username.

    The ``Authorization: Bearer <token>`` header must carry a valid,
    unrevoked, unexpired key. The token is decoded to its key id and
    secret components; the stored hash of the secret is compared to a
    fresh HMAC of the presented secret using ``hmac.compare_digest`` to
    avoid timing side channels.

    Args:
        request: The incoming FastAPI request.
        background_tasks: FastAPI background tasks used to record usage.
        store: The store dependency.

    Returns:
        The username of the authenticated caller.

    Raises:
        HTTPException: 401 on any authentication failure.
    """
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid API key")
    token = header.removeprefix("Bearer ")
    try:
        key_id, secret = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid API key") from exc
    pepper = get_api_key_pepper(request)
    row = await store.get_active_api_key_by_id(key_id.hex)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not hmac.compare_digest(row["key_hash"], hash_secret(secret, pepper=pepper)):
        raise HTTPException(status_code=401, detail="Invalid API key")
    background_tasks.add_task(store.touch_api_key_last_used, row["id"])
    return row["username"]
