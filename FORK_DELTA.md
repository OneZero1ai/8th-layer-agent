# 8th-Layer.ai agent — fork delta from `mozilla-ai/cq`

This repository is a fork of [`mozilla-ai/cq`](https://github.com/mozilla-ai/cq) maintained by [OneZero1.ai](https://github.com/OneZero1ai) for the **8th-Layer.ai** product.

cq's protocol, schema, REST contract, KERI/DID identity model, tier model, and SDKs are upstream's open standard — **we adopt them unchanged**. We fork the agent-side code (per-host plugins + local MCP server) to add enterprise execution capabilities cq's reference plugin doesn't yet have.

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
- REST API contract (`server/`)
- DID/KERI identity model
- Tier model semantics (Local / Remote / Global Commons)
- SDK APIs (`sdk/`)
- Cq's MCP tool surface (`propose`, `query`, `confirm`, `flag`, `reflect`, `status`, `health`)

These are the open protocol; they stay open and we want full interoperability with vanilla cq remotes and other cq-protocol-compatible clients.

## Sync discipline

- **Fork base**: pinned at the cq commit at the time of fork creation (2026-04-26).
- **Upstream sync cadence**: monthly, or on cq-tagged release.
- **Contribution back**: bug fixes + perf + protocol clarifications get pushed upstream as PRs to `mozilla-ai/cq`.
- **Stays in fork**: enterprise-specific capabilities (AIGRP routing, DID-KMS bridge, multi-tenancy, FIPS hooks) — not relevant to upstream's open-standard project scope.

## Mozilla.AI partnership

We engage upstream transparently. The fork is a delineation of where the open-source ends and the commercial differentiator begins, not a competitive split. See the partnership conversation framing in [`docs/external/01-one-pager.md`](https://github.com/OneZero1ai/8th-layer/blob/main/docs/external/01-one-pager.md) of the main repo.

## Repository

- This repo: `OneZero1ai/8th-layer-agent` — the fork
- Main repo: `OneZero1ai/8th-layer` — tenant code, decision docs, specs, vision
- Marketplace: `OneZero1ai/8th-layer-marketplace` — Claude Code plugin marketplace catalog pointing at this fork

## License

Apache-2.0 (inherited from upstream cq).
