# 8th-Layer.ai agent — fork delta from `mozilla-ai/cq`

This repository is a fork of [`mozilla-ai/cq`](https://github.com/mozilla-ai/cq) maintained by [OneZero1.ai](https://github.com/OneZero1ai) for the **8th-Layer.ai** product.

cq's protocol, schema, KERI/DID identity model, tier model, and SDKs are upstream's open standard — **we adopt them unchanged**. We fork the agent-side code (per-host plugins + local MCP server) to add enterprise execution capabilities cq's reference plugin doesn't yet have, and we add an enterprise execution layer on top of the cq REST surface (with declared exceptions documented below — see "Declared exceptions to the REST contract").

See [`docs/decisions/08-agent-side-fork.md`](https://github.com/OneZero1ai/8th-layer/blob/main/docs/decisions/08-agent-side-fork.md) in the main `OneZero1ai/8th-layer` repo for the full architectural decision.

## What we add over upstream cq

(see git history for what has merged; the list below is the intended delta)

- **`propose_batch` MCP tool** — stores N knowledge units in one MCP round-trip. Additive to upstream's tool surface; the per-unit `propose` is unchanged. Lets `/cq:reflect` cap its tool-call echo at a single invocation regardless of candidate count.
- **`propose_batch_file` MCP tool** — file-path sibling of `propose_batch` that reads the candidate list from an absolute JSON path instead of inlining it as a tool-call argument. Same per-candidate behavior; the response shape is compact (drops the per-stored `summary` + `tier` fields — the caller already has summaries locally because it wrote the file). Motivation: even with the single round-trip `propose_batch` gives, the *input* echo of N ~600B candidates still dominates operator-visible output during `/cq:reflect` (~5–6KB for a typical 7-candidate session). File mode moves that payload off the tool-call wire — echo drops to ~250B. The skill writes a temp file under `$XDG_CACHE_HOME/cq/` and best-effort removes it after the response. Closes agent#366.
- **Self-hosted cq binary** — the fork carries Go-side additions (e.g. `propose_batch`) that upstream's published binary does not, so the plugin fetches the binary from an 8th-Layer-owned CloudFront release host rather than upstream GitHub releases. See `plugins/cq/scripts/cq_binary.py`.
- **AIGRP client-side routing** — Agent Intelligence Graph Routing Protocol. The forked agent maintains a routing table fed by gossip + tenant directory, makes routing decisions client-side, executes peer-to-peer within trust boundaries, defers cross-trust-boundary execution to the tenant Remote for consent enforcement.
- **DID-KMS bridge** — derives a DID from a KMS-signed Persona public key (`did:web:` proxy V1; `did:keri:` V2+). Populates `provenance.proposer_did` on every Knowledge Unit.
- **Multi-tenancy hooks** — agent honors tenant + enterprise + team scope from the JWT context (mapped from `CQ_API_KEY`); routing-table entries scope-filtered.
- **Cross-team consent enforcement integration** — agent identifies cross-trust-boundary queries and routes them through the tenant Remote rather than peer-to-peer.
- (Future) Midnight ZKP-attested routing entries when cq's planned Midnight integration ships.

## What we explicitly do NOT modify

- Knowledge Unit schema (`schema/knowledge-unit.schema.json`)
- DID/KERI identity model
- Tier model semantics (Local / Remote / Global Commons)
- SDK APIs (`sdk/`)
- Cq's MCP tool surface (`propose`, `query`, `confirm`, `flag`, `reflect`, `status`, `health`)
- The shape of upstream cq endpoints (`/propose`, `/query`, `/review/*`, `/stats`, `/confirm/*`, `/flag/*`) — we add additive scope parameters and security gates (see "Declared exceptions" below) but do not change request bodies, response models, status semantics, or paths

These are the open protocol; they stay open and we want full interoperability with vanilla cq remotes and other cq-protocol-compatible clients.

## Declared exceptions to the REST contract

The line above said "we adopt the open standard unchanged." There are deliberate exceptions where the fork extends the upstream surface. Each is **additive** (does not break existing clients) and documented here in fulfillment of [`MODIFICATIONS.md`](MODIFICATIONS.md). Treat new entries to this section as needing an explicit decision, not a drift event.

### Additive scope parameters on existing endpoints

PRs #41/#42/#47 added multi-tenant scope parameters (`enterprise_id`, `group_id`, optionally `cross_group_allowed`) to `/query`, `/review/*`, and `/stats`. These resolve from the authenticated caller's user row at request time — they are NOT request-body fields the client sets, and they default to the legacy single-tenant scope when unset (so vanilla cq Remotes are unaffected).

- **What changed at the wire**: same paths, same request shapes, same response models. Behavior change: anonymous requests now require API-key auth (CRIT #33), and the response is filtered to the caller's tenant.
- **Why declared exception, not full upstream**: the multi-tenant model is a coordination question (where does tenancy live: API-key metadata, JWT claims, DID/KERI layer, or scope params?) that we shipped server-side ahead of the upstream conversation. The bucket-3 disposition (cq-fanboy 2026-05-02) is "hold + coordinate" — engage upstream when timing is right; until then, we live with the divergence.

### New endpoint families (entirely additive)

These are new namespaces under our own paths; vanilla cq has nothing at these prefixes.

- `/api/v1/aigrp/*` — Agent Intelligence Graph Routing Protocol (intra-Enterprise peer mesh)
- `/api/v1/network/dsn/*` — Distributed Semantic Network intent resolution
- `/api/v1/consults/*` — L3 live agent-to-agent consults
- `/api/v1/network/topology` — fleet visibility for the marketing aggregator
- `/api/v1/peers/*` — presence registry (per-Enterprise scoped)

These are bucket-4 (commercial moat) per cq-fanboy's classification — out of cq's open-protocol scope, designed to be replaced by cq's own equivalents only if upstream decides those problems belong in the open protocol.

### Security tightening on existing endpoints

CRIT/HIGH triage sweep #32-#39 added auth gates and tenant scoping where upstream was unauthenticated. Same paths, same request shapes; rejection is via 401/403/422. These are bucket-2 candidates (upstream once we have threat-model documentation parity); see `MODIFICATIONS.md` for the file-level catalog.

## Server-side additions (provisional, candidates to upstream)

These DO touch the server, which the policy above said we wouldn't. Each is a deliberate exception, narrow in scope, and should be proposed upstream as a PR after we've battle-tested them in our deployment:

- **`server/backend/src/cq_server/quality.py`** — propose-time content quality guards. Rejects KU shapes that are clearly placeholder (`domains:['test']`, summary=='test', summary==detail, sub-threshold lengths). The `/propose` endpoint is the choke-point because cq's PoC has no admin-side delete, so junk has to be stopped at intake. Generic content-quality, not 8L-specific — should upstream once stable. Tracked as `OneZero1ai/crosstalk-enterprise#24`.

  *Why this is a justified deviation from the "do not modify server" policy:* a forking project that deploys cq Remote in production needs intake-time integrity guards that the upstream PoC doesn't yet have. Without them, smoke-test garbage and project-internal manifesto KUs accumulate and pollute the queryable commons. The fix is general (any cq deployment benefits) so the right long-term home is upstream — but we need it deployed today.

## Active 8th-Layer fork-delta surfaces (registered 2026-05-20)

The surfaces below are merged-and-deployed deviations from upstream `mozilla-ai/cq` that must be carried forward across rebases. Each entry names the surface, what is different, the PR that landed it, and the upstream relationship.

### Enterprise Provisioning Service + signed-identity (Decision 31)

A new public-facing anonymous REST surface for enterprise onboarding that has no upstream analog. The provisioning module is a 6-phase async state machine — Ed25519 key mint (KMS `generate-data-key`, SSM SecureString fallback) → directory register (in-process today, remote HTTP call TODO) → Cloudflare DNS + ACM cert → STS AssumeRole + CloudFormation L2 stack → SES magic-link → COMPLETED. Anchors enterprise identity binding for the AIGRP tenant lifecycle and is the canonical home of the signed-identity contract.

- Files: `server/backend/src/cq_server/provisioning/` (new module), `server/backend/alembic/versions/0021_*` (+ `0021a_*` partial-unique), CORS expansion to `https://signup.8th-layer.ai`.
- Endpoints (additive, anonymous, IP rate-limited): `POST /api/v1/enterprises` (10 req/hr); `GET /api/v1/enterprises/jobs/{job_id}` (ULID, 24h post-COMPLETED).
- PRs: #228 (provisioning service + signed-identity). Decision: `OneZero1ai/8th-layer-core#61` (Decision 31).
- Upstream relationship: **additive, bucket-4 (commercial moat)**. Upstream cq has no concept of enterprises, automated onboarding, or KMS-backed key minting. Any rebase touching `cq_server/` must not disturb `provisioning/` or the `0021/0021a` migration sequence.
- Source issues: #236 (AIGRP-namespace framing), #238 (signed-identity framing), #351 (FORK_DELTA registry placeholder).

### Personas management (L2 admin control)

Per-L2 governance surface for Human→Persona assignments — not present upstream. Adds admin-gated CRUD over `persona_assignments`, integrates with the provisioning magic-link invite path via `role="enterprise_admin"` injection, and ships the L2 admin frontend tab.

- Files: `server/backend/src/cq_server/persona_routes.py` (new), `server/backend/alembic/versions/0022_*` and `0023_*`, edits to `invite_routes.py`, `auth.py`, provisioning models, store layer; `web/admin/src/pages/PersonasPage.tsx` (new) + Layout sidebar + API types.
- Endpoints (additive, admin-gated): `/admin/personas` list / create / patch / disable.
- PR: #229.
- Upstream relationship: **additive, bucket-4 (commercial moat)**. Upstream cq has no personas model; rebases that touch invite minting or auth roles must verify the persona assignment flow.
- Source issue: #237.

### `GET /consults/{thread_id}` thread-metadata endpoint

Fills an obvious gap in the upstream consults surface — previously only sub-routes (`/{thread_id}/messages`, `/{thread_id}/close`) existed; bare thread URLs returned 404. Adds a `ConsultThreadOut` response model. Auth contract matches existing consults endpoints (401 / 403 / 404). Registered at the end of the file to avoid shadowing `/inbox` and `/forward-request` parameterised paths.

- Files: `server/backend/src/cq_server/consults.py`, `server/backend/tests/test_consults.py` (+66 / −5).
- PR: #151. Closes `OneZero1ai/8th-layer-core#42`.
- Upstream relationship: **bucket-2 (upstream this sprint)** — clean, self-contained, well-tested. Candidate to PR back to `mozilla-ai/cq` so the fork can stop carrying it as delta. Until then, watch the upstream consults module for a parallel thread-metadata endpoint that could land with a different schema name/shape.
- Source issue: #260.

### `/api/v1/knowledge` alias for `/api/v1/query` + envelope-shape divergence note

Upstream `mozilla-ai/cq` PR #372 (merged 2026-05-15) renamed the knowledge-search endpoint to `/api/v1/knowledge` and unified list responses on a `{data: [...]}` envelope. Our fork carries `/api/v1/query` (returning a bare `list[KnowledgeUnit]`) — diverged from upstream at the URL layer AND the response shape.

This entry registers the divergence and adds a URL alias so upstream `cli/v0.10.0+` clients pointed at our L2 hit the same handler instead of 404'ing.

- Files: `server/backend/src/cq_server/app.py` (stacked `@api_router.get("/knowledge")` decorator on `query_units`), `server/backend/tests/test_app.py` (`test_knowledge_alias_returns_same_results`).
- Endpoint: `GET /api/v1/knowledge` — same handler as `/api/v1/query`, same response shape (bare list).
- Upstream relationship: **HOLD + COORDINATE**. URL parity is now achieved, but response shape still differs (bare list vs upstream's `{data, count}` envelope). Adopting the envelope is a coordinated SDK + server change — both our Python and Go SDKs parse the bare array in `sdk/python/src/cq/client.py:420` and `sdk/go/remote.go:227`; flipping the server response shape without lock-step SDK bumps would silently break every plugin/v0.1.0 in the field across the 10-L2 fleet. The api-keys half of upstream #372 was already done in our fork (commit `fe91491`, `ApiKeysPublic(data=..., count=...)` in `auth.py:639`).
- Suggested next move when we're ready to converge: ship SDKs with a backward-compat reader (try `data` field, fall back to bare array), wait for fleet plugin rebuild, then wrap the server response. Tracked at agent#377.
- Surfaced by: cq-fanboy crosstalk thread `197ae3491e5dcebd` (2026-05-27 fresh upstream pull).

### `CQ_QUIET_LOCAL_FALLBACK` — suppress per-candidate fallback warnings in MCP propose/propose_batch

The MCP `propose` and `propose_batch` handlers historically echoed a per-call / per-candidate warning string whenever the remote API was unreachable or rejected the request (e.g. invalid API key) and the unit fell back to local storage. In a typical reflect-cycle with N candidates that produced N identical warnings burying the operator pane.

This change adds an env-gated quiet mode (default ON) that:

- Drops the per-candidate `Warning` field on `batchStored` entries (now `omitempty` and only populated when `CQ_QUIET_LOCAL_FALLBACK=false`).
- Adds response-level `local_fallback_count` (int) and `local_fallback_reason` (string) to `batchResponse` so callers render the signal exactly once.
- Replaces the legacy `"warning: <msg>\n<json>"` envelope on single `propose` with a structured JSON wrapper carrying `local_fallback_reason` alongside the unit, again only in quiet mode.

Files: `cli/mcpserver/propose.go`, `cli/mcpserver/propose_batch.go`, `cli/mcpserver/propose_test.go`, `cli/mcpserver/propose_batch_test.go`, `cli/mcpserver/propose_quiet_test.go` (new).
Env: `CQ_QUIET_LOCAL_FALLBACK` — accepts `false`, `0`, `no`, `off` to restore legacy behavior; anything else (including unset) keeps quiet mode active.
Source issue: 8th-layer-core operator directive 2026-05-22 ("ensure our forked cq does not surface this in the foreground").
Upstream relationship: **bucket-2 (upstream this sprint)** — clean UX-tightening change, response schema is backward-compatible for existing callers (new fields are `omitempty`, existing `warning` field still populated when env opts back into legacy mode). Candidate to PR back to `mozilla-ai/cq` once the skill-side `/8l-cq:reflect` rendering update lands.

## Sync discipline

- **Fork base**: pinned at the cq commit at the time of fork creation (2026-04-26).
- **Upstream sync cadence**: weekly checkpoint via the [cq-fanboy](https://github.com/dwinter3/cq-fanboy) trajectory pipeline; full rebase quarterly or on cq-tagged release. (Bumped from monthly per cq-fanboy 2026-05-02 — cq's velocity is high enough that monthly produces 4 weeks of conflict-debt per pass.)
- **Contribution back**: bug fixes + perf + protocol clarifications get pushed upstream as PRs to `mozilla-ai/cq`. The `quality.py` module above is on this list. Security-sweep input-validation work (#35/#37/#39) is also a candidate.
- **Stays in fork**: enterprise-specific capabilities (AIGRP routing, DID-KMS bridge, directory + reputation log, multi-tenancy, FIPS hooks) — not relevant to upstream's open-standard project scope.

### Five-bucket discipline (per cq-fanboy 2026-05-02)

Every fork-delta entry must be in one of:

1. **ADOPT NOW** — cq has it; rip out our reinvention. Current items: Alembic + `Store` protocol rebase, `cq-schema` package pin, `cq.scoring` adoption, stale `reflect` MCP refs.
2. **UPSTREAM THIS SPRINT** — we built; cq needs. Current items: `quality.py`, security-sweep input validation, schema-extension mechanism proposal.
3. **HOLD + COORDINATE** — both sides will eventually need; engage cq early to avoid contract divergence. Current items: multi-tenant scope params, per-L2 + enterprise-root Ed25519 keys, JWT-vs-tenant-scoped-keys auth model. Engagement timing is operator's call (not always immediate).
4. **HOLD WITHOUT COORDINATION** — commercial moat. AIGRP / DSN / consults namespaces, directory + reputation log, AWS Marketplace + ECS deploy templates.
5. **PROCESS DISCIPLINE** — sustains the relationship. This document is the artefact for that discipline.

New entries to "Declared exceptions to the REST contract" must be tagged with their bucket.

## Mozilla.AI partnership

We engage upstream transparently. The fork is a delineation of where the open-source ends and the commercial differentiator begins, not a competitive split. See the partnership conversation framing in [`docs/external/01-one-pager.md`](https://github.com/OneZero1ai/8th-layer/blob/main/docs/external/01-one-pager.md) of the main repo.

## Repository

- This repo: `OneZero1ai/8th-layer-agent` — the fork
- Main repo: `OneZero1ai/8th-layer` — tenant code, decision docs, specs, vision
- Marketplace: `OneZero1ai/8th-layer-marketplace` — Claude Code plugin marketplace catalog pointing at this fork

## License

Apache-2.0 (inherited from upstream cq).
