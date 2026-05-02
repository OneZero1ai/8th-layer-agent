"""cq knowledge store API."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from . import aigrp
from .auth import require_admin
from .auth import router as auth_router
from .consults import router as consults_router
from .db_url import resolve_sqlite_db_path
from .deps import API_KEY_PEPPER_ENV, require_api_key
from .embed import compose_text, embed_text
from .embed import model_id as embed_model_id
from .migrations import run_migrations
from .network import router as network_router
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
    needs_bootstrap = (not aigrp.is_first_deploy()) and bool(aigrp.seed_peer_url())

    def _try_bootstrap() -> bool:
        """Hit the seed's /aigrp/hello and absorb its peer table.
        Returns True on success. Idempotent on the seed side.

        Sprint 4 (#44) — every hello (initial + every poll cycle re-hello)
        carries this L2's Ed25519 forward-signing public key so the
        receiver can populate its ``aigrp_peers.public_key_ed25519`` row.
        Re-sending on every cycle is intentional self-healing: if the
        first hello was lost or the receiver was rebuilt, the next cycle
        recovers without operator intervention.
        """
        from . import forward_sign

        try:
            hello_payload = json.dumps(
                {
                    "l2_id": aigrp.self_l2_id(),
                    "enterprise": aigrp.enterprise(),
                    "group": aigrp.group(),
                    "endpoint_url": aigrp.self_url(),
                    "public_key_ed25519": forward_sign.self_public_key_b64u(),
                }
            ).encode()
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
                    public_key_ed25519=p.get("public_key_ed25519"),
                )
            log.info("aigrp bootstrap: seed=%s peers=%d", aigrp.seed_peer_url(), len(body.get("peers", [])))
            return True
        except Exception:
            log.warning("aigrp bootstrap to seed=%s failed; will retry on next poll cycle", aigrp.seed_peer_url())
            return False

    # 1. First-attempt bootstrap on startup (best effort).
    if needs_bootstrap and _try_bootstrap():
        needs_bootstrap = False

    # 2. Periodic peer-poll loop — fetch each peer's /aigrp/signature.
    #    Also re-attempts bootstrap on every cycle while it's still
    #    pending; gives self-healing if the seed was down at start.
    import base64

    def _rehello_peer(peer_endpoint: str) -> None:
        """Re-send hello to a known peer so they pick up our pubkey.

        Sprint 4 (#44) — re-hello on every poll cycle, idempotent on the
        receiver. This is the recovery path for a peer that joined
        before this L2 generated its key, or whose row was rebuilt.
        """
        from . import forward_sign

        payload = json.dumps(
            {
                "l2_id": aigrp.self_l2_id(),
                "enterprise": aigrp.enterprise(),
                "group": aigrp.group(),
                "endpoint_url": aigrp.self_url(),
                "public_key_ed25519": forward_sign.self_public_key_b64u(),
            }
        ).encode()
        try:
            req = urllib.request.Request(
                f"{peer_endpoint.rstrip('/')}/api/v1/aigrp/hello",
                method="POST",
                data=payload,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {os.environ.get('CQ_AIGRP_PEER_KEY', '')}",
                },
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:  # noqa: BLE001 — best-effort
            pass

    while True:
        try:
            await asyncio.sleep(poll_interval)
            if needs_bootstrap and _try_bootstrap():
                needs_bootstrap = False
            peers = store.list_aigrp_peers(aigrp.enterprise())
            for p in peers:
                if p["l2_id"] == aigrp.self_l2_id():
                    continue
                if not p["endpoint_url"]:
                    continue
                # Sprint 4 — push our pubkey to this peer (cheap, idempotent).
                _rehello_peer(p["endpoint_url"])
                try:
                    req = urllib.request.Request(
                        f"{p['endpoint_url'].rstrip('/')}/api/v1/aigrp/signature",
                        method="GET",
                        headers={"authorization": f"Bearer {os.environ.get('CQ_AIGRP_PEER_KEY', '')}"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        sig = json.loads(resp.read())
                    centroid = (
                        base64.b64decode(sig["embedding_centroid_b64"]) if sig.get("embedding_centroid_b64") else None
                    )
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
    # Resolve URL and filesystem path together so the migration runner
    # and the runtime store cannot diverge on which database they're
    # using — see ``resolve_sqlite_db_path``. This drops once #309
    # wires ``SqliteStore`` to ``CQ_DATABASE_URL`` directly.
    database_url, db_path = resolve_sqlite_db_path()
    # Bring the database under Alembic management before opening the
    # store. Three cases handled: fresh DB → upgrade head; pre-Alembic
    # DB → stamp baseline + upgrade head; already-stamped DB → upgrade
    # head (no-op when no pending revisions). The legacy
    # ``_ensure_schema()`` inside SqliteStore still runs after this;
    # both paths are idempotent and the legacy one will be removed in
    # #310 once this PR has rolled out everywhere.
    run_migrations(database_url)
    # Phase 1 of upstream merge — keep our sync RemoteStore as the
    # primary store (carries all fork-delta methods: AIGRP, consults,
    # directory, multi-tenant scope). Phase 2 ports to async SqliteStore.
    _store = RemoteStore(db_path=db_path)
    app_instance.state.store = _store
    app_instance.state.api_key_pepper = pepper

    aigrp_task = None
    if aigrp.aigrp_enabled():
        aigrp_task = asyncio.create_task(_aigrp_bootstrap_and_poll(_store))

    # DSN signature cache loop (issue #23). Runs on every cq-server
    # process — for the marketing aggregator it keeps the public DSN
    # resolver fast (cache reads, not fan-outs); for fleet L2s it's a
    # no-op because the resolver isn't hit from outside the aggregator.
    # We start it unconditionally because the marketing aggregator runs
    # the same image as fleet L2s.
    from .network import _signature_cache_loop

    dsn_cache_task = asyncio.create_task(_signature_cache_loop())

    # Sprint 3 — 8th-Layer Directory client (announce + 1h pull loop).
    # Opt-in via CQ_DIRECTORY_ENABLED — defaults off until the public
    # directory is deployed. The bootstrap function self-skips with a
    # log line when disabled or under-configured.
    from .directory_client import directory_bootstrap_and_loop

    directory_task = asyncio.create_task(directory_bootstrap_and_loop(_store))

    try:
        yield
    finally:
        for task in (aigrp_task, dsn_cache_task, directory_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        _store.close()


# --- API routes on a shared router so they can be mounted at both / and /api. ---

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(review_router)
api_router.include_router(network_router)
api_router.include_router(consults_router)


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
    prompt.
    """

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
        return AigrpLookupResponse(trigger=request.trigger, results=[], elapsed_ms=0, filtered_count=0)
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

    Sprint 4 (#44) — joiners include their forward-signing Ed25519
    public key (base64url-encoded). Optional for backward compat with
    pre-sprint-4 peers; receivers store NULL when absent and fall back
    to legacy unsigned forward auth for that peer.
    """

    l2_id: str = Field(min_length=1, description="canonical Enterprise/Group identity")
    enterprise: str = Field(min_length=1)
    group: str = Field(min_length=1)
    endpoint_url: str = Field(default="", description="how peers should reach me; empty = stub L2 (consumer-only)")
    public_key_ed25519: str | None = Field(
        default=None,
        description="forward-signing Ed25519 public key, unpadded base64url; None on pre-sprint-4 peers",
    )


class AigrpAnnounceRequest(BaseModel):
    """A peer flooding the existence of another L2 to its sibling peers."""

    l2_id: str = Field(min_length=1)
    enterprise: str = Field(min_length=1)
    group: str = Field(min_length=1)
    endpoint_url: str = Field(default="", description="empty = stub L2 (consumer-only)")
    announced_by: str = Field(default="", description="l2_id of the peer doing the flooding")
    public_key_ed25519: str | None = Field(
        default=None,
        description="forwarded pubkey of the joiner (sprint 4); None for legacy peers",
    )


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
    public_key_ed25519: str | None = None


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
async def aigrp_hello(
    body: AigrpHelloRequest,
    _peer: None = Depends(aigrp.require_peer_key),
) -> AigrpPeersResponse:
    """A new L2 announces itself to this seed peer.

    Validates the shared EnterprisePeerKey, refuses if the new L2 claims
    a different Enterprise. Records the new peer in our local table,
    then fans out /aigrp/announce to every other known peer (best-effort,
    background — does not block the hello response).

    Async because we spawn the flood as an asyncio.Task. The SQLite
    store calls are fast enough to run inline on the event loop.
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
        public_key_ed25519=body.public_key_ed25519,
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

            announce_payload = json.dumps(
                {
                    "l2_id": body.l2_id,
                    "enterprise": body.enterprise,
                    "group": body.group,
                    "endpoint_url": body.endpoint_url,
                    "announced_by": aigrp.self_l2_id(),
                    "public_key_ed25519": body.public_key_ed25519,
                }
            ).encode()
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
    from . import forward_sign

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
        "public_key_ed25519": forward_sign.self_public_key_b64u(),
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
        public_key_ed25519=body.public_key_ed25519,
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
    + counts. Polled by every peer on the AIGRP polling interval.
    """
    store = _get_store()
    return _build_self_signature(store)


class AigrpForwardQueryRequest(BaseModel):
    """Cross-L2 forward-query — one L2 asking another L2 for KUs.

    Phase 6 step 2 / Lane B. The response shape mirrors /aigrp/lookup
    where it overlaps but adds tenancy scope (so the requester can
    correlate to its own peer table) and an explicit ``redacted_fields``
    list for any policy-suppressed fields.
    """

    query_vec: list[float] = Field(min_length=1)
    query_text: str = ""
    requester_l2_id: str = Field(min_length=1)
    requester_enterprise: str = Field(min_length=1)
    requester_group: str = Field(min_length=1)
    requester_persona: str = ""
    max_results: int = Field(default=5, gt=0, le=20)


class AigrpForwardQueryHit(BaseModel):
    """One KU returned by /aigrp/forward-query.

    ``detail`` and ``action`` are populated under ``full_body`` policy
    and omitted (None) under ``summary_only``. ``redacted_fields`` lists
    the field names that were withheld so the requester knows what to
    ask for via a higher-trust channel if they need it.
    """

    ku_id: str
    summary: str
    detail: str | None = None
    action: str | None = None
    domains: list[str]
    sim_score: float
    redacted_fields: list[str] = Field(default_factory=list)


class AigrpForwardQueryResponse(BaseModel):
    """Wire shape for /aigrp/forward-query."""

    responder_l2_id: str
    responder_enterprise: str
    responder_group: str
    policy_applied: str  # "summary_only" | "full_body" | "denied"
    results: list[AigrpForwardQueryHit]
    result_count: int


def _decide_policy_for_ku(
    *,
    ku_enterprise: str,
    ku_group: str,
    ku_cross_group_allowed: bool,
    requester_enterprise: str,
    requester_group: str,
    responder_enterprise: str,
    responder_group: str,
    cross_enterprise_consent: dict[str, object] | None,
) -> tuple[str, list[str]]:
    """Return (policy, redacted_fields) for one KU.

    Implements the rule set spelled out in
    docs/plans/08-live-network-demo.md Lane B step 2:

      1. Same Enterprise + same Group         -> full_body
      2. Same Enterprise + different Group    -> full_body iff cross_group_allowed
                                                else summary_only
      3. Different Enterprise                 -> consent-driven; default deny

    First match wins. Pure function — no DB access — so it's cheap to
    fan over every candidate hit.
    """
    if requester_enterprise == ku_enterprise:
        if requester_group == ku_group:
            return "full_body", []
        if ku_cross_group_allowed:
            return "full_body", []
        return "summary_only", ["detail", "action"]
    # Different Enterprise.
    if cross_enterprise_consent is None:
        return "denied", []
    policy = str(cross_enterprise_consent.get("policy") or "")
    if policy == "full_body":
        return "full_body", []
    # v1 consent grants only summary_only sharing.
    return "summary_only", ["detail", "action"]


@api_router.post("/aigrp/forward-query")
def aigrp_forward_query(
    body: AigrpForwardQueryRequest,
    request: Request,
    _peer: None = Depends(aigrp.require_peer_key),
) -> AigrpForwardQueryResponse:
    """Cross-L2 forward-query — Phase 6 step 2.

    A sibling (or, with consent, a foreign-Enterprise) L2 sends a query
    embedding plus its identity. We run semantic search over our own
    approved KUs, evaluate the per-KU sharing policy, and return either
    a redacted summary list, full bodies, or zero results when policy
    forbids. Every call is appended to the ``cross_l2_audit`` table.

    Auth: shared EnterprisePeerKey (same as the rest of /aigrp/*). For
    cross-Enterprise calls the additional gate is the
    ``cross_enterprise_consents`` row — without that the call returns
    zero results silently rather than 401, so probes can't fingerprint
    consent state.

    SEC-CRIT #34 — caller declares its identity in ``X-8L-Forwarder-L2-Id``;
    receiver pins it to ``body.requester_l2_id`` and to its own Enterprise.
    Closes cross-Enterprise impersonation; sibling-L2 spoof inside an
    Enterprise is the residual gap (sprint 4 / Ed25519).
    """
    import uuid

    # AIGRP forward-query supports cross-Enterprise via consent table — the
    # foreign forwarder's Enterprise legitimately differs from the receiver's.
    # Sprint 4 (#44) — when the peer has a pubkey on file, also verifies
    # the Ed25519 signature over JCS(body) || requester_l2_id.
    store = _get_store()
    aigrp.require_forwarder_identity(
        request,
        body.requester_l2_id,
        same_enterprise_only=False,
        body_for_sig=body.model_dump(mode="json"),
        store=store,
    )

    responder_enterprise = aigrp.enterprise()
    responder_group = aigrp.group()
    responder_l2_id = aigrp.self_l2_id()

    # Same-Enterprise vs cross-Enterprise gate. For cross-Enterprise the
    # consent record drives whether we even attempt to return rows.
    consent: dict[str, object] | None = None
    if body.requester_enterprise != responder_enterprise:
        consent = store.find_cross_enterprise_consent(
            requester_enterprise=body.requester_enterprise,
            responder_enterprise=responder_enterprise,
            requester_group=body.requester_group,
            responder_group=responder_group,
            now_iso=aigrp.now_iso(),
        )
        if consent is None:
            # Silent deny — log the attempt and return empty.
            store.record_cross_l2_audit(
                audit_id=uuid.uuid4().hex,
                ts=aigrp.now_iso(),
                requester_l2_id=body.requester_l2_id,
                requester_enterprise=body.requester_enterprise,
                requester_group=body.requester_group,
                requester_persona=body.requester_persona or None,
                responder_l2_id=responder_l2_id,
                responder_enterprise=responder_enterprise,
                responder_group=responder_group,
                policy_applied="denied",
                result_count=0,
                consent_id=None,
            )
            return AigrpForwardQueryResponse(
                responder_l2_id=responder_l2_id,
                responder_enterprise=responder_enterprise,
                responder_group=responder_group,
                policy_applied="denied",
                results=[],
                result_count=0,
            )

    raw_hits = store.semantic_query_with_scope(
        body.query_vec,
        limit=body.max_results * 3,
    )

    results: list[AigrpForwardQueryHit] = []
    # The response-level ``policy_applied`` is the strictest applied
    # across the returned hits ("summary_only" wins over "full_body" if
    # any KU was redacted).
    response_policy = "full_body"
    for hit in raw_hits:
        unit: KnowledgeUnit = hit["unit"]
        policy, redacted = _decide_policy_for_ku(
            ku_enterprise=hit["enterprise_id"],
            ku_group=hit["group_id"],
            ku_cross_group_allowed=hit["cross_group_allowed"],
            requester_enterprise=body.requester_enterprise,
            requester_group=body.requester_group,
            responder_enterprise=responder_enterprise,
            responder_group=responder_group,
            cross_enterprise_consent=consent,
        )
        if policy == "denied":
            continue
        if policy == "summary_only":
            response_policy = "summary_only"
            results.append(
                AigrpForwardQueryHit(
                    ku_id=unit.id,
                    summary=unit.insight.summary,
                    detail=None,
                    action=None,
                    domains=list(unit.domains),
                    sim_score=hit["similarity"],
                    redacted_fields=redacted,
                )
            )
        else:
            results.append(
                AigrpForwardQueryHit(
                    ku_id=unit.id,
                    summary=unit.insight.summary,
                    detail=unit.insight.detail,
                    action=unit.insight.action,
                    domains=list(unit.domains),
                    sim_score=hit["similarity"],
                    redacted_fields=[],
                )
            )
        if len(results) >= body.max_results:
            break

    consent_id = str(consent["consent_id"]) if consent else None
    store.record_cross_l2_audit(
        audit_id=uuid.uuid4().hex,
        ts=aigrp.now_iso(),
        requester_l2_id=body.requester_l2_id,
        requester_enterprise=body.requester_enterprise,
        requester_group=body.requester_group,
        requester_persona=body.requester_persona or None,
        responder_l2_id=responder_l2_id,
        responder_enterprise=responder_enterprise,
        responder_group=responder_group,
        policy_applied=response_policy,
        result_count=len(results),
        consent_id=consent_id,
    )

    return AigrpForwardQueryResponse(
        responder_l2_id=responder_l2_id,
        responder_enterprise=responder_enterprise,
        responder_group=responder_group,
        policy_applied=response_policy,
        results=results,
        result_count=len(results),
    )


# --- Phase 6 step 3 — Lane C: presence registry ---------------------------

# Heartbeat cadence advertised back to the client. 5-min default keeps the
# table fresh without hammering the DB; clients can over-shoot but we expect
# the harness hook to land near this number.
PEER_HEARTBEAT_INTERVAL_SECONDS = 300
PEER_ACTIVE_DEFAULT_WINDOW_MIN = 15


class PeerHeartbeatRequest(BaseModel):
    """Request body for ``POST /peers/heartbeat``.

    ``persona`` is the agent identity within the caller's tenant — e.g.
    ``persona-cloudfront-asker``. ``discoverable=False`` keeps the
    persona out of the active-peers listing while still recording the
    heartbeat (so an admin dashboard can show presence even for opted-
    out personas). ``expertise_domains`` is a free-text tag list; the
    server stores it as JSON and returns it verbatim.
    """

    persona: str = Field(min_length=1, max_length=128)
    discoverable: bool = False
    working_dir_hint: str | None = Field(default=None, max_length=512)
    expertise_domains: list[str] | None = None


class PeerHeartbeatResponse(BaseModel):
    """Response from a successful heartbeat — echoes the persona and tells the client when to next call back."""

    persona: str
    registered_at: str
    next_heartbeat_in_seconds: int


class ActivePeer(BaseModel):
    """One row of the active-peers listing returned by GET /peers/active."""

    persona: str
    enterprise_id: str
    group_id: str
    last_seen_at: str
    minutes_since_last_seen: float
    discoverable: bool
    working_dir_hint: str | None = None
    expertise_domains: list[str] | None = None


class ActivePeersResponse(BaseModel):
    """Wire shape for GET /peers/active — list + count for client-side rendering."""

    active_peers: list[ActivePeer]
    count: int


@api_router.post("/peers/heartbeat")
def peers_heartbeat(
    request: PeerHeartbeatRequest,
    username: str = Depends(require_api_key),
) -> PeerHeartbeatResponse:
    """Register or refresh a persona's presence on this L2.

    Auth: any valid API key. Tenancy scope (``enterprise_id`` /
    ``group_id``) is resolved from the authenticated user's row — the
    request body never carries scope to avoid spoofing. The row is
    UPSERTed; ``last_seen_at`` advances to "now" on every call.
    """
    from datetime import UTC, datetime

    store = _get_store()
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    now = datetime.now(UTC).isoformat()
    store.upsert_peer(
        persona=request.persona,
        user_id=int(user["id"]),
        enterprise_id=user["enterprise_id"],
        group_id=user["group_id"],
        last_seen_at=now,
        expertise_domains=request.expertise_domains,
        discoverable=request.discoverable,
        working_dir_hint=request.working_dir_hint,
    )
    return PeerHeartbeatResponse(
        persona=request.persona,
        registered_at=now,
        next_heartbeat_in_seconds=PEER_HEARTBEAT_INTERVAL_SECONDS,
    )


@api_router.get("/peers/active")
def peers_active(
    group: Annotated[str | None, Query()] = None,
    since_minutes: Annotated[int, Query(gt=0, le=24 * 60)] = PEER_ACTIVE_DEFAULT_WINDOW_MIN,
    include_self: Annotated[bool, Query()] = False,
    self_persona: Annotated[str | None, Query(alias="self_persona")] = None,
    username: str = Depends(require_api_key),
) -> ActivePeersResponse:
    """Return discoverable peers in the caller's Enterprise.

    Scoping rules (intentionally Enterprise-bounded):

      - Cross-Enterprise visibility is NOT granted by consent; presence
        is its own privacy plane. A consent record unlocks knowledge
        access via /aigrp/forward-query, not who's online.
      - ``group`` narrows further to a single Group inside the caller's
        Enterprise.
      - ``include_self=False`` hides ``self_persona`` from the result.
        The requester provides ``self_persona`` because the API key
        does not pin a single persona — one user can own many personas.

    ``minutes_since_last_seen`` is computed against the row's ISO
    timestamp at request time, so it's monotone-decreasing across calls.
    """
    from datetime import UTC, datetime, timedelta

    store = _get_store()
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    now = datetime.now(UTC)
    since_iso = (now - timedelta(minutes=since_minutes)).isoformat()
    rows = store.list_active_peers(
        enterprise_id=user["enterprise_id"],
        since_iso=since_iso,
        group_id=group,
        exclude_persona=None if include_self else self_persona,
    )
    active: list[ActivePeer] = []
    for r in rows:
        try:
            last_seen = datetime.fromisoformat(r["last_seen_at"])
        except ValueError:
            # Defensive — should never happen since we wrote ISO-8601.
            continue
        delta_min = max(0.0, (now - last_seen).total_seconds() / 60.0)
        active.append(
            ActivePeer(
                persona=r["persona"],
                enterprise_id=r["enterprise_id"],
                group_id=r["group_id"],
                last_seen_at=r["last_seen_at"],
                minutes_since_last_seen=round(delta_min, 2),
                discoverable=r["discoverable"],
                working_dir_hint=r["working_dir_hint"],
                expertise_domains=r["expertise_domains"],
            )
        )
    return ActivePeersResponse(active_peers=active, count=len(active))


# --- Phase 6 step 3 — Lane D: consent admin endpoints ---------------------


class SignConsentRequest(BaseModel):
    """Request body for ``POST /consents/sign``.

    Group columns are optional — null means "any group on that side"
    (wildcard). Same shape as the row schema in
    ``cross_enterprise_consents``. Only ``summary_only`` is accepted in
    v1; ``full_body`` cross-Enterprise sharing is intentionally deferred
    until a higher-trust signing flow exists.
    """

    requester_enterprise: str = Field(min_length=1)
    responder_enterprise: str = Field(min_length=1)
    requester_group: str | None = None
    responder_group: str | None = None
    policy: str = Field(default="summary_only")
    expires_at: str | None = None


class SignConsentResponse(BaseModel):
    """Response from POST /consents/sign — echoes the new consent_id and audit pairing."""

    consent_id: str
    signed_by_admin: str
    signed_at: str
    audit_log_id: str


class ConsentRecord(BaseModel):
    """Public view of one row from cross_enterprise_consents."""

    consent_id: str
    requester_enterprise: str
    responder_enterprise: str
    requester_group: str | None = None
    responder_group: str | None = None
    policy: str
    signed_by_admin: str
    signed_at: str
    expires_at: str | None = None
    audit_log_id: str


class ConsentListResponse(BaseModel):
    """Wire shape for GET /consents — listing + count."""

    consents: list[ConsentRecord]
    count: int


@api_router.post("/consents/sign", status_code=201)
def consents_sign(
    request: SignConsentRequest,
    admin_username: str = Depends(require_admin),
) -> SignConsentResponse:
    """Admin-only: sign a cross-Enterprise consent.

    422 when the request is malformed (intra-Enterprise pair, unsupported
    policy). 409 when an unexpired consent already exists for the same
    ``(req_ent, resp_ent, req_grp, resp_grp)`` tuple. On success a row
    is inserted into ``cross_enterprise_consents`` and a paired audit
    record into ``cross_l2_audit`` with ``policy_applied='consent_signed'``.
    """
    import uuid

    if request.requester_enterprise == request.responder_enterprise:
        raise HTTPException(
            status_code=422,
            detail="cross-Enterprise consents must span two distinct Enterprises; "
            "use the per-KU cross_group_allowed flag for intra-Enterprise scoping",
        )
    if request.policy != "summary_only":
        raise HTTPException(
            status_code=422,
            detail=f"unsupported policy {request.policy!r}; only 'summary_only' is allowed in v1",
        )

    store = _get_store()
    now = aigrp.now_iso()
    existing = store.find_active_consent_for_pair(
        requester_enterprise=request.requester_enterprise,
        responder_enterprise=request.responder_enterprise,
        requester_group=request.requester_group,
        responder_group=request.responder_group,
        now_iso=now,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"active consent already exists: {existing['consent_id']}",
        )

    consent_id = "consent_" + uuid.uuid4().hex[:20]
    audit_log_id = "aud_" + uuid.uuid4().hex[:20]
    store.insert_cross_enterprise_consent(
        consent_id=consent_id,
        requester_enterprise=request.requester_enterprise,
        responder_enterprise=request.responder_enterprise,
        requester_group=request.requester_group,
        responder_group=request.responder_group,
        policy=request.policy,
        signed_by_admin=admin_username,
        signed_at=now,
        expires_at=request.expires_at,
        audit_log_id=audit_log_id,
    )
    # Pair the sign event with a row in cross_l2_audit so the audit log
    # tells the full story of "who signed what, when".
    store.record_cross_l2_audit(
        audit_id=audit_log_id,
        ts=now,
        requester_l2_id=None,
        requester_enterprise=request.requester_enterprise,
        requester_group=request.requester_group,
        requester_persona=None,
        responder_l2_id=None,
        responder_enterprise=request.responder_enterprise,
        responder_group=request.responder_group,
        policy_applied="consent_signed",
        result_count=0,
        consent_id=consent_id,
    )
    return SignConsentResponse(
        consent_id=consent_id,
        signed_by_admin=admin_username,
        signed_at=now,
        audit_log_id=audit_log_id,
    )


@api_router.get("/consents")
def consents_list(
    include_expired: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(gt=0, le=500)] = 50,
    _admin: str = Depends(require_admin),
) -> ConsentListResponse:
    """Admin-only: list cross-Enterprise consents.

    By default only active (non-expired) rows are returned; pass
    ``include_expired=true`` to see soft-revoked / time-boxed records.
    Records are ordered newest-first by ``signed_at``.
    """
    store = _get_store()
    rows = store.list_cross_enterprise_consents(
        include_expired=include_expired,
        now_iso=aigrp.now_iso(),
        limit=limit,
    )
    return ConsentListResponse(
        consents=[ConsentRecord(**r) for r in rows],
        count=len(rows),
    )


@api_router.delete("/consents/{consent_id}", status_code=200)
def consents_revoke(
    consent_id: str,
    admin_username: str = Depends(require_admin),
) -> dict[str, str]:
    """Admin-only: soft-revoke a consent.

    Sets ``expires_at`` to "now" rather than deleting the row, so the
    consent's audit history (who signed it, when) survives revocation.
    Writes a paired ``cross_l2_audit`` row with
    ``policy_applied='consent_revoked'``. 404 when the id is unknown.
    """
    import uuid

    store = _get_store()
    row = store.get_cross_enterprise_consent(consent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Consent not found")
    now = aigrp.now_iso()
    store.revoke_cross_enterprise_consent(consent_id=consent_id, revoked_at=now)
    store.record_cross_l2_audit(
        audit_id="aud_" + uuid.uuid4().hex[:20],
        ts=now,
        requester_l2_id=None,
        requester_enterprise=row["requester_enterprise"],
        requester_group=row["requester_group"],
        requester_persona=None,
        responder_l2_id=None,
        responder_enterprise=row["responder_enterprise"],
        responder_group=row["responder_group"],
        policy_applied="consent_revoked",
        result_count=0,
        consent_id=consent_id,
    )
    return {
        "consent_id": consent_id,
        "revoked_at": now,
        "revoked_by_admin": admin_username,
    }


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
async def query_units(
    domains: Annotated[list[str], Query()],
    languages: Annotated[list[str] | None, Query()] = None,
    frameworks: Annotated[list[str] | None, Query()] = None,
    pattern: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0)] = 5,
    username: str = Depends(require_api_key),
) -> list[KnowledgeUnit]:
    """Search knowledge units by domain tags with relevance ranking.

    Auth: API key required. Tenancy scope (``enterprise_id`` /
    ``group_id``) is resolved from the authenticated user's row — the
    request never carries scope. Results are restricted to the caller's
    Enterprise (own Group plus cross-group-allowed KUs); cross-Enterprise
    discovery flows through ``/aigrp/forward-query`` (consent + audit).
    """
    store = _get_store()
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return store.query(
        domains,
        languages=languages,
        frameworks=frameworks,
        pattern=pattern or "",
        limit=limit,
        enterprise_id=user["enterprise_id"],
        group_id=user["group_id"],
    )


@api_router.post("/propose", status_code=201)
async def propose_unit(
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
async def confirm_unit(unit_id: str, _username: str = Depends(require_api_key)) -> KnowledgeUnit:
    """Confirm a knowledge unit, boosting its confidence."""
    store = _get_store()
    unit = store.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    confirmed = apply_confirmation(unit)
    store.update(confirmed)
    return confirmed


@api_router.post("/flag/{unit_id}")
async def flag_unit(unit_id: str, request: FlagRequest, _username: str = Depends(require_api_key)) -> KnowledgeUnit:
    """Flag a knowledge unit, reducing its confidence."""
    store = _get_store()
    unit = store.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="Knowledge unit not found")
    flagged = apply_flag(unit, request.reason)
    store.update(flagged)
    return flagged


@api_router.get("/stats")
def stats(
    username: str = Depends(require_api_key),
) -> StatsResponse:
    """Return store statistics scoped to the caller's Enterprise.

    SEC-HIGH #39 — pre-fix this was unauthenticated and returned global
    counts (cardinality + domain taxonomy across all tenants). Now
    requires API key auth and scopes aggregates to the caller's
    Enterprise — same pattern as /query (CRIT #33).
    """
    store = _get_store()
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    enterprise_id = user["enterprise_id"]
    return StatsResponse(
        total_units=store.count_in_enterprise(enterprise_id),
        tiers=store.counts_by_tier(enterprise_id=enterprise_id),
        domains=store.domain_counts(enterprise_id=enterprise_id),
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
