# Manual peer announce — admin escape hatches

Two endpoints — sibling escape hatches for direct-CFN and air-gapped
L2 deploys that bypass the `directory.8th-layer.ai` poll loop:

| Endpoint | Populates | Scope |
|---|---|---|
| `POST /api/v1/admin/aigrp/peers` (#337) | `aigrp_peers` | Intra-Enterprise mesh discovery |
| `POST /api/v1/admin/aigrp/directory-peerings` (#347) | `aigrp_directory_peerings` | Cross-Enterprise consult-forward wiring |

The first half of this doc covers the intra-Enterprise endpoint; the
[cross-Enterprise sibling section](#cross-enterprise-sibling--post-apiv1adminaigrpdirectory-peerings)
covers the cross-Enterprise one.

## Intra-Enterprise — `POST /api/v1/admin/aigrp/peers`

Escape hatch for direct-CFN and air-gapped L2 deploys that bypass the
`directory.8th-layer.ai` poll loop. Lets a tenancy-scoped admin
populate `aigrp_peers` directly so intra-Enterprise mesh ops have peer
rows on L2s that never registered themselves with the directory.

Closes the immediate substrate gap from
[#337](https://github.com/OneZero1ai/8th-layer-agent/issues/337) —
mvp-*, s4-ent-b and other direct-CFN deploys can now wire peer
discovery by hand instead of waiting on the directory.

## When to use this vs the directory

| Path | Use when | Cost |
|---|---|---|
| `directory.8th-layer.ai` poll loop (primary) | The L2 has outbound HTTPS to `directory.8th-layer.ai` and the customer is OK with that runtime dependency | Zero — runs automatically on startup |
| `POST /admin/aigrp/peers` (this endpoint, escape hatch) | Air-gapped / IL5 / federal / customer policy forbids the directory connection, or the directory is down and you need to unblock now | Admin must paste each peer manually; no auto-refresh |

The two paths are not mutually exclusive — a row inserted by this
endpoint is overwritten when the directory next pulls a matching record
(directory's `signature_received=True` upsert wins on `last_signature_at`).

## Scope — intra-Enterprise only

This endpoint populates `aigrp_peers` (the intra-Enterprise mesh
discovery table). It does **not** populate `aigrp_directory_peerings`
(the cross-Enterprise offer/accept table). Cross-Enterprise peer
insertion is intentionally refused with 422 because that table belongs
to the bilateral peering protocol — an admin can't unilaterally
fabricate one side of a peering.

The caller's user-row `enterprise_id` must equal `body.enterprise`.

## Request body

```json
{
  "l2_id": "acme/sga",
  "enterprise": "acme",
  "group": "sga",
  "endpoint_url": "https://sga.acme.example.com",
  "pubkey": "<base64url-encoded Ed25519 public key>",
  "embedding_centroid": [0.12, -0.04, 0.31, ...],
  "domain_bloom": "<base64-encoded Bloom filter bytes>",
  "ku_count": 1284,
  "domain_count": 42,
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
}
```

Field notes:

- **`l2_id`** must decompose to `<enterprise>/<group>` matching the
  body's `enterprise` + `group` fields. The server rejects mismatches
  with 422.
- **`pubkey`** is base64url (no padding) of the peer's 32-byte Ed25519
  public key. Stored verbatim in `aigrp_peers.public_key_ed25519` and
  used by the legacy AIGRP forward path to verify peer-signed envelopes.
- **`embedding_centroid`** is optional on first announce. When present,
  the server packs the float list to little-endian float32 bytes
  (byte-compatible with `aigrp._legacy.compute_centroid`'s output). Pass
  `null` or omit when the peer hasn't computed its centroid yet —
  semantic-routing fallback handles missing centroids.
- **`domain_bloom`** is optional base64 of the peer's domain Bloom
  filter. Non-base64-alphabet input is rejected with 422.
- **`ku_count`** / **`domain_count`** are snapshots used for ranking
  tie-breaks; default 0 is safe.

## Auth

`require_admin` — JWT bearer or `cq_session` cookie attached to a user
whose role is in `_ADMIN_ROLES` (`admin` / `enterprise_admin` /
`l2_admin`). Plus the tenancy gate: the caller's user-row
`enterprise_id` must equal `body.enterprise`.

## Response

```json
{
  "l2_id": "acme/sga",
  "enterprise": "acme",
  "group": "sga",
  "endpoint_url": "https://sga.acme.example.com",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "ku_count": 1284,
  "domain_count": 42,
  "first_seen_at": "2026-05-20T14:33:01.123456+00:00",
  "last_seen_at": "2026-05-20T14:33:01.123456+00:00",
  "last_signature_at": "2026-05-20T14:33:01.123456+00:00",
  "public_key_ed25519": "<base64url Ed25519 pubkey>",
  "audit_id": "<32-hex audit id from cross_l2_audit>"
}
```

The endpoint is idempotent — re-announcing the same `l2_id` upserts
cleanly, anchoring `first_seen_at` to the original insert while
`last_seen_at` / `last_signature_at` advance.

Each successful announce writes a row to `cross_l2_audit` with
`policy_applied='manual_peer_announce'`, distinguishable from
directory-pulled rows for forensic review.

## Receiver-side verification

The receiving L2 trusts a manually-announced peer the same way it
trusts a directory-pulled peer — by verifying signatures on inbound
AIGRP envelopes against the cached `aigrp_peers.public_key_ed25519`.
There is no extra round trip; the announce IS the key publication.

That means the **admin pasting the announce is the trust anchor**. The
operator must verify the peer's Ed25519 public key out-of-band before
submitting (peer admin reads it off their L2's startup log; the
receiving admin pastes it into this endpoint). This matches the
threat-model premise behind the air-gapped escape hatch — when you
disable the directory, you accept manual key distribution.

## Example — curl

```bash
JWT=$(cat ~/.config/8l/admin.jwt)
PEER_PUBKEY=$(ssh peer-l2 'cat /var/lib/cq/aigrp_pubkey.b64u')

curl -sS -X POST https://l2.acme.example.com/api/v1/admin/aigrp/peers \
  -H "authorization: Bearer ${JWT}" \
  -H "content-type: application/json" \
  -d @- <<JSON
{
  "l2_id": "acme/sga",
  "enterprise": "acme",
  "group": "sga",
  "endpoint_url": "https://sga.acme.example.com",
  "pubkey": "${PEER_PUBKEY}"
}
JSON
```

## Cross-Enterprise sibling — `POST /api/v1/admin/aigrp/directory-peerings`

The endpoint above (`/admin/aigrp/peers`) populates `aigrp_peers` for
**intra-Enterprise** mesh discovery. The cross-Enterprise sibling —
shipped under [#347](https://github.com/OneZero1ai/8th-layer-agent/issues/347) — is
`POST /api/v1/admin/aigrp/directory-peerings` and populates
`aigrp_directory_peerings`, the table the cross-Enterprise
consult-forward path (`/api/v1/consults/x-enterprise-forward-request`)
reads via `find_active_directory_peering`.

This is **Option A** from the #347 issue body — manual paste of the
peer's announce data, single admin as trust anchor. **Option B** (the
bilateral offer/accept signed-envelope protocol that
`directory.8th-layer.ai` runs) remains the long-term answer and
co-exists with this endpoint — a row inserted here is overwritten on
the next directory pull that returns the same `(from_enterprise,
to_enterprise)` pair under a proper signed peering.

### Request body

```json
{
  "l2_id": "globex/sga",
  "enterprise": "globex",
  "group": "sga",
  "endpoint_url": "https://sga.globex.example.com",
  "pubkey": "<base64url-encoded Ed25519 public key>",
  "aaisn": "AS-65500",
  "embedding_centroid": [0.12, -0.04, 0.31, ...],
  "domain_bloom": "<base64-encoded Bloom filter bytes>",
  "ku_count": 1284,
  "domain_count": 42,
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "ttl_days": 30
}
```

Field notes:

- **`enterprise`** MUST be **different** from the caller's resolved
  tenancy (this is the cross-Enterprise endpoint). Same-Enterprise
  paste is rejected with 422 and a pointer to `/admin/aigrp/peers`.
  The sentinel `default-enterprise` is also rejected — it catches
  misconfigured L2s where tenancy was never bound.
- **`l2_id`** must decompose to `<enterprise>/<group>` matching the
  body's `enterprise` + `group` fields. Mismatches are 422.
- **`pubkey`** is base64url (no padding) of the peer L2's 32-byte
  Ed25519 public key. Stored verbatim and carried in both the
  `offer_signing_key_id` and `accept_signing_key_id` slots of the
  `aigrp_directory_peerings` row.
- **`aaisn`** is optional. The peering schema has no `aaisn` column,
  so the value rides along inside `to_l2_endpoints_json[0].aaisn`
  where future consumers can find it without a migration. Pubkey is
  the security anchor anyway — AAISN is human-readable provenance.
- **`embedding_centroid`** / **`domain_bloom`** are accepted for
  forward-compat and stowed inside the endpoint roster entry. The
  current `aigrp_directory_peerings` consumers don't read them, but
  populating them now means the value survives if a later schema
  migration pulls them into dedicated columns.
- **`ttl_days`** overrides the default 30-day TTL (1..365). After the
  TTL expires the row drops out of `find_active_directory_peering` and
  the consult forward 422s with an honest "no peering" rather than
  silently using stale data — re-paste to refresh.

### What lands in the row

The schema was designed around the offer/accept signed-envelope shape,
but a manual announce has no signed envelope. The bridge:

| Column | Manual-row value |
|---|---|
| `offer_id` | Deterministic — `manual:<from>:<to>:<l2_id>` — so re-paste is idempotent on the same `(from, to, l2_id)` triple |
| `from_enterprise` | Caller's resolved tenancy |
| `to_enterprise` | Body's `enterprise` |
| `status` | `'active'` |
| `content_policy` / `consult_logging_policy` | `'manual'` — sentinel for "no signed policy"; consumers can distinguish manual from directory-pulled rows |
| `topic_filters_json` | `'[]'` |
| `active_from` | Now |
| `expires_at` | Now + `ttl_days` (default 30 days) |
| `offer_payload_canonical` / `accept_payload_canonical` | Synthesized provenance JSON (admin persona, ts, source tag) — **not signed**; consumers must check `content_policy='manual'` before attempting to verify |
| `offer_signature_b64u` / `accept_signature_b64u` | Empty string |
| `offer_signing_key_id` / `accept_signing_key_id` | Body's `pubkey` |
| `to_l2_endpoints_json` | One-entry list `[{l2_id, endpoint_url, pubkey, aaisn?, ...}]` — the exact shape `consults._resolve_x_enterprise_target` reads |
| `last_synced_at` | Now |

### Auth

`require_admin` — same as the intra-Enterprise endpoint. The body's
`enterprise` field MUST be different from the caller's resolved
tenancy.

### Trust caveat

**The admin pasting the announce IS the trust anchor.** There is no
callback verification of the announced pubkey, no co-signature check,
no out-of-band attestation. The receiving L2 will treat the pasted
Ed25519 pubkey as authoritative for the named peer L2 on every
subsequent consult forward.

That's the right trade-off for direct-CFN / federal / air-gapped
deploys — the operator who has admin on this L2 already has the
authority to flip every other security-relevant knob. But: out-of-band
verification of the peer's pubkey **before** pasting is mandatory.
Peer admin reads it off their L2's startup log; receiving admin
pastes it into this endpoint.

When to step up to Option B (the bilateral signed-envelope protocol
the directory client runs): when the deploy can reach
`directory.8th-layer.ai`, or when the customer's threat model
requires the cross-signed evidence trail rather than a single admin's
paste. Option B isn't shipped on direct-CFN deploys yet (the
directory client polls a public endpoint), so for mvp-* / s4-ent-b
this Option-A path is the only way to wire cross-Enterprise consult
forward.

### Example — curl

```bash
JWT=$(cat ~/.config/8l/admin.jwt)
# Peer admin reads these off their L2's startup log and emails them over.
PEER_PUBKEY="..."          # base64url Ed25519 pubkey
PEER_ENDPOINT="https://sga.globex.example.com"
PEER_AAISN="AS-65500"

curl -sS -X POST https://l2.acme.example.com/api/v1/admin/aigrp/directory-peerings \
  -H "authorization: Bearer ${JWT}" \
  -H "content-type: application/json" \
  -d @- <<JSON
{
  "l2_id": "globex/sga",
  "enterprise": "globex",
  "group": "sga",
  "endpoint_url": "${PEER_ENDPOINT}",
  "pubkey": "${PEER_PUBKEY}",
  "aaisn": "${PEER_AAISN}"
}
JSON
```

The response carries the synthesized `offer_id`, the `active_from` /
`expires_at` window, and the audit_id from `cross_l2_audit`
(`policy_applied='manual_directory_peer_announce'`).

### Drift items this unblocks

Closes 4 of 6 drift items in
[#343](https://github.com/OneZero1ai/8th-layer-agent/issues/343) for
mvp-* / direct-CFN substrates: `aigrp`, `dsn-intro`, `dsn-reveal`,
`consented-query`. Each was failing at the
`find_active_directory_peering` lookup; with this endpoint, an admin
paste lands the row and the forward path completes.

## What these PRs do NOT close

- **L2 startup auto-announce** — the primary path improvement (have
  every L2 announce itself to the directory on boot) is a separate
  follow-up.
- **Option B for direct-CFN deploys** — wiring the bilateral
  signed-envelope protocol on L2s that can't reach the directory is
  separate work. The current direction is "Option A unblocks the
  forward path; Option B is a future hardening pass."

## Refs

- [#337](https://github.com/OneZero1ai/8th-layer-agent/issues/337) —
  parent issue for the intra-Enterprise endpoint
- [#347](https://github.com/OneZero1ai/8th-layer-agent/issues/347) —
  cross-Enterprise sibling (Option A path)
- [#343](https://github.com/OneZero1ai/8th-layer-agent/issues/343) —
  mvp-* drift items the cross-Enterprise endpoint unblocks
- [#322](https://github.com/OneZero1ai/8th-layer-agent/issues/322) —
  Enterprise-scoped `AigrpPeerKey` (sibling concern: directory could
  also distribute the peer key)
- `server/backend/src/cq_server/aigrp_peer_routes.py` — intra-Enterprise impl
- `server/backend/src/cq_server/aigrp_directory_peer_routes.py` — cross-Enterprise impl
- `server/backend/tests/test_aigrp_peer_routes.py` — intra-Enterprise tests
- `server/backend/tests/test_aigrp_directory_peer_routes.py` — cross-Enterprise tests
