"""8th-Layer Directory client — sprint 3.

Cq-server's L2-side adapter for the public directory at
``directory.8thlayer.onezero1.ai``. Per ``decisions/11`` and
``specs/directory-v1.md``:

- **Announce-on-startup**: when ``CQ_DIRECTORY_ENABLED=true``, the L2
  signs an `/announce` envelope with the enterprise root key on
  startup and POSTs it. 201 → first contact. 200 → record updated.
- **Peering pull loop**: every ``CQ_DIRECTORY_PULL_INTERVAL_SEC``
  (default 1 hour), GET ``/peerings/{enterprise_id}`` with a signed
  empty-payload envelope as proof-of-identity, verify each returned
  peering record's BOTH signatures (offer + accept) against the
  ``/enterprises/{id}/key`` endpoint, and write-through to the local
  ``aigrp_directory_peerings`` table.

The directory is **never** in the data path. Once a peering is pulled
and verified, /aigrp/forward-query and /consults/forward-* talk
directly L2-to-L2 using the agreed peering key.

Design choices:

- We re-use ``rfc8785``'s JCS canonicalization so the bytes the
  directory verifies are byte-identical to what we sign.
- Ed25519 keypair source is filesystem-mount in v1
  (``CQ_ENTERPRISE_ROOT_PRIVKEY_PATH``); HSM/KMS-backed signing is
  v2.
- Pull-loop failures are logged but never crash the server — the
  directory is best-effort. Local routing falls back to whatever
  peerings were pulled on the last success.
- All work runs as asyncio background tasks attached to the FastAPI
  lifespan. Cancellation is graceful.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import (
    b64u as _b64u,
)
from .crypto import (
    b64u_decode as _b64u_decode,
)
from .crypto import (
    canonicalize,
    fingerprint_sha256,
    load_private_key,
    public_key_b64u,
    sign_envelope,
    verify_envelope_signature,
)
from .store import RemoteStore

# Re-export for callers and tests that still import these from
# ``cq_server.directory_client`` (sprint-3 surface). The canonical
# definitions now live in ``cq_server.crypto``.
__all__ = [
    "_b64u",
    "_b64u_decode",
    "canonicalize",
    "directory_bootstrap_and_loop",
    "directory_enabled",
    "directory_url",
    "publish_reputation_root",
    "reputation_publish_loop",
    "fingerprint_sha256",
    "load_private_key",
    "now_iso",
    "public_key_b64u",
    "pull_interval_sec",
    "sign_envelope",
    "verify_envelope_signature",
]

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_h)

# Public directory base URL. Override in tests via env.
DEFAULT_DIRECTORY_URL = "https://directory.8thlayer.onezero1.ai"
DEFAULT_PULL_INTERVAL_SEC = 3600
ANNOUNCE_RETRY_BASE_SEC = 30
ANNOUNCE_RETRY_MAX_SEC = 600


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def directory_enabled() -> bool:
    return os.environ.get("CQ_DIRECTORY_ENABLED", "false").lower() == "true"


def skip_announce() -> bool:
    """Pull-only mode: skip announce-on-startup, only run the pull loop.

    Used when an enterprise's roster is managed out-of-band by an admin
    via the ``8l-directory announce`` CLI (run from the operator's
    workstation with the enterprise root key, never on the L2 itself).
    The L2 still needs to pull peerings so it can authorize incoming
    cross-Enterprise forwards — that's all this mode does.

    When ``true``, the L2 doesn't need ``CQ_ENTERPRISE_ROOT_PRIVKEY_PATH``
    or ``CQ_DIRECTORY_CONTACT_EMAIL`` set, since neither is read by the
    pull loop (peerings GET is public-read since 8th-layer-directory#1).
    """
    return os.environ.get("CQ_DIRECTORY_SKIP_ANNOUNCE", "false").lower() == "true"


def directory_url() -> str:
    return os.environ.get("CQ_DIRECTORY_URL", DEFAULT_DIRECTORY_URL).rstrip("/")


def pull_interval_sec() -> int:
    try:
        return int(os.environ.get("CQ_DIRECTORY_PULL_INTERVAL_SEC", str(DEFAULT_PULL_INTERVAL_SEC)))
    except ValueError:
        return DEFAULT_PULL_INTERVAL_SEC


# ---------------------------------------------------------------------------
# Crypto helpers — moved to ``cq_server.crypto`` in sprint 4. Imported above
# and re-exported via ``__all__`` so existing callers keep working.
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _post_announce(
    client: httpx.AsyncClient,
    privkey: Ed25519PrivateKey,
    enterprise_id: str,
    display_name: str,
    visibility: str,
    contact_email: str,
    l2_endpoints: list[dict[str, Any]],
    discoverable_topics: list[str],
) -> tuple[int, dict[str, Any] | None]:
    """POST /announce. Returns (status_code, response_body | None on error)."""
    payload = {
        "enterprise_id": enterprise_id,
        "display_name": display_name,
        "visibility": visibility,
        "root_pubkey": public_key_b64u(privkey),
        "l2_endpoints": l2_endpoints,
        "discoverable_topics": discoverable_topics,
        "contact_email": contact_email,
        "announce_ts": now_iso(),
    }
    envelope = sign_envelope(privkey, payload)
    url = f"{directory_url()}/api/v1/directory/announce"
    try:
        r = await client.post(url, json=envelope, timeout=10.0)
    except httpx.RequestError as e:
        log.warning("directory: announce failed (network) err=%s", e)
        return 0, None
    if r.status_code in (200, 201):
        log.info(
            "directory: announce ok enterprise=%s status=%d (%s)",
            enterprise_id,
            r.status_code,
            "first" if r.status_code == 201 else "update",
        )
        return r.status_code, r.json()
    log.warning(
        "directory: announce rejected enterprise=%s status=%d body=%s",
        enterprise_id,
        r.status_code,
        r.text[:200],
    )
    return r.status_code, None


async def _announce_with_retries(
    privkey: Ed25519PrivateKey,
    enterprise_id: str,
    display_name: str,
    visibility: str,
    contact_email: str,
    l2_endpoints: list[dict[str, Any]],
    discoverable_topics: list[str],
    max_attempts: int = 6,
) -> bool:
    """Announce with exponential backoff. Returns True on success."""
    delay = ANNOUNCE_RETRY_BASE_SEC
    async with httpx.AsyncClient() as client:
        for attempt in range(1, max_attempts + 1):
            status, _body = await _post_announce(
                client,
                privkey,
                enterprise_id,
                display_name,
                visibility,
                contact_email,
                l2_endpoints,
                discoverable_topics,
            )
            if status in (200, 201):
                return True
            # 4xx that isn't auth/timing → permanent; stop retrying.
            if status in (400, 409, 422):
                log.error(
                    "directory: announce permanent failure status=%d; not retrying",
                    status,
                )
                return False
            log.info("directory: announce retry %d/%d in %ds", attempt, max_attempts, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, ANNOUNCE_RETRY_MAX_SEC)
    return False


async def _fetch_enterprise_pubkey(client: httpx.AsyncClient, enterprise_id: str, _cache: dict[str, str]) -> str | None:
    """Resolve an enterprise's root pubkey via /enterprises/{id}/key.

    Cached per process for the life of the pull loop; cache flushed on
    each pull cycle (a key rotation that landed mid-cycle is picked up
    on the next cycle).
    """
    if enterprise_id in _cache:
        return _cache[enterprise_id]
    url = f"{directory_url()}/api/v1/directory/enterprises/{enterprise_id}/key"
    try:
        r = await client.get(url, timeout=10.0)
    except httpx.RequestError as e:
        log.warning("directory: pubkey fetch failed enterprise=%s err=%s", enterprise_id, e)
        return None
    if r.status_code != 200:
        log.warning(
            "directory: pubkey fetch unexpected status=%d enterprise=%s",
            r.status_code,
            enterprise_id,
        )
        return None
    pubkey = r.json().get("root_pubkey", "")
    if not pubkey:
        return None
    _cache[enterprise_id] = pubkey
    return pubkey


def _verify_peering_record(
    record: dict[str, Any],
    initiator_pubkey: str,
    responder_pubkey: str,
) -> bool:
    """Verify both signatures (offer + accept) on a peering record."""
    offer_ok = verify_envelope_signature(
        initiator_pubkey,
        record["offer_payload_canonical"],
        record["offer_signature"],
    )
    accept_ok = verify_envelope_signature(
        responder_pubkey,
        record["accept_payload_canonical"],
        record["accept_signature"],
    )
    return offer_ok and accept_ok


async def _post_peerings_pull(
    client: httpx.AsyncClient,
    privkey: Ed25519PrivateKey | None,
    enterprise_id: str,
) -> list[dict[str, Any]] | None:
    """Pull /peerings/{enterprise_id}. No auth in V1.

    Peering records are bilateral-signed by the two enterprises' root
    keys; the records ARE publicly verifiable offline by anyone with
    the enterprise public keys. Privacy of the peering graph is
    deferred to V2. The privkey parameter is retained for backward
    compatibility (and future-proofing if V2 adds per-pair bearers);
    silently unused on the wire today. ``privkey=None`` is valid when
    the L2 is in skip-announce / pull-only mode.
    """
    del privkey  # V1: no auth on this endpoint; CloudFront-strip-body forced this.
    url = f"{directory_url()}/api/v1/directory/peerings/{enterprise_id}"
    try:
        r = await client.get(url, timeout=15.0)
    except httpx.RequestError as e:
        log.warning("directory: peerings pull failed enterprise=%s err=%s", enterprise_id, e)
        return None
    if r.status_code != 200:
        log.warning(
            "directory: peerings pull unexpected status=%d enterprise=%s",
            r.status_code,
            enterprise_id,
        )
        return None
    return r.json().get("peerings", [])


async def _pull_and_persist_once(
    privkey: Ed25519PrivateKey | None,
    enterprise_id: str,
    store: RemoteStore,
) -> int:
    """One pull cycle. Returns number of peerings persisted."""
    pubkey_cache: dict[str, str] = {}
    persisted = 0
    async with httpx.AsyncClient() as client:
        records = await _post_peerings_pull(client, privkey, enterprise_id)
        if records is None:
            return 0

        for rec in records:
            initiator_pubkey = await _fetch_enterprise_pubkey(client, rec["from_enterprise"], pubkey_cache)
            responder_pubkey = await _fetch_enterprise_pubkey(client, rec["to_enterprise"], pubkey_cache)
            if initiator_pubkey is None or responder_pubkey is None:
                log.warning(
                    "directory: missing pubkey from=%s to=%s offer=%s — skipping",
                    rec["from_enterprise"],
                    rec["to_enterprise"],
                    rec.get("offer_id"),
                )
                continue
            if not _verify_peering_record(rec, initiator_pubkey, responder_pubkey):
                log.warning(
                    "directory: peering signature INVALID offer=%s — skipping",
                    rec.get("offer_id"),
                )
                continue

            store.upsert_directory_peering(
                offer_id=rec["offer_id"],
                from_enterprise=rec["from_enterprise"],
                to_enterprise=rec["to_enterprise"],
                status=rec["status"],
                content_policy=rec["content_policy"],
                consult_logging_policy=rec["consult_logging_policy"],
                topic_filters_json=json.dumps(rec.get("topic_filters") or []),
                active_from=rec.get("active_from"),
                expires_at=rec["expires_at"],
                offer_payload_canonical=rec["offer_payload_canonical"],
                offer_signature_b64u=rec["offer_signature"],
                offer_signing_key_id=rec["offer_signing_key_id"],
                accept_payload_canonical=rec["accept_payload_canonical"],
                accept_signature_b64u=rec["accept_signature"],
                accept_signing_key_id=rec["accept_signing_key_id"],
                last_synced_at=now_iso(),
                # Sprint-4 Track A — persist the directory's roster snapshot
                # of the OTHER enterprise's L2s so cross-Enterprise consult
                # forwards can resolve the target endpoint locally without
                # per-request directory round-trips.
                to_l2_endpoints_json=json.dumps(rec.get("to_l2_endpoints") or []),
            )
            persisted += 1
    return persisted


async def _pull_loop(privkey: Ed25519PrivateKey | None, enterprise_id: str, store: RemoteStore) -> None:
    """Long-running peering pull cron."""
    interval = pull_interval_sec()
    log.info("directory: pull loop started enterprise=%s interval=%ds", enterprise_id, interval)
    while True:
        try:
            n = await _pull_and_persist_once(privkey, enterprise_id, store)
            log.info("directory: pull cycle ok persisted=%d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("directory: pull cycle exploded err=%s", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# Lifespan integration
# ---------------------------------------------------------------------------


def _load_endpoints_config() -> list[dict[str, Any]]:
    """Build the l2_endpoints list from env. V1 advertises just this L2.

    A multi-L2 enterprise running the announce flow from its admin
    workstation will assemble the full roster and announce it from
    there; for cq-server-driven announces we just declare ourselves.
    """
    self_url = os.environ.get("CQ_AIGRP_SELF_URL", "")
    enterprise = os.environ.get("CQ_ENTERPRISE", "")
    group = os.environ.get("CQ_GROUP", "")
    if not (self_url and enterprise and group):
        return []
    return [
        {
            "l2_id": f"{enterprise}/{group}",
            "endpoint_url": self_url,
            "groups": [group],
        }
    ]


async def publish_reputation_root(
    client: httpx.AsyncClient,
    privkey: Ed25519PrivateKey,
    *,
    enterprise_id: str,
    root_date: str,
    event_count: int,
    merkle_root_hash: str,
    first_event_id: str | None,
    last_event_id: str | None,
) -> tuple[int, dict[str, Any] | None]:
    """POST one daily Merkle root to the directory's /reputation/root.

    Signed with the enterprise's AAISN root privkey (per directory v1
    accept rule — see decision 21 + the directory route docstring).
    Returns (status_code, response_body | None on error). 200/201 are
    success; 400/401 indicate caller-side issues; 0 indicates network
    failure.
    """
    payload = {
        "enterprise_id": enterprise_id,
        "root_date": root_date,
        "event_count": event_count,
        "merkle_root_hash": merkle_root_hash,
        "first_event_id": first_event_id,
        "last_event_id": last_event_id,
    }
    envelope = sign_envelope(privkey, payload)
    url = f"{directory_url()}/api/v1/directory/reputation/root"
    try:
        r = await client.post(url, json=envelope, timeout=10.0)
    except httpx.RequestError as e:
        log.warning("directory: reputation/root publish failed (network) err=%s", e)
        return 0, None
    if r.status_code in (200, 201):
        log.info(
            "directory: reputation/root ok enterprise=%s day=%s status=%d",
            enterprise_id,
            root_date,
            r.status_code,
        )
        return r.status_code, r.json()
    log.warning(
        "directory: reputation/root rejected enterprise=%s day=%s status=%d body=%s",
        enterprise_id,
        root_date,
        r.status_code,
        r.text[:200],
    )
    return r.status_code, None


async def reputation_publish_loop(get_conn: Callable[[], sqlite3.Connection]) -> None:
    """Periodically POST any unpublished daily roots to the directory.

    Runs every ``CQ_DIRECTORY_PUBLISH_INTERVAL_SEC`` (default 5 min).
    Polls ``reputation_roots WHERE published_to_directory_at IS NULL``
    and publishes each. Updates ``published_to_directory_at`` on
    success. Failures are logged and retried on the next tick — the
    column stays NULL until success.

    Decoupled from the daily-root computation loop on purpose: a
    network blip during compute shouldn't lose the root, and a delayed
    compute (recovered missed cron) shouldn't block other Enterprises'
    publishing.
    """
    if not directory_enabled():
        log.info("directory: reputation publish loop disabled (CQ_DIRECTORY_ENABLED!=true)")
        return

    if skip_announce():
        # Skip-announce mode means no privkey on disk; we can't sign roots.
        log.info("directory: reputation publish loop disabled (CQ_DIRECTORY_SKIP_ANNOUNCE=true)")
        return

    privkey_path = os.environ.get("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", "")
    if not privkey_path:
        log.info("directory: reputation publish loop disabled (CQ_ENTERPRISE_ROOT_PRIVKEY_PATH unset)")
        return

    try:
        privkey = load_private_key(Path(privkey_path))
    except (FileNotFoundError, ValueError) as e:
        log.error(
            "directory: reputation publish — cannot load privkey path=%s err=%s",
            privkey_path,
            e,
        )
        return

    interval = int(os.environ.get("CQ_DIRECTORY_PUBLISH_INTERVAL_SEC", "300"))
    log.info("directory: reputation publish loop starting (interval=%ds)", interval)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                log.info("directory: reputation publish loop cancelled")
                return

            try:
                conn = get_conn()
                try:
                    rows = conn.execute(
                        """
                        SELECT enterprise_id, root_date, event_count,
                               merkle_root_hash, first_event_id, last_event_id
                        FROM reputation_roots
                        WHERE published_to_directory_at IS NULL
                        ORDER BY root_date ASC
                        LIMIT 50
                        """
                    ).fetchall()
                    for row in rows:
                        status, _body = await publish_reputation_root(
                            client,
                            privkey,
                            enterprise_id=row[0],
                            root_date=row[1],
                            event_count=row[2],
                            merkle_root_hash=row[3],
                            first_event_id=row[4],
                            last_event_id=row[5],
                        )
                        if status in (200, 201):
                            conn.execute(
                                """
                                UPDATE reputation_roots
                                SET published_to_directory_at = ?
                                WHERE enterprise_id = ? AND root_date = ?
                                """,
                                (now_iso(), row[0], row[1]),
                            )
                            conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — never let this loop die
                log.warning(
                    "directory: reputation publish loop iteration crashed",
                    exc_info=True,
                )


async def directory_bootstrap_and_loop(store: RemoteStore) -> None:
    """Top-level lifespan task: announce, then start the pull loop.

    Three modes via env:
    - ``CQ_DIRECTORY_ENABLED`` unset/false (default) — entirely skipped
    - ``CQ_DIRECTORY_ENABLED=true`` + ``CQ_DIRECTORY_SKIP_ANNOUNCE=false`` —
      L2 announces itself + runs the pull loop. Requires the enterprise
      root privkey on disk; appropriate for single-L2 enterprises.
    - ``CQ_DIRECTORY_ENABLED=true`` + ``CQ_DIRECTORY_SKIP_ANNOUNCE=true`` —
      pull-only. L2 doesn't announce; an operator manages the roster
      out-of-band via the 8l-directory CLI from a separate workstation.
      The L2 still pulls peerings so it can authorize cross-Enterprise
      forwards. No privkey on disk needed.
    """
    if not directory_enabled():
        log.info("directory: disabled (CQ_DIRECTORY_ENABLED!=true) — skipping bootstrap")
        return

    enterprise_id = os.environ.get("CQ_ENTERPRISE", "")
    if not enterprise_id:
        log.error("directory: enabled but CQ_ENTERPRISE unset — skipping")
        return

    if skip_announce():
        # Pull-only mode. No privkey, no announce; just pull peerings.
        log.info(
            "directory: skip-announce mode (CQ_DIRECTORY_SKIP_ANNOUNCE=true) — "
            "starting pull loop only for enterprise=%s",
            enterprise_id,
        )
        await _pull_loop(privkey=None, enterprise_id=enterprise_id, store=store)
        return

    privkey_path = os.environ.get("CQ_ENTERPRISE_ROOT_PRIVKEY_PATH", "")
    if not privkey_path:
        log.error("directory: enabled but CQ_ENTERPRISE_ROOT_PRIVKEY_PATH unset — skipping")
        return
    contact_email = os.environ.get("CQ_DIRECTORY_CONTACT_EMAIL", "")
    if not contact_email:
        log.error("directory: enabled but CQ_DIRECTORY_CONTACT_EMAIL unset — skipping")
        return

    try:
        privkey = load_private_key(Path(privkey_path))
    except (FileNotFoundError, ValueError) as e:
        log.error("directory: cannot load privkey path=%s err=%s — skipping", privkey_path, e)
        return

    display_name = os.environ.get("CQ_DIRECTORY_DISPLAY_NAME", enterprise_id)
    visibility = os.environ.get("CQ_DIRECTORY_VISIBILITY", "public")
    topics_csv = os.environ.get("CQ_DIRECTORY_TOPICS", "")
    discoverable_topics = [t.strip() for t in topics_csv.split(",") if t.strip()]
    l2_endpoints = _load_endpoints_config()
    if not l2_endpoints:
        log.error(
            "directory: cannot build l2_endpoints (CQ_AIGRP_SELF_URL/CQ_ENTERPRISE/CQ_GROUP) — skipping",
        )
        return

    ok = await _announce_with_retries(
        privkey,
        enterprise_id,
        display_name,
        visibility,
        contact_email,
        l2_endpoints,
        discoverable_topics,
    )
    if not ok:
        log.error("directory: announce never succeeded after retries — pull loop not started")
        return

    # Pull loop runs forever until cancelled by lifespan teardown.
    await _pull_loop(privkey, enterprise_id, store)
