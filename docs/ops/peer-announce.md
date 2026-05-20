# Manual peer announce — `POST /api/v1/admin/aigrp/peers`

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

## What this PR does NOT close

- **Cross-Enterprise consult on mvp-\* L2s** — the
  `aigrp_directory_peerings` table (cross-Enterprise offer/accept rows)
  is separate. That gap needs a follow-up that either (a) walks the
  bilateral peering protocol manually or (b) adds a sibling escape
  hatch for `aigrp_directory_peerings` with cryptographic safeguards
  appropriate to cross-tenant trust. Tracked as a continuation of #337.
- **L2 startup auto-announce** — the primary path improvement (have
  every L2 announce itself to the directory on boot) is a separate
  follow-up.

## Refs

- [#337](https://github.com/OneZero1ai/8th-layer-agent/issues/337) —
  parent issue
- [#322](https://github.com/OneZero1ai/8th-layer-agent/issues/322) —
  Enterprise-scoped `AigrpPeerKey` (sibling concern: directory could
  also distribute the peer key)
- `server/backend/src/cq_server/aigrp_peer_routes.py` — implementation
- `server/backend/tests/test_aigrp_peer_routes.py` — tests
