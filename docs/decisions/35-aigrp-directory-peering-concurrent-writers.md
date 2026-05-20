# 35 — `aigrp_directory_peerings` concurrent writers + lock boundary

**Status**: accepted
**Date**: 2026-05-20
**Drives**: #346 concern 3 (post-EBS migration write contention)
**Touches**: #327 (EBS swap), #347 / PR #349 (Option-A admin endpoint),
`directory_client._sync_peerings` (Option-B-shaped poll loop),
`forward_sign.py` (no-write verifier — listed here for completeness).

## Context

PR #349 adds `POST /api/v1/admin/aigrp/directory-peerings` — the manual
"Option A" cross-Enterprise peer-announce escape hatch for direct-CFN
and air-gapped L2s. Concern 3 of issue #346 noted that the
`aigrp_directory_peerings` table is now written by multiple
concurrent paths post-EBS migration, and asked for explicit ordering /
isolation guidance.

This note codifies the answer.

## Writers

There are three logical writers contending for the table; in practice
two of them share a single function call (`directory_client` is the
on-L2 writer for the bilateral signed flow). All run inside the same
Python process, against the same SQLite file mounted on EBS.

| ID  | Writer                                                                | Trigger                          | `offer_id` shape                    | Verifies signatures? |
| --- | --------------------------------------------------------------------- | -------------------------------- | ----------------------------------- | -------------------- |
| (a) | `aigrp_directory_peer_routes.announce_directory_peering` (this PR)    | Admin HTTP call                  | `manual:<from>:<to>:<l2_id>`        | No — admin is anchor |
| (b) | `directory_client._sync_peerings` (Option-B / bilateral signed)       | Periodic poll loop               | Directory-assigned UUID-shaped id   | Yes (offer + accept) |
| (c) | "The bilateral signed flow"                                           | (same as (b) on this L2)         | (same as (b))                       | (same as (b))        |

(c) is not a distinct on-L2 writer — the bilateral envelopes are
verified and ingested by (b) during the directory pull. The
`forward_sign.py` module only verifies signatures; it never writes
peering rows. We list it as a separate row in issue #346's
enumeration because conceptually the *flow* (offer/accept) is
distinct from the *transport* (directory poll), but on this codebase
both collapse into (b)'s `store.upsert_directory_peering` call site.

## Conflict resolution

Both (a) and (b) call `store.upsert_directory_peering`, which under
the hood runs:

```sql
INSERT INTO aigrp_directory_peerings (...) VALUES (...)
ON CONFLICT(offer_id) DO UPDATE SET ...
```

inside a single `self._engine.begin()` block (see
`_upsert_directory_peering_sync` in `store/_sqlite.py`).

The natural-key collision space is small because:

- (a) emits `offer_id = manual:<from>:<to>:<l2_id>` — synthetic,
  deterministic, idempotent on the natural key.
- (b) emits the directory service's UUID-shaped `offer_id` — never
  starts with `manual:`.

So an upsert from (a) and an upsert from (b) for the same *logical*
`(from_enterprise, to_enterprise)` peer pair land in **distinct
rows** with **distinct primary keys**. The
`find_active_directory_peering` reader applies
`status = 'active' AND expires_at > now`, then orders by
`active_from DESC LIMIT 1` — the most recent active row wins
regardless of which writer produced it.

### Why UPSERT, not straight INSERT

Concern 3 raised the question of which the admin endpoint should
use. We use UPSERT for both writers because:

1. **Re-paste idempotency.** Straight INSERT would 409 on the second
   paste of the same announce (deterministic `offer_id` collision).
   That breaks the operator UX — pasting again should refresh
   `expires_at`, not error.
2. **Symmetry with the directory poll loop.** (b) already uses
   UPSERT (it has to — the same directory record gets re-pulled on
   every cycle). Keeping (a) on the same primitive simplifies
   reasoning about the two writers.
3. **Last-write-wins is correct here.** Both writers carry full
   row state on every call; there is no partial-update pattern.

### Manual-row → signed-row precedence

If (a) lands first (admin pastes a peer ahead of the directory
catching up) and (b) later returns a real signed peering for the
same `(from, to)`, both rows coexist with different `offer_id`
PKs. The reader's `ORDER BY active_from DESC LIMIT 1` picks the
most-recently-active row.

In practice (b)'s `active_from` will be later than (a)'s, so the
signed row wins as soon as the directory catches up. The manual
row expires after its TTL (default 30 days) and drops out of the
`WHERE expires_at > now` filter — no GC cycle needed.

If for some reason (a) is pasted *after* (b)'s row, the manual row
overrides the signed row until either re-pull or expiry. That's
the cost of having an admin escape hatch — explicitly documented
in `docs/ops/peer-announce.md` under the trust caveat.

## Isolation model (SQLite WAL on EBS, post-#327)

SQLite WAL allows N readers + 1 writer concurrently. Writers
serialise on the database-wide lock:
`BEGIN IMMEDIATE` → RESERVED → PENDING → EXCLUSIVE during commit.

Both (a) and (b) hit the same `engine.begin()` boundary, so their
upserts cannot interleave at statement granularity — one fully
commits before the other starts. SQLite has no row-level locking;
the unit of mutual exclusion is the whole database file.

This is intentional. The table is small (O(10s of rows per L2)),
write throughput is low (admin paste + periodic directory poll),
and a single global writer lock is well within budget. We do not
need MVCC or row-level locks for this workload.

### EBS vs EFS distinction (#327)

The original `aigrp_peers` corruption event (#323 / #324) was
caused by EFS's loose fsync semantics breaking WAL ordering
guarantees. The fix in #327 swapped the L2 substrate to
EBS-on-EC2 with DLM snapshots; EBS preserves the POSIX fsync
contract that WAL requires.

With EBS the lock-boundary analysis above holds. With EFS it did
not — writes could appear to commit, then be lost if the EFS
server rolled back, leaving in-memory state and on-disk state
divergent. We are no longer exposed to that mode on the L2 path.

The DR target (jump-server) still uses EBS — same isolation
guarantees.

## What this note does not cover

- Cross-process contention. There is one cq-server process per L2;
  in-process concurrency is the only mode that matters.
- Cross-L2 contention. Each L2 has its own SQLite file; rows in
  one L2's `aigrp_directory_peerings` are independent of another
  L2's.
- The intra-Enterprise `aigrp_peers` table (PR #345). That table
  has the same writer model — admin endpoint + directory poll loop
  — and the same WAL isolation analysis applies. We do not
  duplicate the note for it; refer here.

## References

- Issue #346 — concern 3 raised this gap
- PR #349 — the Option-A endpoint this note documents
- Issue #347 — parent (Option A path design)
- PR #345 — intra-Enterprise sibling (same write model on
  `aigrp_peers`)
- Issue #327 — EBS swap that fixed the WAL fsync ordering
- Issues #323 / #324 — the EFS corruption event that motivated #327
- `server/backend/src/cq_server/aigrp_directory_peer_routes.py` —
  the admin endpoint (writer (a))
- `server/backend/src/cq_server/directory_client.py:_sync_peerings`
  — the directory poll loop (writer (b))
- `server/backend/src/cq_server/store/_sqlite.py:_upsert_directory_peering_sync`
  — the shared transaction boundary both writers share
