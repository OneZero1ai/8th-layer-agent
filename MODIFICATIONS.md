# Modifications from upstream `mozilla-ai/cq`

This file states the modifications made by OneZero1.ai to the upstream
[`mozilla-ai/cq`](https://github.com/mozilla-ai/cq) codebase, in fulfillment
of [Apache License, Version 2.0](LICENSE) §4(b).

For project-level rationale and the upstream-vs-fork delta narrative, see
[`FORK_DELTA.md`](FORK_DELTA.md). For per-PR detail, see this repository's
[git log](https://github.com/OneZero1ai/8th-layer-agent/commits/main).

_Generated 2026-06-01 from `git ls-files server/backend/src/cq_server/ server/backend/tests/`
classified against upstream `563d0e93cf4676bdf64c21a55ad5e54c94d6d0ca`._

## Fork base

- Forked from `mozilla-ai/cq` on **2026-04-26**.
- Upstream commit pinned at fork creation: **`563d0e93cf4676bdf64c21a55ad5e54c94d6d0ca`**
  (`server: add SQLAlchemy + Alembic dependencies and skeleton`, #321).
- Recorded in [`FORK_DELTA.md`](FORK_DELTA.md) sync discipline; derived from
  the parent of the first fork-marker commit (`21af2bc`).

## Apache-2.0 §4(b) compliance posture

Apache-2.0 §4(b) requires that "any modified files" carry "prominent notices
stating that You changed the files." This document is the prominent notice
covering all tracked source files under `server/backend/src/cq_server/` and
`server/backend/tests/`. We do not maintain per-file modification headers;
this manifest is exhaustive for those paths and is updated whenever a file's
status changes. Other fork changes (plugins, deploy templates, docs) are
described in [`FORK_DELTA.md`](FORK_DELTA.md).

**Coverage:** 150 files — 127 new, 15 modified,
8 unchanged from fork base.

## New files (entirely OneZero1.ai)

These files do not exist in upstream at the fork-base commit and are wholly
authored by OneZero1.ai:

| File | Purpose |
|---|---|
| `server/backend/src/cq_server/activity.py` | Activity log of record — shared module for #108 Stage 1 substrate. |
| `server/backend/src/cq_server/activity_logger.py` | Non-blocking activity-log writer for #108 Stage 2 instrumentation. |
| `server/backend/src/cq_server/activity_routes.py` | Activity-log read endpoint (#108 Stage 2 — Workstream D). |
| `server/backend/src/cq_server/admin_routes.py` | Admin routes — xgroup_consent propose/cosign/ratify/revoke (Phase 1.0b). |
| `server/backend/src/cq_server/agent_key_routes.py` | FO-4: Self-service Add Agent — admin routes for minting agent keys. |
| `server/backend/src/cq_server/aigrp/__init__.py` | AIGRP package. |
| `server/backend/src/cq_server/aigrp/_legacy.py` | AIGRP — peer-to-peer mesh inside an Enterprise (EIGRP-shaped). |
| `server/backend/src/cq_server/aigrp/enterprise_root.py` | Enterprise AIGRP root: SSM-backed, CMK-encrypted, in-process cached. |
| `server/backend/src/cq_server/aigrp/envelope.py` | AIGRP message envelope: HMAC-SHA256 sign + verify with replay protection. |
| `server/backend/src/cq_server/aigrp/federation.py` | Cross-L2 AIGRP federation — outbound forward-query client (agent#316). |
| `server/backend/src/cq_server/aigrp/pair_secret.py` | Per-pair AIGRP secret derivation (Decision 28 §1.1). |
| `server/backend/src/cq_server/aigrp/runtime.py` | AIGRP runtime wiring — bridges enterprise_root + pair_secret into HTTP auth. |
| `server/backend/src/cq_server/aigrp_directory_peer_routes.py` | Admin route — manual cross-Enterprise directory-peering announce (agent#347). |
| `server/backend/src/cq_server/aigrp_peer_routes.py` | Admin route — manual AIGRP peer announcement (agent#337). |
| `server/backend/src/cq_server/bootstrap_admin.py` | First-admin bootstrap for fresh-from-marketplace L2s (P2.5, task #218). |
| `server/backend/src/cq_server/claim_page.py` | Server-rendered HTML claim page for invite acceptance (FO-1c, #191). |
| `server/backend/src/cq_server/consults.py` | L3 consults — agent-to-agent live consult endpoints. |
| `server/backend/src/cq_server/crosstalk_routes.py` | Crosstalk endpoints (#124) — L2-mediated inter-session messaging. |
| `server/backend/src/cq_server/crypto.py` | Shared Ed25519 + RFC 8785 primitives. |
| `server/backend/src/cq_server/daily_root.py` | Daily Merkle root computation + persistence (task #108 sub-task 3). |
| `server/backend/src/cq_server/directory_client.py` | 8th-Layer Directory client — sprint 3. |
| `server/backend/src/cq_server/email_sender.py` | Email sending — L2-side client for the central transactional-mail service. |
| `server/backend/src/cq_server/embed.py` | Embedding generation via AWS Bedrock Titan. |
| `server/backend/src/cq_server/forward_sign.py` | Per-L2 Ed25519 keypair management + forward-* request signing. |
| `server/backend/src/cq_server/invite_routes.py` | Invite HTTP routes — FO-1b magic-link surface. |
| `server/backend/src/cq_server/invites.py` | Invite minting / validation / claim — FO-1b. |
| `server/backend/src/cq_server/l2_provision_routes.py` | FO-3 Phase 2 — cq-server L2-provision proxy + SSE passthrough (agent#193). |
| `server/backend/src/cq_server/merkle.py` | SHA-256 Merkle tree over reputation event hashes (task #108 sub-task 3). |
| `server/backend/src/cq_server/migrations.py` | Run Alembic migrations at server startup. |
| `server/backend/src/cq_server/network.py` | Network-demo proxy endpoints — Lane H/I/J support. |
| `server/backend/src/cq_server/passkey.py` | WebAuthn / passkey ceremony helpers (FO-1a, #191). |
| `server/backend/src/cq_server/passkey_routes.py` | FastAPI router for passkey enrollment + login (FO-1a, #191). |
| `server/backend/src/cq_server/persona_routes.py` | AS-1: Personas tab — admin routes for Human persona management. |
| `server/backend/src/cq_server/quality.py` | Propose-time content quality guards. |
| `server/backend/src/cq_server/reflect.py` | Server-side batch-reflect endpoints (#67). |
| `server/backend/src/cq_server/reputation.py` | Reputation log v1 — Ed25519-signed append-only hash chain. |
| `server/backend/src/cq_server/reputation_routes.py` | Reputation reader endpoints (task #108 sub-tasks 6 + admin trigger). |
| `server/backend/src/cq_server/reputation_verifier.py` | Reputation chain + root verifier (task #108 sub-task 7). |
| `server/backend/src/cq_server/store/_normalize.py` | Domain-tag normalisation shared between concrete stores. |
| `server/backend/src/cq_server/store/_queries.py` | Shared SQLAlchemy Core query helpers for portable cq server queries. |
| `server/backend/src/cq_server/store/_sqlite.py` | SqliteStore: SQLite-backed implementation of the async Store protocol. |
| `server/backend/src/cq_server/tenancy.py` | Single source of truth for write-path tenancy resolution (agent#339). |
| `server/backend/src/cq_server/theme.py` | Theme resolver — 3-tier brand hierarchy (FO-1d, Decision 30). |
| `server/backend/src/cq_server/theme_routes.py` | FastAPI router for ``GET /api/v1/theme`` (FO-1d, Decision 30). |
| `server/backend/src/cq_server/tour_routes.py` | Founder-tour persistence — per-user `tour_state` read/write. |
| `server/backend/src/cq_server/transactional/__init__.py` | Central transactional-mail service — Decision 34 (agent#348). |
| `server/backend/src/cq_server/transactional/auth.py` | HMAC v0 auth for the central transactional-mail service (Decision 34). |
| `server/backend/src/cq_server/transactional/dispatcher.py` | SES dispatcher — the central service's SES boto3 wrapper. |
| `server/backend/src/cq_server/transactional/idempotency.py` | In-memory idempotency cache for ``Idempotency-Key`` headers (Decision 34). |
| `server/backend/src/cq_server/transactional/routes.py` | ``POST /api/v1/transactional/send`` — central transactional-mail service. |
| `server/backend/src/cq_server/transactional/sns_writer.py` | SES → SNS bounce/complaint event → ``transactional_suppression`` writer. |
| `server/backend/src/cq_server/transactional/suppression.py` | ``transactional_suppression`` read/write helpers (Decision 34). |
| `server/backend/src/cq_server/transactional/tenancy.py` | Tenancy enforcement for the central transactional-mail service. |
| `server/backend/src/cq_server/web_session.py` | Cookie-bound web-session bearer (FO-1c, #191). |
| `server/backend/src/cq_server/xgroup_consent.py` | Intra-Enterprise xgroup_consent — 2-of-2 admin co-signed grants. |
| `server/backend/tests/test_activity_log_instrumentation.py` | Activity-log instrumentation — #108 Stage 2 Workstream A. |
| `server/backend/tests/test_activity_log_read_path.py` | Activity-log read-path instrumentation — agent#284. |
| `server/backend/tests/test_activity_read_endpoint.py` | ``GET /api/v1/activity`` — #108 Stage 2 Workstream D. |
| `server/backend/tests/test_admin_delete.py` | Tests for the admin DELETE /review/{ku_id} endpoint. |
| `server/backend/tests/test_admin_routes_xgroup_consent.py` | HTTP-level tests for /api/v1/admin/xgroup_consent/* (Phase 1.0b). |
| `server/backend/tests/test_agent_key_routes.py` | Tests for FO-4 Self-service Add Agent endpoints (agent#194). |
| `server/backend/tests/test_aigrp_directory_peer_routes.py` | HTTP-level tests for ``POST /api/v1/admin/aigrp/directory-peerings`` (agent#347). |
| `server/backend/tests/test_aigrp_enterprise_root.py` | Tests for the SSM-backed Enterprise AIGRP root (Decision 28 §1.2). |
| `server/backend/tests/test_aigrp_envelope.py` | AIGRP HMAC envelope tests (Decision 28 §1.6). |
| `server/backend/tests/test_aigrp_federation.py` | Cross-L2 AIGRP federation — outbound forward-query fan-out (agent#316). |
| `server/backend/tests/test_aigrp_pair_secret.py` | KAT + property tests for AIGRP pair-secret derivation (Decision 28 §1.1). |
| `server/backend/tests/test_aigrp_peer_routes.py` | HTTP-level tests for ``POST /api/v1/admin/aigrp/peers`` (agent#337). |
| `server/backend/tests/test_aigrp_runtime.py` | Phase 1.0d (Decision 28) — AIGRP runtime wiring tests. |
| `server/backend/tests/test_aud_discriminant.py` | Tests for FO-1c JWT aud-claim discriminant on /auth/me. |
| `server/backend/tests/test_auto_approve_propose.py` | Regression tests for #123 — CQ_AUTO_APPROVE_PROPOSE env flag. |
| `server/backend/tests/test_bloom_prefilter.py` | Issue #22 — Bloom prefilter at DSN query time. |
| `server/backend/tests/test_bootstrap_admin.py` | Tests for the password-login admin bootstrap (agent#165). |
| `server/backend/tests/test_bootstrap_liaison.py` | Tests for ``bootstrap_liaison_key_if_needed`` (decision 42 / W2). |
| `server/backend/tests/test_consents_revoke.py` | Phase 6 step 3 / Lane D: DELETE /consents/{consent_id} tests. |
| `server/backend/tests/test_consents_sign.py` | Phase 6 step 3 / Lane D: POST /consents/sign tests. |
| `server/backend/tests/test_consults.py` | Sprint 2 / Issue #20 — L3 consults · same-L2 path. |
| `server/backend/tests/test_cross_l2_routing.py` | Sprint 2 part 2 — cross-L2 consult routing via AIGRP. |
| `server/backend/tests/test_crosstalk_routes.py` | Tests for crosstalk endpoints (#124). |
| `server/backend/tests/test_daily_root.py` | Tests for daily Merkle root computation (task #108 sub-task 3). |
| `server/backend/tests/test_default_enterprise_backfill.py` | Backfill migration 0013 — legacy default-enterprise KUs (#121 finding 3). |
| `server/backend/tests/test_demo_scenarios.py` | Phase 6 step 4 / Lane J: POST /network/demo/{scenario} tests. |
| `server/backend/tests/test_directory_client.py` | Tests for the L2-side 8th-Layer Directory client (sprint 3). |
| `server/backend/tests/test_dsn_resolve.py` | Phase 6 step 4 / Lane I: POST /network/dsn/resolve tests. |
| `server/backend/tests/test_ed25519_forward_signing.py` | Sprint 4 — Ed25519 per-L2 forward-signing tests (#44 / full CRIT #34 close). |
| `server/backend/tests/test_email_sender_http.py` | Tests for the L2-side HTTP-client ``EmailSender`` (Decision 34). |
| `server/backend/tests/test_forward_query.py` | Phase 6 step 2: cross-L2 /aigrp/forward-query endpoint tests. |
| `server/backend/tests/test_invites.py` | Tests for FO-1b magic-link invites. |
| `server/backend/tests/test_l2_provision_routes.py` | HTTP-level tests for FO-3 Phase 2 — the cq-server L2-provision proxy. |
| `server/backend/tests/test_l2_tenancy_scope.py` | agent#303 — an FO-2-provisioned L2 must run in its own tenancy scope. |
| `server/backend/tests/test_merkle.py` | Tests for the SHA-256 Merkle tree (task #108 sub-task 3). |
| `server/backend/tests/test_migration_0002_xgroup_consent.py` | Phase 6 step 2: Alembic migration smoke-test. |
| `server/backend/tests/test_migration_0003_presence.py` | Phase 6 step 3: Alembic migration smoke-test. |
| `server/backend/tests/test_migration_0011_activity_log.py` | Stage 1 of #108 — activity-log Alembic migration smoke-tests. |
| `server/backend/tests/test_migration_0015_aigrp_peers_pair_secret_ref.py` | Phase 1.0c — ``aigrp_peers.pair_secret_ref`` migration smoke-tests. |
| `server/backend/tests/test_migrations.py` | Tests for Alembic baseline migration + stamp-on-startup logic. |
| `server/backend/tests/test_network_dsn_cache.py` | Issue #23 — DSN routed-hop · in-memory signature cache. |
| `server/backend/tests/test_network_topology.py` | Phase 6 step 4 / Lane J: POST /network/topology aggregator tests. |
| `server/backend/tests/test_passkey.py` | End-to-end tests for the FO-1a passkey enrollment substrate (#191). |
| `server/backend/tests/test_peers_active.py` | Phase 6 step 3 / Lane C: GET /peers/active scoping + filter tests. |
| `server/backend/tests/test_peers_heartbeat.py` | Phase 6 step 3 / Lane C: presence heartbeat upsert tests. |
| `server/backend/tests/test_pending_review_concurrency.py` | Optimistic-concurrency control on review-status transitions. |
| `server/backend/tests/test_pending_review_tier.py` | Pending-review tier (#103) — store + route tests. |
| `server/backend/tests/test_pending_review_ttl.py` | Read-time TTL enforcement on pending_review queries. |
| `server/backend/tests/test_per_l2_isolation.py` | Tests for Decision 27 — per-L2 isolation read-path migration. |
| `server/backend/tests/test_persona_routes.py` | Tests for AS-1 persona management endpoints. |
| `server/backend/tests/test_propose_tenancy_env_fallback.py` | agent#324 regression — propose stamps KU tenancy from env when the |
| `server/backend/tests/test_propose_tenancy_regression.py` | Regression tests for #89 — KU tenancy must come from auth claims. |
| `server/backend/tests/test_propose_tier.py` | Regression tests for #90 — /propose tier resolution. |
| `server/backend/tests/test_quality.py` | Tests for the propose-time content quality guards. |
| `server/backend/tests/test_queries.py` | Tests for the shared SQLAlchemy Core query helpers in ``store._queries``. |
| `server/backend/tests/test_reflect.py` | Tests for the batch-reflect endpoints (#67). |
| `server/backend/tests/test_reputation.py` | Tests for reputation log v1-alpha (task #99). |
| `server/backend/tests/test_reputation_verifier.py` | Tests for the reputation verifier library (task #108 sub-task 7). |
| `server/backend/tests/test_sqlite_store.py` | Tests for SqliteStore-only behaviour: engine wiring, PRAGMAs, threadpool shim, lifecycle. |
| `server/backend/tests/test_sqlite_store_e2e.py` | End-to-end smoke for the cq server against a temporary SQLite database. |
| `server/backend/tests/test_sqlite_store_fork_delta.py` | Tests for fork-delta methods ported to async SqliteStore (#105 PR-A). |
| `server/backend/tests/test_tenancy.py` | Unit tests for the central write-path tenancy resolver (agent#339). |
| `server/backend/tests/test_tenancy_columns.py` | Phase 6 step 1: regression tests for additive tenancy columns. |
| `server/backend/tests/test_theme.py` | Tests for FO-1d ``GET /api/v1/theme`` (Decision 30). |
| `server/backend/tests/test_tour_routes.py` | Tests for the founder-tour persistence endpoints. |
| `server/backend/tests/test_transactional_idempotency.py` | Unit tests for the in-memory ``IdempotencyStore`` (Decision 34). |
| `server/backend/tests/test_transactional_send.py` | End-to-end tests for ``POST /api/v1/transactional/send`` (Decision 34). |
| `server/backend/tests/test_transactional_sns_writer.py` | Tests for the SNS → suppression writer (``transactional.sns_writer``). |
| `server/backend/tests/test_transactional_suppression.py` | Tests for ``transactional.suppression`` read/write helpers. |
| `server/backend/tests/test_web_session.py` | Tests for the cookie-bound web-session module (FO-1c, #191). |
| `server/backend/tests/test_x_enterprise_consult.py` | Sprint 4 Track A — cross-Enterprise consult forward (sender side). |
| `server/backend/tests/test_xgroup_consent.py` | Round-trip + revocation + lineage tests for xgroup_consent (Phase 1.0b). |

## Modified files (additions on top of upstream)

These files exist in upstream at the fork-base commit and have been modified
by OneZero1.ai. The git diff against the fork-base commit is the authoritative
record of changes.

| File | Nature of modification |
|---|---|
| `server/backend/src/cq_server/app.py` | Multi-tenant scope on query/review/stats; AIGRP forward-query; directory-client lifespan; security gates; `/knowledge` alias |
| `server/backend/src/cq_server/auth.py` | Admin role; tenant scope from user row; versioned API-key list envelope |
| `server/backend/src/cq_server/db_url.py` | SQLite + Alembic URL resolution for SqliteStore startup |
| `server/backend/src/cq_server/deps.py` | Tenant-scope helpers; API-key and admin dependencies |
| `server/backend/src/cq_server/review.py` | Admin gate on all routes; tenant scoping on aggregates |
| `server/backend/src/cq_server/store/__init__.py` | Re-exports SqliteStore; tenant-scoped CRUD; AIGRP/consults/directory methods |
| `server/backend/src/cq_server/store/_protocol.py` | Extended async Store protocol for fork-delta store methods |
| `server/backend/src/cq_server/tables.py` | Multi-tenant + AIGRP + activity/reputation schema tables |
| `server/backend/tests/test_app.py` | Multi-tenant API tests; knowledge alias; forward-query coverage |
| `server/backend/tests/test_auth.py` | Admin role and tenant-scope auth tests |
| `server/backend/tests/test_db_url.py` | Database URL resolution tests for fork startup path |
| `server/backend/tests/test_deps.py` | Tenant-scope and require_api_key dependency tests |
| `server/backend/tests/test_review.py` | Admin-gated review queue tests with tenant scoping |
| `server/backend/tests/test_store.py` | SqliteStore CRUD and fork-delta store method tests |
| `server/backend/tests/test_store_protocol.py` | SqliteStore satisfies extended Store protocol |

## Files unchanged from upstream fork base

These tracked backend files are byte-identical to upstream at the fork-base
commit — preserved open-protocol portions with no OneZero1.ai edits:

| File | Purpose |
|---|---|
| `server/backend/src/cq_server/__init__.py` | Package marker; unchanged from fork base |
| `server/backend/src/cq_server/api_keys.py` | API key token encoding and hashing.; unchanged from fork base |
| `server/backend/src/cq_server/scoring.py` | Confidence scoring and relevance functions for knowledge units.; unchanged from fork base |
| `server/backend/src/cq_server/ttl.py` | Duration-string parser for API key TTLs.; unchanged from fork base |
| `server/backend/tests/__init__.py` | Package marker; unchanged from fork base |
| `server/backend/tests/test_api_keys.py` | Tests for API key token encoding and hashing.; unchanged from fork base |
| `server/backend/tests/test_scoring.py` | Tests for confidence scoring and relevance functions.; unchanged from fork base |
| `server/backend/tests/test_ttl.py` | Tests for the TTL duration parser.; unchanged from fork base |

## Other fork modifications (outside backend source tree)

Additional changes not enumerated in the tables above include plugin rebranding
(`plugins/cq/`), deploy templates, SDK packaging, and root documentation
(`README.md`, `FORK_DELTA.md`, `NOTICE`). See [`FORK_DELTA.md`](FORK_DELTA.md)
and the git log for those surfaces.

## Open standard preserved elsewhere

These protocol portions remain upstream-aligned and are not modified in this fork:

- `LICENSE` (Apache-2.0, verbatim)
- `schema/knowledge-unit.schema.json` (the open KU schema)
- DID/KERI identity model and tier model semantics
- SDK APIs (`sdk/`) and the core MCP tool surface

## Source-of-truth references

- **Upstream**: https://github.com/mozilla-ai/cq
- **Fork**: https://github.com/OneZero1ai/8th-layer-agent
- **Project decisions**: https://github.com/OneZero1ai/crosstalk-enterprise/tree/main/docs/decisions
- **Per-PR change history**: this repository's git log
- **Trademark posture**: see [`NOTICE`](NOTICE) — Apache-2.0 §6 acknowledged; references to "cq" / "Mozilla.ai" are factual attribution only

