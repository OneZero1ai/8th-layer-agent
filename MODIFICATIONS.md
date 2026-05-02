# Modifications from upstream `mozilla-ai/cq`

This file states the modifications made by OneZero1.ai to the upstream
[`mozilla-ai/cq`](https://github.com/mozilla-ai/cq) codebase, in fulfillment
of [Apache License, Version 2.0](LICENSE) §4(b).

For project-level rationale and the upstream-vs-fork delta narrative, see
[`FORK_DELTA.md`](FORK_DELTA.md). For per-PR detail, see this repository's
[git log](https://github.com/OneZero1ai/8th-layer-agent/commits/main).

## Fork base

- Forked from `mozilla-ai/cq` on **2026-04-26**.
- The exact upstream commit pinned at fork creation is recorded in
  [`FORK_DELTA.md`](FORK_DELTA.md).

## Apache-2.0 §4(b) compliance posture

Apache-2.0 §4(b) requires that "any modified files" carry "prominent notices
stating that You changed the files." This document is the prominent notice
covering all modified files in this repository. We do not maintain per-file
modification headers; this manifest is exhaustive and is updated whenever a
file's status changes.

## New files (entirely OneZero1.ai)

These files do not exist in upstream `mozilla-ai/cq` and are wholly authored
by OneZero1.ai:

| File | Purpose |
|---|---|
| `server/backend/src/cq_server/aigrp.py` | AIGRP (Agent Intelligence Graph Routing Protocol) — intra-Enterprise peer-mesh routing |
| `server/backend/src/cq_server/network.py` | DSN (Distributed Semantic Network) intent resolution + topology |
| `server/backend/src/cq_server/consults.py` | L3 live consults — same-L2 + cross-L2 routing |
| `server/backend/src/cq_server/directory_client.py` | Sprint 3 client for the public 8th-Layer Directory |
| `server/backend/src/cq_server/quality.py` | Propose-quality guards (candidate to upstream) |
| `server/backend/src/cq_server/embed.py` | Bedrock Titan v2 embeddings (8th-Layer-specific embedding setup) |
| `server/backend/src/cq_server/tables.py` | Multi-tenant + AIGRP schema additions |
| `server/backend/tests/test_aigrp_*.py` | AIGRP test suites |
| `server/backend/tests/test_network_*.py` | DSN test suites |
| `server/backend/tests/test_consults.py`, `test_cross_l2_routing.py`, `test_forward_query.py` | Consults + forward-query tests |
| `server/backend/tests/test_directory_client.py` | Directory client tests |
| `server/local-demo/**` | Local Docker mesh demo |
| `FORK_DELTA.md`, `MODIFICATIONS.md`, `NOTICE` | This fork's documentation |

## Modified files (additions on top of upstream)

These files exist in upstream `mozilla-ai/cq` and have been modified by
OneZero1.ai. The git diff against the fork-base commit is the authoritative
record of changes.

| File | Nature of modification |
|---|---|
| `server/backend/src/cq_server/app.py` | Multi-tenant scope params on `/query`, `/review/*`, `/stats`; AIGRP forward-query handler; directory-client lifespan task; security fixes (CRIT #32/#33/#34, HIGH #35/#37/#39) |
| `server/backend/src/cq_server/store/__init__.py` | Tenant scoping on existing CRUD methods; new methods for AIGRP peers, consults, directory peerings, cross-L2 audit |
| `server/backend/src/cq_server/auth.py` | Admin role; tenant scope resolution from user row |
| `server/backend/src/cq_server/review.py` | Admin gate (`require_admin`) on every route; tenant scoping on aggregate methods |
| `server/backend/src/cq_server/deps.py` | Tenant scope helpers |
| `server/backend/tests/test_app.py`, `test_review.py`, `test_admin_delete.py` | Tests adapted for the multi-tenant surface |
| `server/backend/pyproject.toml` | Added `cryptography`, `rfc8785`, `httpx` for the directory client |
| `server/scripts/seed-users.py`, `server/local-demo/bin/seed-admin.sh` | Set `role='admin'` (post-CRIT #32) |
| `plugins/cq/.claude-plugin/plugin.json` | Renamed plugin to `8l-cq`; OneZero1.ai authorship; updated description, repository, keywords |
| `README.md` | Prepended 8th-Layer.ai fork-disclosure header (upstream cq README preserved verbatim below the separator) |

## Files unchanged from upstream (the open standard)

These are the open-protocol portions we explicitly preserve unchanged:

- `LICENSE` (Apache-2.0, verbatim)
- `schema/knowledge-unit.schema.json` (the open KU schema)
- The MCP server name `cq` inside the renamed `8l-cq` plugin (kept for protocol compatibility — agent code calling `mcp__cq__*` continues to work)
- DID/KERI identity model
- Tier model semantics (Local / Remote / Global Commons)
- SDK APIs (`sdk/`)
- Cq's MCP tool surface (`propose`, `query`, `confirm`, `flag`, `reflect`, `status`, `health`)

## Source-of-truth references

- **Upstream**: https://github.com/mozilla-ai/cq
- **Fork**: https://github.com/OneZero1ai/8th-layer-agent
- **Project decisions**: https://github.com/OneZero1ai/crosstalk-enterprise/tree/main/docs/decisions
- **Per-PR change history**: this repository's git log
- **Trademark posture**: see [`NOTICE`](NOTICE) — Apache-2.0 §6 acknowledged; references to "cq" / "Mozilla.ai" are factual attribution only
