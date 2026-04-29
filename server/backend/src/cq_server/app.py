"""cq knowledge store API."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from cq.models import (
    Context,
    FlagReason,
    Insight,
    KnowledgeUnit,
    Tier,
    create_knowledge_unit,
)
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from .auth import router as auth_router
from .deps import API_KEY_PEPPER_ENV, require_api_key
from .embed import compose_text, embed_text
from .quality import check_propose_quality
from .review import router as review_router
from .scoring import apply_confirmation, apply_flag
from .store import RemoteStore, normalize_domains

_STATIC_DIR = Path(__file__).parent / "static"


class ProposeRequest(BaseModel):
    """Request body for proposing a new knowledge unit."""

    domains: list[str] = Field(min_length=1)
    insight: Insight
    context: Context = Field(default_factory=Context)
    created_by: str = ""


class FlagRequest(BaseModel):
    """Request body for flagging a knowledge unit."""

    reason: FlagReason


class StatsResponse(BaseModel):
    """Response body for store statistics."""

    total_units: int
    tiers: dict[str, int]
    domains: dict[str, int]


_store: RemoteStore | None = None


def _get_store() -> RemoteStore:
    """Return the global store instance."""
    if _store is None:
        raise RuntimeError("Store not initialised")
    return _store


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    """Manage the store lifecycle."""
    global _store  # noqa: PLW0603
    jwt_secret = os.environ.get("CQ_JWT_SECRET")
    if not jwt_secret:
        raise RuntimeError("CQ_JWT_SECRET environment variable is required")
    pepper = os.environ.get(API_KEY_PEPPER_ENV, "")
    if not pepper:
        raise RuntimeError(f"{API_KEY_PEPPER_ENV} environment variable is required")
    db_path = Path(os.environ.get("CQ_DB_PATH", "/data/cq.db"))
    _store = RemoteStore(db_path=db_path)
    app_instance.state.store = _store
    app_instance.state.api_key_pepper = pepper
    yield
    _store.close()


# --- API routes on a shared router so they can be mounted at both / and /api. ---

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(review_router)


@api_router.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


class SemanticHit(BaseModel):
    """A KU with its similarity score for /query/semantic."""

    knowledge_unit: KnowledgeUnit
    similarity: float


class AigrpLookupRequest(BaseModel):
    """Request body for /aigrp/lookup — Phase 2 automatic-trigger endpoint.

    The harness fires this on user_prompt / session_start / tool_failure
    moments. The server embeds the freeform context, runs semantic search
    over approved KUs, applies persona+confidence+similarity filters, and
    returns ranked hits the harness injects as a system-reminder.
    """

    context: str = Field(min_length=1)
    trigger: str = "user_prompt"
    session_id: str = ""
    persona: str = ""
    max_results: int = Field(default=5, gt=0, le=20)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    min_similarity: float = Field(default=0.3, ge=0.0, le=1.0)
    exclude_self: bool = True


class AigrpLookupHit(BaseModel):
    """Lean wire shape returned by /aigrp/lookup — only the fields the
    harness needs to inject as a system-reminder. Avoids shipping the
    full KnowledgeUnit blob (with evidence, context, etc.) on every
    prompt."""

    ku_id: str
    summary: str
    action: str
    domains: list[str]
    similarity: float
    confidence: float
    created_by: str


class AigrpLookupResponse(BaseModel):
    trigger: str
    results: list[AigrpLookupHit]
    elapsed_ms: int
    filtered_count: int  # how many candidates dropped by filters


@api_router.post("/aigrp/lookup")
def aigrp_lookup(
    request: AigrpLookupRequest,
    _username: str = Depends(require_api_key),
) -> AigrpLookupResponse:
    """Automatic-trigger lookup for AIGRP-pull (Phase 2).

    Fired by the harness on every prompt / session-start / tool-failure.
    Embeds the freeform context, runs semantic search, filters by
    confidence + similarity + exclude_self, returns ranked hits.
    """
    import time

    t0 = time.monotonic()
    store = _get_store()
    payload = embed_text(request.context)
    if payload is None:
        # Don't 503 here — the hook is best-effort and a 503 would
        # log loudly on every prompt if Bedrock is briefly slow.
        return AigrpLookupResponse(
            trigger=request.trigger, results=[], elapsed_ms=0, filtered_count=0
        )
    from .embed import unpack

    query_vec = unpack(payload[0])
    raw_hits = store.semantic_query(
        query_vec,
        limit=request.max_results * 3,  # over-fetch so filters have headroom
    )

    filtered: list[AigrpLookupHit] = []
    dropped = 0
    for unit, sim in raw_hits:
        if sim < request.min_similarity:
            dropped += 1
            continue
        if unit.evidence.confidence < request.min_confidence:
            dropped += 1
            continue
        if request.exclude_self and request.persona and unit.created_by == request.persona:
            dropped += 1
            continue
        filtered.append(
            AigrpLookupHit(
                ku_id=unit.id,
                summary=unit.insight.summary,
                action=unit.insight.action,
                domains=list(unit.domains),
                similarity=sim,
                confidence=unit.evidence.confidence,
                created_by=unit.created_by,
            )
        )
        if len(filtered) >= request.max_results:
            break

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return AigrpLookupResponse(
        trigger=request.trigger,
        results=filtered,
        elapsed_ms=elapsed_ms,
        filtered_count=dropped,
    )


@api_router.get("/query/semantic")
def query_semantic(
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(gt=0, le=50)] = 10,
    _username: str = Depends(require_api_key),
) -> list[SemanticHit]:
    """Embed `q` and return top-N approved KUs by cosine similarity."""
    store = _get_store()
    payload = embed_text(q)
    if payload is None:
        raise HTTPException(status_code=503, detail="embedding unavailable")
    from .embed import unpack

    query_vec = unpack(payload[0])
    hits = store.semantic_query(query_vec, limit=limit)
    return [SemanticHit(knowledge_unit=u, similarity=s) for u, s in hits]


@api_router.get("/query")
def query_units(
    domains: Annotated[list[str], Query()],
    languages: Annotated[list[str] | None, Query()] = None,
    frameworks: Annotated[list[str] | None, Query()] = None,
    pattern: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0)] = 5,
) -> list[KnowledgeUnit]:
    """Search knowledge units by domain tags with relevance ranking."""
    store = _get_store()
    return store.query(
        domains,
        languages=languages,
        frameworks=frameworks,
        pattern=pattern or "",
        limit=limit,
    )


@api_router.post("/propose", status_code=201)
def propose_unit(
    request: ProposeRequest,
    username: str = Depends(require_api_key),
) -> KnowledgeUnit:
    """Submit a new knowledge unit.

    ``created_by`` is always set to the authenticated caller's username; any
    value supplied by the client is discarded.
    """
    store = _get_store()
    normalized = normalize_domains(request.domains)
    if not normalized:
        raise HTTPException(status_code=422, detail="At least one non-empty domain is required")
    quality_reason = check_propose_quality(normalized, request.insight)
    if quality_reason is not None:
        raise HTTPException(status_code=422, detail=f"propose quality guard: {quality_reason}")
    unit = create_knowledge_unit(
        domains=normalized,
        insight=request.insight,
        context=request.context,
        tier=Tier.PRIVATE,
        created_by=username,
    )
    embed_payload = embed_text(
        compose_text(
            request.insight.summary,
            request.insight.detail,
            request.insight.action,
        )
    )
    if embed_payload is not None:
        embedding_bytes, embedding_model = embed_payload
        store.insert(unit, embedding=embedding_bytes, embedding_model=embedding_model)
    else:
        store.insert(unit)
    return unit


@api_router.post("/confirm/{unit_id}")
def confirm_unit(unit_id: str, _username: str = Depends(require_api_key)) -> KnowledgeUnit:
    """Confirm a knowledge unit, boosting its confidence."""
    store = _get_store()
    unit = store.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    confirmed = apply_confirmation(unit)
    store.update(confirmed)
    return confirmed


@api_router.post("/flag/{unit_id}")
def flag_unit(unit_id: str, request: FlagRequest, _username: str = Depends(require_api_key)) -> KnowledgeUnit:
    """Flag a knowledge unit, reducing its confidence."""
    store = _get_store()
    unit = store.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    flagged = apply_flag(unit, request.reason)
    store.update(flagged)
    return flagged


@api_router.get("/stats")
def stats() -> StatsResponse:
    """Return store statistics."""
    store = _get_store()
    return StatsResponse(
        total_units=store.count(),
        tiers=store.counts_by_tier(),
        domains=store.domain_counts(),
    )


# --- Application assembly. ---

app = FastAPI(title="cq Server", version="0.1.0", lifespan=lifespan)

# Mount API routes at root (SDK compatibility) and at /api (frontend).
app.include_router(api_router)
app.include_router(api_router, prefix="/api/v1")

# Serve the frontend static build when present (combined Docker image).
if _STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa_fallback(path: str) -> FileResponse:
        """Serve the SPA entry point for any unmatched path."""
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(_STATIC_DIR / "index.html")


def main() -> None:
    """Start the cq API server."""
    port = int(os.environ.get("CQ_PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
