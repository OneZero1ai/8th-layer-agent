"""Cross-L2 AIGRP federation — outbound forward-query client (agent#316).

The receiving side of cross-L2 federation (``POST /aigrp/forward-query``)
has existed since Phase 6 step 2. What was missing is the *outbound* leg:
nothing actually issued a forward-query to a sibling L2. This module is
that leg.

``aigrp_lookup`` (app.py) calls :func:`fan_out_forward_query` after its
local ``semantic_query``: it fans the signed forward-query at every
sibling peer in the same Enterprise concurrently, merges the remote hits
into the local result set, and re-ranks the union by similarity.

Design constraints (from the issue):

- **Non-blocking.** A peer that errors or times out must never fail the
  whole lookup — it is logged and skipped. The lookup degrades to
  local-only results.
- **Pair-secret auth intact.** Every outbound call carries the per-pair
  HKDF bearer (``_aigrp_outbound_bearer``) and the
  ``X-8L-Forwarder-L2-Id`` identity header, exactly what the receiver's
  ``require_peer_key`` / ``require_forwarder_identity`` expect.
- **Intra-Enterprise only.** The caller passes only same-Enterprise
  peers; the receiver's existing cross-Enterprise consent gate is
  untouched.
- **Bloom prefilter.** Peers whose ``domain_bloom`` matches none of the
  query's domain tags are skipped before the HTTP call — their corpus
  provably cannot hold a relevant KU. A query with no domain tags
  queries all peers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from . import _legacy

log = logging.getLogger("aigrp.federation")

# Bounded timeouts. A connect failure is fast; a slow read is the more
# common degradation, so it gets the larger budget. Either way the
# fan-out caller wraps every call in ``return_exceptions=True`` so a
# breach degrades to local-only, never an error.
_CONNECT_TIMEOUT_SEC = 3.0
_READ_TIMEOUT_SEC = 8.0


class RemoteHit:
    """One KU returned by a sibling L2's forward-query.

    Mirrors the fields ``aigrp_lookup`` needs to merge a remote hit into
    its local result set and apply the ``min_similarity`` /
    ``min_confidence`` filters. ``detail`` / ``action`` are ``None`` when
    the peer applied ``summary_only`` policy (cross-group); ``summary``
    and ``confidence`` are always present.
    """

    __slots__ = (
        "ku_id",
        "summary",
        "detail",
        "action",
        "domains",
        "similarity",
        "confidence",
        "created_by",
        "peer_l2_id",
        "policy_applied",
    )

    def __init__(
        self,
        *,
        ku_id: str,
        summary: str,
        detail: str | None,
        action: str | None,
        domains: list[str],
        similarity: float,
        confidence: float,
        created_by: str,
        peer_l2_id: str,
        policy_applied: str,
    ) -> None:
        """Build a RemoteHit from a peer's parsed forward-query result."""
        self.ku_id = ku_id
        self.summary = summary
        self.detail = detail
        self.action = action
        self.domains = domains
        self.similarity = similarity
        self.confidence = confidence
        self.created_by = created_by
        self.peer_l2_id = peer_l2_id
        self.policy_applied = policy_applied


def select_peers_for_query(
    peers: Iterable[dict[str, Any]],
    query_domains: Iterable[str],
) -> list[dict[str, Any]]:
    """Filter the peer list down to the ones worth a forward-query.

    Drops:

    - this L2 itself (``l2_id == self_l2_id``),
    - stub peers with no ``endpoint_url`` (consumer-only, can't be
      queried),
    - peers whose ``domain_bloom`` matches none of ``query_domains``
      (Bloom prefilter — their corpus can't hold a relevant KU).

    The Bloom prefilter is skipped — every non-stub peer is kept — when
    ``query_domains`` is empty: with no domain tags there is nothing to
    test the Bloom against, so fanning at all peers is correct.
    """
    self_id = _legacy.self_l2_id()
    domains = [d for d in (query_domains or []) if d]
    selected: list[dict[str, Any]] = []
    for peer in peers:
        if peer.get("l2_id") == self_id:
            continue
        if not peer.get("endpoint_url"):
            continue
        bloom = peer.get("domain_bloom")
        if domains and bloom and not _legacy.bloom_matches_any(bloom, domains):
            log.debug(
                "aigrp federation: Bloom prefilter skipped peer %s (no domain overlap)",
                peer.get("l2_id"),
            )
            continue
        selected.append(peer)
    return selected


async def forward_query_peer(
    peer: dict[str, Any],
    *,
    query_vec: list[float],
    query_text: str,
    requester_l2_id: str,
    requester_enterprise: str,
    requester_group: str,
    requester_persona: str,
    max_results: int,
    bearer_resolver: Callable[[str | None], str],
    client: httpx.AsyncClient | None = None,
) -> list[RemoteHit]:
    """Issue a single ``POST /aigrp/forward-query`` to one sibling peer.

    ``peer`` is one row from ``store.list_aigrp_peers``. ``bearer_resolver``
    is ``app._aigrp_outbound_bearer`` — given the peer's ``l2_id`` it
    returns the per-pair HKDF bearer (or the legacy env bearer as
    fallback). Injected rather than imported to keep this module free of
    an ``app`` import cycle.

    Returns the peer's hits as :class:`RemoteHit` objects. On any error —
    timeout, connection failure, non-200, malformed body — logs a warning
    and returns ``[]`` so the caller's fan-out degrades to local-only for
    this peer rather than failing.
    """
    peer_l2_id = peer.get("l2_id") or ""
    endpoint = (peer.get("endpoint_url") or "").rstrip("/")
    if not endpoint:
        return []

    url = f"{endpoint}/api/v1/aigrp/forward-query"
    body = {
        "query_vec": query_vec,
        "query_text": query_text,
        "requester_l2_id": requester_l2_id,
        "requester_enterprise": requester_enterprise,
        "requester_group": requester_group,
        "requester_persona": requester_persona,
        "max_results": max_results,
    }
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {bearer_resolver(peer_l2_id)}",
        _legacy.FORWARDER_HEADER: requester_l2_id,
    }
    timeout = httpx.Timeout(_READ_TIMEOUT_SEC, connect=_CONNECT_TIMEOUT_SEC)

    try:
        if client is not None:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                resp = await owned.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort: degrade to local-only.
        log.warning(
            "aigrp federation: forward-query to peer %s (%s) failed: %s",
            peer_l2_id,
            url,
            exc,
        )
        return []

    policy = str(data.get("policy_applied") or "")
    hits: list[RemoteHit] = []
    for raw in data.get("results", []) or []:
        try:
            hits.append(
                RemoteHit(
                    ku_id=str(raw["ku_id"]),
                    summary=str(raw.get("summary") or ""),
                    detail=raw.get("detail"),
                    action=raw.get("action"),
                    domains=list(raw.get("domains") or []),
                    similarity=float(raw.get("sim_score") or 0.0),
                    # confidence is agent#316 — older peers omit it; a
                    # missing value defaults to 0.0 and will be dropped
                    # by any non-zero min_confidence filter, which is the
                    # safe direction (don't surface unscored remote KUs).
                    confidence=float(raw.get("confidence") or 0.0),
                    created_by=str(raw.get("created_by") or ""),
                    peer_l2_id=peer_l2_id,
                    policy_applied=policy,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "aigrp federation: skipping malformed hit from peer %s: %s",
                peer_l2_id,
                exc,
            )
    return hits


async def fan_out_forward_query(
    peers: Iterable[dict[str, Any]],
    *,
    query_vec: list[float],
    query_text: str,
    query_domains: Iterable[str],
    requester_l2_id: str,
    requester_enterprise: str,
    requester_group: str,
    requester_persona: str,
    max_results: int,
    bearer_resolver: Callable[[str | None], str],
) -> list[RemoteHit]:
    """Fan a forward-query at every eligible sibling peer concurrently.

    Applies the Bloom prefilter (:func:`select_peers_for_query`), then
    issues all forward-queries in parallel with ``asyncio.gather`` /
    ``return_exceptions=True``. A peer that raises is logged and
    contributes no hits; the merged list from the survivors is returned
    flat (caller re-ranks + filters).
    """
    import asyncio

    selected = select_peers_for_query(peers, query_domains)
    if not selected:
        return []

    timeout = httpx.Timeout(_READ_TIMEOUT_SEC, connect=_CONNECT_TIMEOUT_SEC)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [
            forward_query_peer(
                peer,
                query_vec=query_vec,
                query_text=query_text,
                requester_l2_id=requester_l2_id,
                requester_enterprise=requester_enterprise,
                requester_group=requester_group,
                requester_persona=requester_persona,
                max_results=max_results,
                bearer_resolver=bearer_resolver,
                client=client,
            )
            for peer in selected
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[RemoteHit] = []
    for peer, result in zip(selected, results, strict=True):
        if isinstance(result, BaseException):
            log.warning(
                "aigrp federation: peer %s fan-out task raised: %s",
                peer.get("l2_id"),
                result,
            )
            continue
        merged.extend(result)
    return merged
