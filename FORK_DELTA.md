# 8th-Layer.ai agent — fork delta from `mozilla-ai/cq`

This repository is a fork of [`mozilla-ai/cq`](https://github.com/mozilla-ai/cq) maintained by [OneZero1.ai](https://github.com/OneZero1ai) for the **8th-Layer.ai** product.

cq's protocol, schema, KERI/DID identity model, tier model, and SDKs are upstream's open standard — **we adopt them unchanged**. We fork the agent-side code (per-host plugins + local MCP server) to add enterprise execution capabilities cq's reference plugin doesn't yet have, and we add an enterprise execution layer on top of the cq REST surface (with declared exceptions documented below — see "Declared exceptions to the REST contract").

See [`docs/decisions/08-agent-side-fork.md`](https://github.com/OneZero1ai/8th-layer/blob/main/docs/decisions/08-agent-side-fork.md) in the main `OneZero1ai/8th-layer` repo for the full architectural decision.

## What we add over upstream cq

(planned — none merged yet as of fork creation)

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
