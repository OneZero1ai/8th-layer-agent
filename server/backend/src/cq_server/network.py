"""Network-demo proxy endpoints — Lane H/I/J support.

This module aggregates calls across the live 6-L2 fleet for the
``/network`` page in the frontend. It owns three POST endpoints:

* ``/api/v1/network/topology`` — fan-out aggregator that calls each L2's
  ``/aigrp/peers``, ``/aigrp/signature``, and ``/peers/active`` and folds
  the result into the ``TopologyResponse`` shape the frontend types
  declare. Cached in-process for 3 seconds to damp the 5s poll cadence.
* ``/api/v1/network/dsn/resolve`` — DSN-style intent resolver. Embeds the
  free-text intent via Bedrock Titan, fans out to every L2's
  ``/aigrp/signature`` to recover its centroid, ranks by cosine
  similarity, returns top-N candidates with the policy that *would* be
  applied if the caller forwarded a query.
* ``/api/v1/network/demo/{scenario}`` — three deterministic packet-trace
  scenarios that exercise the live fleet end-to-end (cross-Group,
  cross-Enterprise blocked, cross-Enterprise consented). Returns a list
  of trace events the frontend animates as a packet flow over the
  topology graph.

The module is the only place the FLEET_L2S table lives today; a future
PR can move it into env-var or DB config.

Auth is the standard JWT dep (``get_current_user``) — these endpoints
expose network-operations metadata, not raw KU bodies, so any logged-in
user can see them. The fan-out itself authenticates to each L2 with the
per-Enterprise peer key (read from SSM Parameter Store at startup) for
``/aigrp/*`` and an admin-issued JWT for ``/peers/active``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import create_token, get_current_user
from .deps import get_store
from .embed import embed_text, unpack
from .store import RemoteStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fleet roster — the 6 L2s the demo aggregates over.
#
# Hardcoded in-module for now. Each row carries the slug used in API
# requests, the Enterprise/Group it belongs to, and the ALB endpoint the
# proxy will fan out to. Future PR can move this to env vars.
# ---------------------------------------------------------------------------

FLEET_L2S: list[dict[str, str]] = [
    {
        "slug": "orion-eng",
        "enterprise": "orion",
        "group": "engineering",
        "endpoint": "http://test-ori-Alb-uYtCiM8iwUDE-1537178551.us-east-1.elb.amazonaws.com",
    },
    {
        "slug": "orion-sol",
        "enterprise": "orion",
        "group": "solutions",
        "endpoint": "http://test-ori-Alb-iWhYcfoCeuHA-164324034.us-east-1.elb.amazonaws.com",
    },
    {
        "slug": "orion-gtm",
        "enterprise": "orion",
        "group": "gtm",
        "endpoint": "http://test-ori-Alb-D7CVfG04aGRc-778844735.us-east-1.elb.amazonaws.com",
    },
    {
        "slug": "acme-eng",
        "enterprise": "acme",
        "group": "engineering",
        "endpoint": "http://test-acm-Alb-w0Eq2rO5MeVM-1954810296.us-east-1.elb.amazonaws.com",
    },
    {
        "slug": "acme-sol",
        "enterprise": "acme",
        "group": "solutions",
        "endpoint": "http://test-acm-Alb-jIOoMinF94dR-73889023.us-east-1.elb.amazonaws.com",
    },
    {
        "slug": "acme-fin",
        "enterprise": "acme",
        "group": "finance",
        "endpoint": "http://test-acm-Alb-3z1VuBmK1VDX-1994393375.us-east-1.elb.amazonaws.com",
    },
]


L2_FANOUT_TIMEOUT_SECONDS = 10.0
TOPOLOGY_CACHE_TTL_SECONDS = 3.0


# ---------------------------------------------------------------------------
# DSN signature cache (issue #23 — "routed hop" refactor).
#
# The marketing-aggregator's public DSN resolver used to fan out to every
# fleet L2 on every visitor query. The marketing copy says we do "a routed
# hop, not a fan-out" — turning that into truth means the resolver reads
# from a locally-maintained signature table that a background task fills
# on a polling cadence. That's the routing-table model the AIGRP thesis
# describes.
#
# Populated by `_signature_cache_loop` (started in `app.lifespan`) every
# DSN_CACHE_REFRESH_SECS. DSN resolve reads from here. If the cache is
# empty (cold boot) or stale (>STALE secs), the resolver falls back to
# one live `_fan_out_all` and warms the cache for the next request.
# ---------------------------------------------------------------------------

_signature_cache: dict[str, "_L2Snapshot"] = {}
_signature_cache_filled_at: float = 0.0  # monotonic; 0.0 means never
_SIGNATURE_CACHE_LOCK: asyncio.Lock | None = None  # lazily created in async context

DSN_CACHE_REFRESH_SECS = int(os.environ.get("DSN_CACHE_REFRESH_SECS", "60"))
DSN_CACHE_STALE_SECS = int(os.environ.get("DSN_CACHE_STALE_SECS", "180"))


def _signature_cache_lock() -> asyncio.Lock:
    """Create the cache lock lazily so module import doesn't require an event loop."""
    global _SIGNATURE_CACHE_LOCK  # noqa: PLW0603
    if _SIGNATURE_CACHE_LOCK is None:
        _SIGNATURE_CACHE_LOCK = asyncio.Lock()
    return _SIGNATURE_CACHE_LOCK


async def _refill_signature_cache() -> tuple[int, int]:
    """One refill pass: fan out to every fleet L2, write snapshots into cache.

    Returns ``(filled, total)`` so callers can log refresh quality.
    Failures on individual L2s are tolerated (they're omitted from the
    cache and re-tried next cycle); only the overall fan-out exception
    is logged at WARNING level.
    """
    global _signature_cache_filled_at  # noqa: PLW0603
    snapshots = await _fan_out_all(FLEET_L2S)
    async with _signature_cache_lock():
        _signature_cache.clear()
        for snap in snapshots:
            if snap.signature is not None:
                _signature_cache[snap.slug] = snap
        _signature_cache_filled_at = time.monotonic()
    return len(_signature_cache), len(FLEET_L2S)


async def _signature_cache_loop() -> None:
    """Background task: refill _signature_cache on a polling cadence.

    Lives for the lifetime of the FastAPI process, started from
    `app.lifespan`. Self-healing — any iteration's exception is logged
    and the loop continues so a transient SSM/peer outage doesn't
    permanently freeze the cache.
    """
    log = logging.getLogger("dsn-cache")
    while True:
        try:
            t0 = time.monotonic()
            filled, total = await _refill_signature_cache()
            log.info(
                "dsn cache refreshed: %d/%d L2s in %dms",
                filled,
                total,
                int((time.monotonic() - t0) * 1000),
            )
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("dsn cache refresh failed; will retry next cycle")
        await asyncio.sleep(DSN_CACHE_REFRESH_SECS)


# ---------------------------------------------------------------------------
# SSM peer-key resolution.
#
# Per-Enterprise shared secrets live at /8l-aigrp/<enterprise>/peer-key as
# SecureStrings. We pull them once at first call and cache; tests bypass
# via _PEER_KEY_OVERRIDES (monkeypatchable).
# ---------------------------------------------------------------------------

_PEER_KEY_CACHE: dict[str, str] = {}
_PEER_KEY_OVERRIDES: dict[str, str] = {}
# Negative cache: how long to back off after a failed lookup before retrying.
# Prior bug: failures cached as empty string forever, so a transient
# AccessDenied / throttle would silently dark-out the resolver until restart.
_PEER_KEY_FAIL_AT: dict[str, float] = {}
_PEER_KEY_RETRY_AFTER_SECS = 30.0


def _peer_key_for(enterprise: str) -> str:
    """Resolve the AIGRP peer key for ``enterprise`` from cache or SSM.

    Returns an empty string when the key cannot be resolved — callers
    treat that as "skip this L2" rather than failing the whole fan-out
    so a one-Enterprise SSM outage doesn't dark-out the demo for the
    other Enterprise. Failures are cached only for ``_PEER_KEY_RETRY_AFTER_SECS``
    so a transient AccessDenied or throttle self-heals on the next request
    without requiring a service restart.
    """
    if enterprise in _PEER_KEY_OVERRIDES:
        return _PEER_KEY_OVERRIDES[enterprise]
    cached = _PEER_KEY_CACHE.get(enterprise)
    if cached:
        return cached
    # Negative cache: only short-circuit if the recent failure is still warm.
    failed_at = _PEER_KEY_FAIL_AT.get(enterprise)
    if failed_at is not None and (time.time() - failed_at) < _PEER_KEY_RETRY_AFTER_SECS:
        return ""
    # Local override via env (for one-shot dev, e.g. running both L2 keys
    # under the same value during smoke tests).
    env_key = os.environ.get(f"CQ_AIGRP_PEER_KEY_{enterprise.upper()}", "")
    if env_key:
        _PEER_KEY_CACHE[enterprise] = env_key
        _PEER_KEY_FAIL_AT.pop(enterprise, None)
        return env_key
    try:
        import boto3

        ssm = boto3.client(
            "ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        resp = ssm.get_parameter(
            Name=f"/8l-aigrp/{enterprise}/peer-key",
            WithDecryption=True,
        )
        value = resp["Parameter"]["Value"]
        _PEER_KEY_CACHE[enterprise] = value
        _PEER_KEY_FAIL_AT.pop(enterprise, None)
        return value
    except Exception:
        logger.warning("failed to resolve peer key for enterprise=%s", enterprise)
        _PEER_KEY_FAIL_AT[enterprise] = time.time()
        return ""


def _admin_service_jwt() -> str:
    """Mint a short-lived admin JWT to authenticate /peers/active calls.

    Each L2 in the fleet validates this with its own ``CQ_JWT_SECRET``;
    in dev that secret is shared, in prod each L2 gets its own and we'd
    move to per-L2 service tokens — out of scope for this PR. Returns
    empty string when no secret is configured.
    """
    secret = os.environ.get("CQ_JWT_SECRET", "")
    if not secret:
        return ""
    # Username here is informational; the receiving L2 only checks
    # signature + expiry. We send "service-network-proxy" so audit logs
    # can identify the caller.
    return create_token("service-network-proxy", secret=secret, ttl_hours=1)


# ---------------------------------------------------------------------------
# Pydantic response models.
# ---------------------------------------------------------------------------


class TopologyPeerEdge(BaseModel):
    """One edge in the peer mesh — pairs a peer's L2 id with the time we last got a signature from it."""

    l2_id: str
    last_signature_at: str | None = None


class TopologyActivePersona(BaseModel):
    """An active persona on an L2 — wire shape for the frontend's topology view."""

    persona: str
    last_seen_at: str
    working_dir_hint: str | None = None
    expertise_domains: list[str] = Field(default_factory=list)


class TopologyL2(BaseModel):
    """One L2 row inside the topology aggregate.

    ``peer_count`` is null when the L2 was unreachable during the
    fan-out — the frontend renders such rows greyed-out rather than
    failing the whole topology call.
    """

    l2_id: str
    group: str
    endpoint_url: str
    ku_count: int = 0
    domain_count: int = 0
    peer_count: int | None = None
    generated_at: str | None = None
    peers: list[TopologyPeerEdge] = Field(default_factory=list)
    active_personas: list[TopologyActivePersona] = Field(default_factory=list)


class TopologyEnterprise(BaseModel):
    """Enterprise grouping — bundles the L2s that share an Enterprise."""

    enterprise: str
    l2s: list[TopologyL2]


class TopologyConsent(BaseModel):
    """Cross-Enterprise consent edge for the topology view."""

    requester_enterprise: str
    responder_enterprise: str
    requester_group: str | None = None
    responder_group: str | None = None
    policy: str
    expires_at: str | None = None


class TopologyResponse(BaseModel):
    """Aggregated network view returned by ``POST /network/topology``."""

    generated_at: str
    enterprises: list[TopologyEnterprise]
    cross_enterprise_consents: list[TopologyConsent] = Field(default_factory=list)


class DsnResolveRequest(BaseModel):
    """Request body for ``POST /network/dsn/resolve`` — the DSN search bar."""

    intent: str = Field(min_length=1)
    max_candidates: int = Field(default=5, gt=0, le=20)
    include_consented_cross_enterprise: bool = True
    # Optional caller scope for policy_if_queried decisions. Defaults
    # to ('marketing', 'public') when omitted — the public-viewer
    # scope used by 8thlayer.onezero1.ai. Internal callers can pass
    # their actual scope to get accurate policy hints.
    caller_enterprise: str = ""
    caller_group: str = ""


class DsnCandidate(BaseModel):
    """One ranked L2 candidate returned by the DSN resolver."""

    l2_id: str
    enterprise: str
    group: str
    sim_score: float
    ku_count_in_topic: int = 0
    top_domains: list[str] = Field(default_factory=list)
    expert_personas: list[str] = Field(default_factory=list)
    policy_if_queried: str
    policy_reason: str


class DsnPathStep(BaseModel):
    """One step in the DSN resolution timing breakdown.

    `cache_hit` and `cache_age_ms` are populated for the `cache_lookup`
    step so the frontend can show "served from cache · 7s old · 6 L2s"
    vs "cache miss · live fetch" honestly. -1 cache_age_ms means the
    cache has never been filled yet (first request after process boot).
    """

    step: str
    latency_ms: int
    l2_count: int | None = None
    cache_hit: bool | None = None
    cache_age_ms: int | None = None


class DsnResolveResponse(BaseModel):
    """Wire shape for ``POST /network/dsn/resolve``."""

    intent: str
    embedding_dims: int
    candidates: list[DsnCandidate]
    resolution_path: list[DsnPathStep]


class DemoScenarioRequest(BaseModel):
    """Body for ``POST /network/demo/{scenario}`` — caller identifies their own L2."""

    requester_persona: str = Field(min_length=1)
    requester_l2_slug: str = Field(min_length=1)


class TraceEvent(BaseModel):
    """One event in a demo packet trace."""

    step: int
    ts: str
    l2_id: str
    action: str
    payload_preview: str = ""
    result_summary: str = ""
    latency_ms: int = 0


class TraceFinalResult(BaseModel):
    """One row of the final knowledge result returned by a demo run."""

    summary: str
    redacted_fields: list[str] = Field(default_factory=list)
    sim_score: float = 0.0


class TraceResponse(BaseModel):
    """Wire shape for any of the three demo scenarios."""

    scenario: str
    started_at: str
    completed_at: str
    total_latency_ms: int
    trace: list[TraceEvent]
    final_results: list[TraceFinalResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Fan-out helpers.
# ---------------------------------------------------------------------------


@dataclass
class _L2Snapshot:
    """In-memory result of one L2's fan-out (peers + signature + active)."""

    slug: str
    enterprise: str
    group: str
    endpoint: str
    peers: list[dict[str, Any]] | None = None
    signature: dict[str, Any] | None = None
    active_personas: list[dict[str, Any]] | None = None
    reachable: bool = False


async def _fetch_one_l2(client: httpx.AsyncClient, l2: dict[str, str]) -> _L2Snapshot:
    """Fan out three calls to one L2 in parallel; return a snapshot.

    Errors are swallowed individually — a partial response (e.g. peers
    OK but /peers/active 401) still yields a valid snapshot with the
    rest populated. Used by the topology aggregator.
    """
    snap = _L2Snapshot(
        slug=l2["slug"],
        enterprise=l2["enterprise"],
        group=l2["group"],
        endpoint=l2["endpoint"],
    )
    peer_key = _peer_key_for(l2["enterprise"])
    admin_jwt = _admin_service_jwt()
    base = l2["endpoint"].rstrip("/")
    aigrp_headers = (
        {"authorization": f"Bearer {peer_key}"} if peer_key else {}
    )
    admin_headers = (
        {"authorization": f"Bearer {admin_jwt}"} if admin_jwt else {}
    )

    async def _peers() -> dict[str, Any] | None:
        try:
            r = await client.get(f"{base}/api/v1/aigrp/peers", headers=aigrp_headers)
            if r.status_code == 200:
                return r.json()
        except Exception:
            logger.warning("aigrp/peers fetch failed slug=%s", l2["slug"])
        return None

    async def _sig() -> dict[str, Any] | None:
        try:
            r = await client.get(
                f"{base}/api/v1/aigrp/signature", headers=aigrp_headers
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            logger.warning("aigrp/signature fetch failed slug=%s", l2["slug"])
        return None

    async def _active() -> list[dict[str, Any]] | None:
        try:
            r = await client.get(
                f"{base}/api/v1/peers/active",
                headers=admin_headers,
                params={"include_self": "true", "since_minutes": 60},
            )
            if r.status_code == 200:
                body = r.json()
                return list(body.get("active_peers", []))
        except Exception:
            logger.warning("peers/active fetch failed slug=%s", l2["slug"])
        return None

    peers_res, sig_res, active_res = await asyncio.gather(
        _peers(), _sig(), _active(), return_exceptions=False
    )
    if peers_res is not None:
        snap.peers = peers_res.get("peers", [])
        snap.reachable = True
    if sig_res is not None:
        snap.signature = sig_res
        snap.reachable = True
    if active_res is not None:
        snap.active_personas = active_res
    return snap


async def _fan_out_all(fleet: list[dict[str, str]]) -> list[_L2Snapshot]:
    """Issue parallel fan-out to every L2 in the fleet, return snapshots."""
    timeout = httpx.Timeout(L2_FANOUT_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await asyncio.gather(*[_fetch_one_l2(client, l2) for l2 in fleet])


# ---------------------------------------------------------------------------
# Topology aggregator + cache.
# ---------------------------------------------------------------------------


_TOPOLOGY_CACHE: dict[str, Any] = {"value": None, "expires_at": 0.0}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_topology(snapshots: list[_L2Snapshot], consents: list[dict[str, Any]]) -> TopologyResponse:
    """Fold L2 snapshots + consent rows into the wire shape."""
    by_enterprise: dict[str, list[TopologyL2]] = {}
    for snap in snapshots:
        sig = snap.signature or {}
        peers = snap.peers
        active = snap.active_personas or []
        l2_id = sig.get("l2_id") or f"{snap.enterprise}/{snap.group}"
        peer_edges: list[TopologyPeerEdge] = []
        if peers is not None:
            for p in peers:
                if p.get("l2_id") == l2_id:
                    continue  # skip self-row
                peer_edges.append(
                    TopologyPeerEdge(
                        l2_id=p.get("l2_id", ""),
                        last_signature_at=p.get("last_signature_at"),
                    )
                )
        active_personas = [
            TopologyActivePersona(
                persona=row.get("persona", ""),
                last_seen_at=row.get("last_seen_at", ""),
                working_dir_hint=row.get("working_dir_hint"),
                expertise_domains=list(row.get("expertise_domains") or []),
            )
            for row in active
        ]
        l2_row = TopologyL2(
            l2_id=l2_id,
            group=snap.group,
            endpoint_url=snap.endpoint,
            ku_count=int(sig.get("ku_count", 0)),
            domain_count=int(sig.get("domain_count", 0)),
            peer_count=(len(peers) if peers is not None else None),
            generated_at=sig.get("computed_at"),
            peers=peer_edges,
            active_personas=active_personas,
        )
        by_enterprise.setdefault(snap.enterprise, []).append(l2_row)

    enterprises = [
        TopologyEnterprise(enterprise=name, l2s=l2s)
        for name, l2s in sorted(by_enterprise.items())
    ]
    consent_rows = [
        TopologyConsent(
            requester_enterprise=c["requester_enterprise"],
            responder_enterprise=c["responder_enterprise"],
            requester_group=c.get("requester_group"),
            responder_group=c.get("responder_group"),
            policy=c["policy"],
            expires_at=c.get("expires_at"),
        )
        for c in consents
    ]
    return TopologyResponse(
        generated_at=_now_iso(),
        enterprises=enterprises,
        cross_enterprise_consents=consent_rows,
    )


# ---------------------------------------------------------------------------
# DSN policy decisioning.
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Defensive on length mismatch."""
    import math

    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    nb = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _decode_centroid(b64_str: str | None) -> list[float] | None:
    """Decode a base64-encoded packed-float32 centroid back into a Python list."""
    if not b64_str:
        return None
    try:
        blob = base64.b64decode(b64_str)
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))
    except Exception:
        logger.warning("failed to decode centroid")
        return None


def _decide_dsn_policy(
    *,
    caller_enterprise: str,
    caller_group: str,
    cand_enterprise: str,
    cand_group: str,
    consents: list[dict[str, Any]],
    include_consented_cross_enterprise: bool,
) -> tuple[str, str]:
    """Return (policy, reason) for a candidate L2 from the caller's POV.

    Mirrors the per-KU policy ladder in ``_decide_policy_for_ku`` but
    works at the L2-level (since DSN doesn't have per-KU
    cross_group_allowed flags to consult). The returned policy is the
    *upper bound* — actual forward-query may downgrade if the KUs are
    not cross-group-shareable.
    """
    if caller_enterprise == cand_enterprise:
        if caller_group == cand_group:
            return "full_body", "same_enterprise_same_group"
        return "summary_only", "same_enterprise_xgroup_summary"
    # Cross-Enterprise — search for a matching consent.
    for c in consents:
        if (
            c.get("requester_enterprise") == caller_enterprise
            and c.get("responder_enterprise") == cand_enterprise
        ):
            req_g = c.get("requester_group")
            resp_g = c.get("responder_group")
            if (req_g is None or req_g == caller_group) and (
                resp_g is None or resp_g == cand_group
            ):
                return c.get("policy", "summary_only"), "cross_enterprise_consent"
    if include_consented_cross_enterprise:
        return "denied", "cross_enterprise_no_consent"
    return "denied", "boundary"


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/network", tags=["network"])


@router.post("/topology", response_model=TopologyResponse)
@router.get("/topology", response_model=TopologyResponse)
async def network_topology(
    store: RemoteStore = Depends(get_store),
) -> TopologyResponse:
    """Aggregate per-L2 metadata + consent edges into the topology view.

    Public read — no auth. Returns only operator-authorized topology
    metadata (L2 names, KU counts, peer table, declared-discoverable
    presence rows, active consent edges). The data is intended for the
    public marketing site at 8thlayer.onezero1.ai.

    Cached in-process for ``TOPOLOGY_CACHE_TTL_SECONDS`` to damp the
    frontend's 5s poll loop. Per-L2 failures are tolerated (rendered
    as ``peer_count=null`` rows).
    """
    now = time.monotonic()
    cached = _TOPOLOGY_CACHE.get("value")
    if cached is not None and _TOPOLOGY_CACHE.get("expires_at", 0.0) > now:
        return cached  # type: ignore[no-any-return]

    snapshots = await _fan_out_all(FLEET_L2S)
    consents = store.list_cross_enterprise_consents(
        include_expired=False, now_iso=_now_iso(), limit=200
    )
    response = _build_topology(snapshots, consents)
    _TOPOLOGY_CACHE["value"] = response
    _TOPOLOGY_CACHE["expires_at"] = now + TOPOLOGY_CACHE_TTL_SECONDS
    return response


def _resolve_caller_scope(store: RemoteStore, username: str) -> tuple[str, str]:
    """Pull the caller's (enterprise_id, group_id) from their user row."""
    user = store.get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return str(user.get("enterprise_id") or ""), str(user.get("group_id") or "")


@router.post("/dsn/resolve", response_model=DsnResolveResponse)
@router.get("/dsn/resolve", response_model=DsnResolveResponse)
async def network_dsn_resolve(
    request: DsnResolveRequest,
    store: RemoteStore = Depends(get_store),
) -> DsnResolveResponse:
    """Embed the caller's intent and rank fleet L2s by topical similarity.

    Public read — no auth. The ranking is purely centroid-based; no KU
    bodies leave the queried L2s. Each candidate carries the policy
    that *would* be applied if the caller called
    ``/aigrp/forward-query`` against it, so the frontend can pre-render
    boundary edges. Caller scope defaults to ('marketing', 'public')
    when not provided — that's the public-viewer scope used by
    8thlayer.onezero1.ai. Internal demo callers can pass their actual
    scope via ``caller_enterprise`` / ``caller_group``.
    """
    caller_ent = request.caller_enterprise or "marketing"
    caller_grp = request.caller_group or "public"

    path: list[DsnPathStep] = []

    t0 = time.monotonic()
    payload = embed_text(request.intent)
    embed_ms = int((time.monotonic() - t0) * 1000)
    if payload is None:
        raise HTTPException(status_code=503, detail="embedding unavailable")
    intent_vec = unpack(payload[0])
    path.append(DsnPathStep(step="embed", latency_ms=embed_ms))

    # Routed-hop read (issue #23): consult the locally-maintained signature
    # cache instead of fanning out to every fleet L2 per request. The cache
    # is filled by `_signature_cache_loop` every DSN_CACHE_REFRESH_SECS
    # (default 60s). On cold boot or stale cache we fall back to one live
    # fan-out and warm — that's marked cache_hit=False in the trace so
    # the frontend can show "cache miss · live fetch" honestly.
    t1 = time.monotonic()
    async with _signature_cache_lock():
        cached_snapshots = list(_signature_cache.values())
        cache_age_ms = (
            int((time.monotonic() - _signature_cache_filled_at) * 1000)
            if _signature_cache_filled_at
            else -1
        )
    cache_hit = (
        len(cached_snapshots) > 0
        and 0 <= cache_age_ms <= DSN_CACHE_STALE_SECS * 1000
    )
    if cache_hit:
        snapshots = cached_snapshots
    else:
        # Cold start or stale: warm the cache via one live fan-out.
        await _refill_signature_cache()
        async with _signature_cache_lock():
            snapshots = list(_signature_cache.values())
            cache_age_ms = int((time.monotonic() - _signature_cache_filled_at) * 1000)
    lookup_ms = int((time.monotonic() - t1) * 1000)
    path.append(
        DsnPathStep(
            step="cache_lookup",
            latency_ms=lookup_ms,
            l2_count=len(snapshots),
            cache_hit=cache_hit,
            cache_age_ms=cache_age_ms,
        )
    )

    t2 = time.monotonic()
    consents = store.list_cross_enterprise_consents(
        include_expired=False, now_iso=_now_iso(), limit=200
    )

    candidates: list[DsnCandidate] = []
    for snap in snapshots:
        sig = snap.signature or {}
        centroid = _decode_centroid(sig.get("embedding_centroid_b64"))
        sim = _cosine(intent_vec, centroid) if centroid else 0.0
        policy, reason = _decide_dsn_policy(
            caller_enterprise=caller_ent,
            caller_group=caller_grp,
            cand_enterprise=snap.enterprise,
            cand_group=snap.group,
            consents=consents,
            include_consented_cross_enterprise=request.include_consented_cross_enterprise,
        )
        if policy == "denied" and not request.include_consented_cross_enterprise and reason == "boundary":
            continue
        l2_id = sig.get("l2_id") or f"{snap.enterprise}/{snap.group}"
        active = snap.active_personas or []
        expert_personas = [
            p.get("persona", "") for p in active if p.get("discoverable") is not False
        ][:5]
        candidates.append(
            DsnCandidate(
                l2_id=l2_id,
                enterprise=snap.enterprise,
                group=snap.group,
                sim_score=round(sim, 4),
                ku_count_in_topic=int(sig.get("ku_count", 0)),
                top_domains=[],
                expert_personas=expert_personas,
                policy_if_queried=policy,
                policy_reason=reason,
            )
        )

    candidates.sort(key=lambda c: c.sim_score, reverse=True)
    candidates = candidates[: request.max_candidates]
    rank_ms = int((time.monotonic() - t2) * 1000)
    path.append(DsnPathStep(step="rank", latency_ms=rank_ms))

    return DsnResolveResponse(
        intent=request.intent,
        embedding_dims=len(intent_vec),
        candidates=candidates,
        resolution_path=path,
    )


# ---------------------------------------------------------------------------
# Demo orchestration.
# ---------------------------------------------------------------------------


_DEMO_INTENTS: dict[str, str] = {
    "cross-group-query": "CloudFront cache invalidation gotchas",
    "cross-enterprise-blocked": "Bedrock Titan embedding throughput tuning",
    "cross-enterprise-consented": "Bedrock Titan embedding throughput tuning",
}


def _l2_by_slug(slug: str) -> dict[str, str] | None:
    """Resolve a fleet slug to the FLEET_L2S row, or None."""
    for row in FLEET_L2S:
        if row["slug"] == slug:
            return row
    return None


def _trace_event(
    step: int,
    *,
    l2_id: str,
    action: str,
    payload_preview: str = "",
    result_summary: str = "",
    latency_ms: int = 0,
) -> TraceEvent:
    """Build one trace event with a stamped timestamp."""
    return TraceEvent(
        step=step,
        ts=_now_iso(),
        l2_id=l2_id,
        action=action,
        payload_preview=payload_preview,
        result_summary=result_summary,
        latency_ms=latency_ms,
    )


async def _call_aigrp_lookup(
    client: httpx.AsyncClient,
    l2: dict[str, str],
    *,
    intent: str,
    persona: str,
) -> tuple[dict[str, Any] | None, int]:
    """Helper: call /aigrp/lookup on an L2; return (body, latency_ms)."""
    base = l2["endpoint"].rstrip("/")
    peer_key = _peer_key_for(l2["enterprise"])
    headers = {"authorization": f"Bearer {peer_key}"} if peer_key else {}
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{base}/api/v1/aigrp/lookup",
            headers=headers,
            json={
                "context": intent,
                "trigger": "demo",
                "persona": persona,
                "max_results": 5,
            },
        )
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return r.json(), latency
    except Exception:
        logger.warning("aigrp/lookup failed slug=%s", l2["slug"])
    return None, int((time.monotonic() - t0) * 1000)


async def _call_forward_query(
    client: httpx.AsyncClient,
    target: dict[str, str],
    *,
    requester: dict[str, str],
    requester_persona: str,
    query_vec: list[float],
    query_text: str,
) -> tuple[dict[str, Any] | None, int]:
    """Helper: call /aigrp/forward-query on the target L2 with the requester's scope."""
    base = target["endpoint"].rstrip("/")
    # forward-query uses the *target*'s peer key (caller signs with the
    # responder's key in this internal-fleet topology). For cross-Enterprise
    # the responder has its own key, which is why we resolve by target.
    peer_key = _peer_key_for(target["enterprise"])
    headers = {"authorization": f"Bearer {peer_key}"} if peer_key else {}
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{base}/api/v1/aigrp/forward-query",
            headers=headers,
            json={
                "query_vec": query_vec,
                "query_text": query_text,
                "requester_l2_id": f"{requester['enterprise']}/{requester['group']}",
                "requester_enterprise": requester["enterprise"],
                "requester_group": requester["group"],
                "requester_persona": requester_persona,
                "max_results": 5,
            },
        )
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return r.json(), latency
    except Exception:
        logger.warning("forward-query failed slug=%s", target["slug"])
    return None, int((time.monotonic() - t0) * 1000)


async def _resolve_dsn_internal(
    intent: str, caller_enterprise: str, caller_group: str, store: RemoteStore
) -> tuple[DsnResolveResponse | None, list[float], int]:
    """Inline DSN resolve used by demo scenarios. Returns (response, intent_vec, dsn_ms)."""
    t0 = time.monotonic()
    payload = embed_text(intent)
    if payload is None:
        return None, [], int((time.monotonic() - t0) * 1000)
    intent_vec = unpack(payload[0])
    snapshots = await _fan_out_all(FLEET_L2S)
    consents = store.list_cross_enterprise_consents(
        include_expired=False, now_iso=_now_iso(), limit=200
    )
    candidates: list[DsnCandidate] = []
    for snap in snapshots:
        sig = snap.signature or {}
        centroid = _decode_centroid(sig.get("embedding_centroid_b64"))
        sim = _cosine(intent_vec, centroid) if centroid else 0.0
        policy, reason = _decide_dsn_policy(
            caller_enterprise=caller_enterprise,
            caller_group=caller_group,
            cand_enterprise=snap.enterprise,
            cand_group=snap.group,
            consents=consents,
            include_consented_cross_enterprise=True,
        )
        l2_id = sig.get("l2_id") or f"{snap.enterprise}/{snap.group}"
        candidates.append(
            DsnCandidate(
                l2_id=l2_id,
                enterprise=snap.enterprise,
                group=snap.group,
                sim_score=round(sim, 4),
                ku_count_in_topic=int(sig.get("ku_count", 0)),
                top_domains=[],
                expert_personas=[],
                policy_if_queried=policy,
                policy_reason=reason,
            )
        )
    candidates.sort(key=lambda c: c.sim_score, reverse=True)
    response = DsnResolveResponse(
        intent=intent,
        embedding_dims=len(intent_vec),
        candidates=candidates,
        resolution_path=[],
    )
    return response, intent_vec, int((time.monotonic() - t0) * 1000)


def _final_results_from_forward(body: dict[str, Any] | None) -> list[TraceFinalResult]:
    """Translate a forward-query response body into the TraceFinalResult list."""
    if not body:
        return []
    out: list[TraceFinalResult] = []
    for hit in body.get("results", []):
        out.append(
            TraceFinalResult(
                summary=hit.get("summary", ""),
                redacted_fields=list(hit.get("redacted_fields") or []),
                sim_score=float(hit.get("sim_score", 0.0)),
            )
        )
    return out


@router.post("/demo/{scenario}", response_model=TraceResponse)
@router.get("/demo/{scenario}", response_model=TraceResponse)
async def network_demo(
    scenario: str,
    request: DemoScenarioRequest,
    store: RemoteStore = Depends(get_store),
) -> TraceResponse:
    """Run one of three named scenarios end-to-end against the live fleet.

    The trace events returned are what the frontend animates as a
    packet flow over the topology canvas. Each step is a real HTTP
    call to a real L2 — the caller persona/L2 is taken from the
    request body so a single demo shell can drive multiple personas.
    """
    if scenario not in _DEMO_INTENTS:
        raise HTTPException(status_code=404, detail=f"unknown scenario: {scenario}")
    requester = _l2_by_slug(request.requester_l2_slug)
    if requester is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown requester_l2_slug={request.requester_l2_slug!r}",
        )

    started_at = _now_iso()
    started_mono = time.monotonic()
    intent = _DEMO_INTENTS[scenario]
    trace: list[TraceEvent] = []
    final_results: list[TraceFinalResult] = []

    # Pre-flight gate for the consented variant: a row must exist or we
    # 412 so the demo button can show "sign consent first".
    if scenario == "cross-enterprise-consented":
        consent = store.find_cross_enterprise_consent(
            requester_enterprise=requester["enterprise"],
            responder_enterprise=("orion" if requester["enterprise"] == "acme" else "acme"),
            requester_group=requester["group"],
            responder_group="engineering",
            now_iso=_now_iso(),
        )
        if consent is None:
            raise HTTPException(
                status_code=412,
                detail={
                    "error": "no_consent",
                    "hint": "POST /api/v1/consents/sign first",
                },
            )

    timeout = httpx.Timeout(L2_FANOUT_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1 — local /aigrp/lookup against the requester's own L2.
        lookup_body, lookup_ms = await _call_aigrp_lookup(
            client, requester, intent=intent, persona=request.requester_persona
        )
        local_hits = len((lookup_body or {}).get("results", []))
        trace.append(
            _trace_event(
                1,
                l2_id=f"{requester['enterprise']}/{requester['group']}",
                action="aigrp_lookup",
                payload_preview=f"intent={intent!r}",
                result_summary=(
                    f"{local_hits} local hits; routing via DSN to find expert L2"
                ),
                latency_ms=lookup_ms,
            )
        )

        # Step 2 — DSN resolve.
        dsn_resp, intent_vec, dsn_ms = await _resolve_dsn_internal(
            intent, requester["enterprise"], requester["group"], store
        )
        if dsn_resp is None or not intent_vec:
            raise HTTPException(status_code=503, detail="embedding unavailable")
        # Pick top non-self candidate.
        self_l2_id = f"{requester['enterprise']}/{requester['group']}"
        top: DsnCandidate | None = None
        for c in dsn_resp.candidates:
            if c.l2_id == self_l2_id:
                continue
            # Scenario-specific routing constraints — keep the demo
            # deterministic by only selecting the expected target type.
            if scenario == "cross-group-query" and c.enterprise != requester["enterprise"]:
                continue
            if scenario.startswith("cross-enterprise") and c.enterprise == requester["enterprise"]:
                continue
            top = c
            break
        if top is None:
            raise HTTPException(
                status_code=503,
                detail=f"no candidate L2 found for scenario={scenario}",
            )
        trace.append(
            _trace_event(
                2,
                l2_id="dsn",
                action="dsn_resolve",
                payload_preview=f"intent={intent!r}",
                result_summary=(
                    f"top candidate={top.l2_id} sim={top.sim_score:.3f} "
                    f"policy={top.policy_if_queried} reason={top.policy_reason}"
                ),
                latency_ms=dsn_ms,
            )
        )

        # Step 3 — forward-query the chosen target.
        target_slug_map = {
            "orion/engineering": "orion-eng",
            "orion/solutions": "orion-sol",
            "orion/gtm": "orion-gtm",
            "acme/engineering": "acme-eng",
            "acme/solutions": "acme-sol",
            "acme/finance": "acme-fin",
        }
        target_slug = target_slug_map.get(top.l2_id)
        target = _l2_by_slug(target_slug) if target_slug else None
        if target is None:
            raise HTTPException(
                status_code=503, detail=f"no fleet row for l2_id={top.l2_id}"
            )
        fq_body, fq_ms = await _call_forward_query(
            client,
            target,
            requester=requester,
            requester_persona=request.requester_persona,
            query_vec=intent_vec,
            query_text=intent,
        )
        result_count = len((fq_body or {}).get("results", []))
        policy_applied = (fq_body or {}).get("policy_applied", "denied")
        trace.append(
            _trace_event(
                3,
                l2_id=top.l2_id,
                action="aigrp_forward_query",
                payload_preview=(
                    f"requester={requester['enterprise']}/{requester['group']} "
                    f"persona={request.requester_persona}"
                ),
                result_summary=(
                    f"{result_count} hits returned; policy_applied={policy_applied}"
                ),
                latency_ms=fq_ms,
            )
        )
        final_results = _final_results_from_forward(fq_body)

    completed_at = _now_iso()
    total_ms = int((time.monotonic() - started_mono) * 1000)
    return TraceResponse(
        scenario=scenario,
        started_at=started_at,
        completed_at=completed_at,
        total_latency_ms=total_ms,
        trace=trace,
        final_results=final_results,
    )
