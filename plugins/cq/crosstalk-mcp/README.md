# `@8th-layer/crosstalk-mcp`

The 8th-Layer crosstalk MCP server — local-first inter-agent messaging with L2 sync.

## Provenance

This package is the productization of `claude-mux/crosstalk/`. claude-mux was the prototype that inspired 8th-Layer's L2 consult primitive; this is the same idea arriving at its proper home: distributed via the 8th-Layer marketplace, sharing types with the L2, owned by the 8th-Layer-agent team going forward.

The original is MIT-licensed; the relocation inherits operator-validated content under Apache-2.0 (matching the rest of `8th-layer-agent`).

## Status

🚧 **Phase 1 in progress** — see [`OneZero1ai/8th-layer-agent#126`](https://github.com/OneZero1ai/8th-layer-agent/issues/126).

The current code is a **verbatim copy of claude-mux/crosstalk's source** (server.js + db.js + find-expert.js). It's the baseline for the dual-write integration that's the actual work of #126.

What's done:
- ✅ Source copied as baseline
- ✅ Package shape established (`package.json`, dir layout)
- ✅ L2 endpoints already exist + accept client-provided IDs (8th-layer-agent#124, commit `f0905f4`)

What's pending (the real work):
- ⏳ Strip claude-mux-specific features that don't belong here (wake-on-message, trust markers, subagent-tag scoping — see [Out of scope](#out-of-scope))
- ⏳ Add `pending_l2_sync` table to db.js
- ⏳ Add `src/l2-sync.js` module — `syncMessageToL2`, `syncReplyToL2`, `syncCloseToL2`, `drainPendingSync`
- ⏳ Wire dual-write into server.js handlers (`send_message`, `reply`, `close_thread`)
- ⏳ Background drain timer in server startup
- ⏳ Env-var contract: `CROSSTALK_BACKEND`, `CROSSTALK_L2_URL`, `CROSSTALK_L2_API_KEY`
- ⏳ `crosstalk-mcp migrate-to-l2 [--dry-run]` command
- ⏳ Tests against TeamDW

## Architecture

**Dual-write with local-first delivery + offline-buffered L2 sync.**

1. **Primary path (always works):** local agent:agent via SQLite + inbox files. The recipient's pane gets the message via the existing inbox-file polling mechanism (see "Inbox files are the actual delivery mechanism" gotcha below). Doesn't care if L2 is down.

2. **Secondary path (persistence/audit):** every send/reply/close also POSTed to L2.
   - L2 up → POST live, message marked synced
   - L2 down → row in `pending_l2_sync` table, queued for drain
   - Background drain timer (every 30s when l2-http enabled) re-tries queued operations on next successful network event

3. **Idempotency:** L2 endpoints accept client-provided `thread_id` + `message_id`, so retries are safe and thread refs stay stable across local + L2 storage. See `f0905f4` in the parent repo.

## Env-var contract

| Variable | Default | Description |
|---|---|---|
| `CROSSTALK_BACKEND` | `local-sqlite` | `local-sqlite` (no L2) or `l2-http` (dual-write) |
| `CROSSTALK_L2_URL` | falls back to `CQ_ADDR` | L2 base URL; crosstalk endpoints live at `<URL>/crosstalk/*` |
| `CROSSTALK_L2_API_KEY` | falls back to `CQ_API_KEY` | Bearer token for L2 calls |

Default behavior is `local-sqlite` — no behavior change unless explicitly opted in. This is the operator's gate before prod cutover.

## In scope (Phase 1)

- 1:1 messages — `send_message`, `reply`, `check_inbox`, `close_thread`, `list_threads`, `adopt_thread`, `reject_routing`
- Group messages — `group(participants, subject, content)`, fan-out delivery
- Dual-write to L2 with offline buffer
- Migrate command for existing-history bulk upload
- WAL mode + busy_timeout=3000 (preserves claude-mux's concurrency guarantees)

## Out of scope (Phase 2 / never)

- **Wake-on-message** (`enqueueWakeRequest`, `wakeEligibility`, `checkWakeChainDepth`) — laptop-fleet-orchestration; stays in claude-mux's session manager. The MCP can return "recipient offline; queued" without trying to relaunch.
- **Trust markers** (`loadTrustConfig`, `isTrustedSender`, `trustMarker`) — obsolete in multi-tenant L2 world; tenancy is the trust model.
- **Subagent-tag scoping** (`from_tag`, `to_tag`, `pendingRoutings`, `adoptThread`) — Claude Code subagent concept; orthogonal to crosstalk protocol. Schema columns kept for forward-compat; filtering stays claude-mux-side.
- **Help/find-expert/haiku-gate/learned-expertise** — separate domain (broadcast + topic-routing + budget-gated dispatch). Worth being part of this same package eventually but a much bigger lift; defer to **Phase 2**.

## Inbox files — the actual delivery mechanism

Per claude-mux's gotcha list (2026-05-07): SQLite is the audit log; **inbox files at `~/.claude-mux/<session>/inbox.jsonl` are how messages reach recipient panes**. claude-mux's hook system polls these files and injects `← crosstalk:` notifications.

Phase 1 design preserves this: local SQLite + inbox-file delivery stays the primary path on the operator's laptop. L2 is an additional sink, not a replacement. The l2-http backend dual-writes; it does not replace the inbox-file delivery channel.

A future model (Phase 3+) where the L2 itself drives delivery — long-poll, SSE, webhook — would change this. For now, pure dual-write keeps semantics identical to claude-mux's crosstalk.

## Coordination

- Source repo: `OneZero1ai/8th-layer-agent`
- Tracking issue: [#126](https://github.com/OneZero1ai/8th-layer-agent/issues/126)
- L2 endpoints (companion, shipped): [#124](https://github.com/OneZero1ai/8th-layer-agent/issues/124)
- L2 idempotency support: commit `f0905f4`
- claude-mux validation partner: standing by; will re-run Plan-21 scenario 4 against this package once draft is up
- TeamDW endpoint for live testing: `https://team-dw.8th-layer.ai/api/v1/crosstalk/*` (canonical; raw ALB hostname is internal-only)
- Plan-21 scenario 4 (cutover gate): `crosstalk-enterprise/docs/plans/21-teamdw-test-orchestration.md`

## License

Apache-2.0. The original claude-mux code is MIT; the relocation inherits operator-validated content under Apache-2.0 alongside the rest of `8th-layer-agent`.
