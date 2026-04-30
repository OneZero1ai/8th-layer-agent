"""cq knowledge store API."""

import json
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

from . import aigrp
from .auth import router as auth_router
from .deps import API_KEY_PEPPER_ENV, require_api_key
from .embed import compose_text, embed_text
from .embed import model_id as embed_model_id
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


async def _aigrp_bootstrap_and_poll(store: RemoteStore) -> None:
    """Bootstrap into the Enterprise mesh on first start, then poll
    every known peer's /aigrp/signature on a 5-min interval forever.

    Best-effort: any individual call failure is logged and skipped;
    convergence happens on the next poll. This task lives for the
    lifetime of the FastAPI process.
    """
    import asyncio
    import logging
    import urllib.request

    log = logging.getLogger("aigrp")
    poll_interval = int(os.environ.get("CQ_AIGRP_POLL_INTERVAL_SEC", "300"))

    # 1. If not first deploy, hit the seed peer's /aigrp/hello once.
    if not aigrp.is_first_deploy() and aigrp.seed_peer_url():
        try:
            hello_payload = json.dumps({
                "l2_id": aigrp.self_l2_id(),
                "enterprise": aigrp.enterprise(),
                "group": aigrp.group(),
                "endpoint_url": aigrp.self_url(),
            }).encode()
            req = urllib.request.Request(
                f"{aigrp.seed_peer_url()}/api/v1/aigrp/hello",
                method="POST",
                data=hello_payload,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {os.environ.get('CQ_AIGRP_PEER_KEY', '')}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            for p in body.get("peers", []):
                if p.get("l2_id") == aigrp.self_l2_id():
                    continue
                store.upsert_aigrp_peer(
                    l2_id=p["l2_id"],
                    enterprise=p["enterprise"],
                    group=p["group"],
                    endpoint_url=p["endpoint_url"],
                    embedding_centroid=None,
                    domain_bloom=None,
                    ku_count=p.get("ku_count", 0),
                    domain_count=p.get("domain_count", 0),
                    embedding_model=p.get("embedding_model"),
                    signature_received=False,
                )
            log.info("aigrp bootstrap: seed=%s peers=%d", aigrp.seed_peer_url(), len(body.get("peers", [])))
        except Exception:
            log.exception("aigrp bootstrap to seed=%s failed; will retry via poll loop", aigrp.seed_peer_url())

    # 2. Periodic peer-poll loop — fetch each peer's /aigrp/signature.
    import base64

    while True:
        try:
            await asyncio.sleep(poll_interval)
            peers = store.list_aigrp_peers(aigrp.enterprise())
            for p in peers:
                if p["l2_id"] == aigrp.self_l2_id():
                    continue
                if not p["endpoint_url"]:
                    continue
                try:
                    req = urllib.request.Request(
                        f"{p['endpoint_url'].rstrip('/')}/api/v1/aigrp/signature",
                        method="GET",
                        headers={"authorization": f"Bearer {os.environ.get('CQ_AIGRP_PEER_KEY', '')}"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        sig = json.loads(resp.read())
                    centroid = base64.b64decode(sig["embedding_centroid_b64"]) if sig.get("embedding_centroid_b64") else None
                    bloom = base64.b64decode(sig["domain_bloom_b64"]) if sig.get("domain_bloom_b64") else None
                    store.upsert_aigrp_peer(
                        l2_id=sig["l2_id"],
                        enterprise=sig["enterprise"],
                        group=sig["group"],
                        endpoint_url=sig.get("endpoint_url") or p["endpoint_url"],
                        embedding_centroid=centroid,
                        domain_bloom=bloom,
                        ku_count=sig.get("ku_count", 0),
                        domain_count=sig.get("domain_count", 0),
                        embedding_model=sig.get("embedding_model"),
                        signature_received=True,
                    )
                except Exception:
                    log.warning("aigrp poll of peer %s failed", p["l2_id"])
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("aigrp poll loop iteration crashed; continuing")


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    """Manage the store lifecycle + AIGRP background task."""
    import asyncio

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

    aigrp_task = None
    if aigrp.aigrp_enabled():
        aigrp_task = asyncio.create_task(_aigrp_bootstrap_and_poll(_store))

    yield

    if aigrp_task is not None:
        aigrp_task.cancel()
        try:
            await aigrp_task
        except (asyncio.CancelledError, Exception):
            pass
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


class AigrpHelloRequest(BaseModel):
    """A new L2 introducing itself to a seed peer.

    Empty `endpoint_url` flags the joiner as a *stub L2* — it can poll
    peers but cannot be polled back (typical for an L2 behind NAT, e.g.
    a developer laptop or a customer-edge node without inbound exposure).
    Stub peers are recorded in the table but skipped by the periodic
    poll loop (since there's no address to poll).
    """

    l2_id: str = Field(min_length=1, description="canonical Enterprise/Group identity")
    enterprise: str = Field(min_length=1)
    group: str = Field(min_length=1)
    endpoint_url: str = Field(default="", description="how peers should reach me; empty = stub L2 (consumer-only)")


class AigrpAnnounceRequest(BaseModel):
    """A peer flooding the existence of another L2 to its sibling peers."""

    l2_id: str = Field(min_length=1)
    enterprise: str = Field(min_length=1)
    group: str = Field(min_length=1)
    endpoint_url: str = Field(default="", description="empty = stub L2 (consumer-only)")
    announced_by: str = Field(default="", description="l2_id of the peer doing the flooding")


class AigrpPeer(BaseModel):
    """One row from the peer table — wire shape."""

    l2_id: str
    enterprise: str
    group: str
    endpoint_url: str
    ku_count: int
    domain_count: int
    embedding_model: str | None = None
    first_seen_at: str
    last_seen_at: str
    last_signature_at: str | None = None


class AigrpPeersResponse(BaseModel):
    enterprise: str
    self_l2_id: str
    peer_count: int
    peers: list[AigrpPeer]


class AigrpSignatureResponse(BaseModel):
    """This L2's current corpus signature."""

    l2_id: str
    enterprise: str
    group: str
    endpoint_url: str
    ku_count: int
    domain_count: int
    embedding_model: str | None = None
    embedding_centroid_b64: str | None = None
    domain_bloom_b64: str | None = None
    computed_at: str


def _build_self_signature(store: RemoteStore) -> AigrpSignatureResponse:
    """Walk the local approved corpus and produce this L2's signature."""
    import base64

    embeddings = store.approved_embeddings_iter()
    centroid = aigrp.compute_centroid(embeddings)
    domains = store.approved_domains()
    bloom = aigrp.compute_domain_bloom(domains)
    return AigrpSignatureResponse(
        l2_id=aigrp.self_l2_id(),
        enterprise=aigrp.enterprise(),
        group=aigrp.group(),
        endpoint_url=aigrp.self_url(),
        ku_count=len(embeddings),
        domain_count=len(domains),
        embedding_model=embed_model_id() if embeddings else None,
        embedding_centroid_b64=base64.b64encode(centroid).decode("ascii") if centroid else None,
        domain_bloom_b64=base64.b64encode(bloom).decode("ascii"),
        computed_at=aigrp.now_iso(),
    )


@api_router.post("/aigrp/hello", status_code=201)
def aigrp_hello(
    body: AigrpHelloRequest,
    _peer: None = Depends(aigrp.require_peer_key),
) -> AigrpPeersResponse:
    """A new L2 announces itself to this seed peer.

    Validates the shared EnterprisePeerKey, refuses if the new L2 claims
    a different Enterprise. Records the new peer in our local table,
    then fans out /aigrp/announce to every other known peer (best-effort,
    background — does not block the hello response).
    """
    if body.enterprise != aigrp.enterprise():
        raise HTTPException(
            status_code=403,
            detail=f"this L2 belongs to enterprise={aigrp.enterprise()!r}; refusing hello from {body.enterprise!r}",
        )

    store = _get_store()
    store.upsert_aigrp_peer(
        l2_id=body.l2_id,
        enterprise=body.enterprise,
        group=body.group,
        endpoint_url=body.endpoint_url,
        embedding_centroid=None,
        domain_bloom=None,
        ku_count=0,
        domain_count=0,
        embedding_model=None,
        signature_received=False,
    )

    # Compose the response — current peer table from this L2's POV.
    peers = store.list_aigrp_peers(aigrp.enterprise())

    # Fan out the new peer's existence to every other known peer
    # asynchronously. Failures are best-effort; convergence is via
    # subsequent polls.
    import asyncio

    async def _flood() -> None:
        try:
            import urllib.request

            announce_payload = json.dumps({
                "l2_id": body.l2_id,
                "enterprise": body.enterprise,
                "group": body.group,
                "endpoint_url": body.endpoint_url,
                "announced_by": aigrp.self_l2_id(),
            }).encode()
            for p in peers:
                if p["l2_id"] == body.l2_id or p["l2_id"] == aigrp.self_l2_id():
                    continue
                if not p["endpoint_url"]:
                    continue
                req = urllib.request.Request(
                    f"{p['endpoint_url'].rstrip('/')}/api/v1/aigrp/announce",
                    method="POST",
                    data=announce_payload,
                    headers={
                        "content-type": "application/json",
                        "authorization": f"Bearer {os.environ.get('CQ_AIGRP_PEER_KEY', '')}",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=5):
                        pass
                except Exception:
                    pass  # best-effort flood

        except Exception:
            pass

    asyncio.create_task(_flood())

    # Include ourselves (the seed) in the peer list. New L2 needs to know
    # we exist as a peer; otherwise it learns about every OTHER peer the
    # seed knows but never records the seed itself. Real EIGRP neighbor
    # adjacency advertisements include the speaker too.
    self_entry = {
        "l2_id": aigrp.self_l2_id(),
        "enterprise": aigrp.enterprise(),
        "group": aigrp.group(),
        "endpoint_url": aigrp.self_url(),
        "ku_count": 0,
        "domain_count": 0,
        "embedding_model": None,
        "first_seen_at": aigrp.now_iso(),
        "last_seen_at": aigrp.now_iso(),
        "last_signature_at": None,
    }
    peers_plus_self = peers + [self_entry]

    return AigrpPeersResponse(
        enterprise=aigrp.enterprise(),
        self_l2_id=aigrp.self_l2_id(),
        peer_count=len(peers_plus_self),
        peers=[AigrpPeer(**{k: v for k, v in p.items() if k in AigrpPeer.model_fields}) for p in peers_plus_self],
    )


@api_router.post("/aigrp/announce", status_code=201)
def aigrp_announce(
    body: AigrpAnnounceRequest,
    _peer: None = Depends(aigrp.require_peer_key),
) -> dict[str, str]:
    """A sibling L2 is informing us that a new peer has joined the mesh."""
    if body.enterprise != aigrp.enterprise():
        raise HTTPException(
            status_code=403,
            detail=f"this L2 belongs to enterprise={aigrp.enterprise()!r}; refusing announce for {body.enterprise!r}",
        )

    store = _get_store()
    store.upsert_aigrp_peer(
        l2_id=body.l2_id,
        enterprise=body.enterprise,
        group=body.group,
        endpoint_url=body.endpoint_url,
        embedding_centroid=None,
        domain_bloom=None,
        ku_count=0,
        domain_count=0,
        embedding_model=None,
        signature_received=False,
    )
    return {"recorded": body.l2_id, "by": aigrp.self_l2_id()}


@api_router.get("/aigrp/peers")
def aigrp_peers(
    _peer: None = Depends(aigrp.require_peer_key),
) -> AigrpPeersResponse:
    """Return our current view of the Enterprise's peer mesh."""
    store = _get_store()
    peers = store.list_aigrp_peers(aigrp.enterprise())
    return AigrpPeersResponse(
        enterprise=aigrp.enterprise(),
        self_l2_id=aigrp.self_l2_id(),
        peer_count=len(peers),
        peers=[AigrpPeer(**{k: v for k, v in p.items() if k in AigrpPeer.model_fields}) for p in peers],
    )


@api_router.get("/aigrp/signature")
def aigrp_signature(
    _peer: None = Depends(aigrp.require_peer_key),
) -> AigrpSignatureResponse:
    """Return this L2's current corpus signature — centroid + Bloom filter
    + counts. Polled by every peer on the AIGRP polling interval."""
    store = _get_store()
    return _build_self_signature(store)


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
