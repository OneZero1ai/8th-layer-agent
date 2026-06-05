# 8th-Layer Integration: Amazon Quick Configuration

## Overview

This document describes how the 8th-Layer Commons (`8l-cq`) integrates with Amazon Quick desktop sessions, including the activation model, tool flow, and known limitations.

## Architecture

Amazon Quick connects to the 8th-Layer Commons via a **Liaison Server** (remote MCP) exposing five tools:

| Tool | Purpose |
|------|---------|
| `query` | Semantic search by domain tags; returns matching Knowledge Units (KUs) with similarity scores |
| `propose` | Record a new KU after solving a non-trivial problem |
| `propose_batch` | Bulk-propose multiple KUs in a single call |
| `confirm` | Endorse an existing KU after applying it successfully (confidence ↑) |
| `flag` | Mark a KU as incorrect, stale, or duplicate (confidence ↓) |
| `status` | Show tier counts, domain distribution, and confidence histogram |

## Activation Model

The 8th-Layer skill is loaded into the Amazon Quick agent via a **skill definition** — a structured instruction block that tells the agent when and how to use the tools. The skill declares:

> "ALWAYS use this skill BEFORE answering any technical question, AWS question, infrastructure question, code question, debugging question, troubleshooting question, architecture question, or 'how do I' question."

### Query: Deterministic Hook

The **query** step is structured as a **mandatory first-action** — a deterministic hook that fires unconditionally at the start of any substantive turn:

```
User prompt arrives
  → Extract 1-3 domain keywords
  → Call query(domains=[...], limit=5)
  → Weave matching KUs into answer (if similarity > 0.3)
```

This is the reliable side of the integration. The instruction is unconditional ("ALWAYS query before any substantive turn"), simple to execute (one tool call), and positioned as a prerequisite before any other reasoning.

### Propose: Non-Deterministic / Agentic

The **propose** step is structured as a **soft behavioral instruction** — a discretionary action at the end of a turn:

> "At the end of any non-trivial turn where you SOLVED something and didn't have a matching KU at the start, consider proposing back."

This is an agentic judgment call. The agent evaluates:
1. Was the problem non-trivial?
2. Was the solution novel (no existing KU matched)?
3. Is the insight generalizable enough to help future sessions?

There is no structural enforcement — it depends on the agent's assessment of novelty.

## Known Limitations

### 1. Skill Activation Is Probabilistic

The entire 8th-Layer loop depends on the skill being **loaded and activated**. This happens via prompt instruction, not architectural enforcement. If the agent:
- Doesn't recognize the turn as "substantive" or "technical"
- Is deep in a complex multi-step task and skips the load
- Hits context window pressure and omits the query step

...then the entire knowledge layer is bypassed silently.

**Impact**: No query → the agent may re-solve problems the fleet already knows. No propose → novel insights are lost.

### 2. No Middleware / Structural Guarantee

There is no middleware layer intercepting agent turns to force the query. The "deterministic hook" is deterministic only in the sense that the **instruction** is unconditional — but LLM instruction adherence is inherently probabilistic. The system relies on prompt compliance, not code-level enforcement.

### 3. Propose Reliability Is Lower Than Query

The propose step has strictly worse reliability characteristics than query because:
- It's positioned at the **end** of a turn (easier to forget after complex work)
- It requires a **judgment call** (novelty assessment) rather than a simple action
- It's a "nice to have" rather than a prerequisite for answering

This means the commons grows slower than it theoretically could.

### 4. Context Window Competition

The skill instructions, tool schemas, and KU results all consume context window budget. In long conversations or complex tasks, there's implicit pressure to skip the 8th-Layer loop to preserve context for the primary task.

### 5. Single-Session Scope Per Turn

The query happens once at the start of a turn. If the agent discovers mid-turn that a *different* set of domain tags would be relevant, it typically doesn't re-query. The initial keyword extraction determines what knowledge is surfaced.

## Possible Hardening Approaches

1. **Structured post-turn evaluation**: Make propose a mandatory evaluation step ("ALWAYS log a propose/skip decision with reasoning") even if the actual proposal is conditional.

2. **Architectural pre-hook**: A wrapper layer that intercepts every agent turn and forces a query before the agent's reasoning begins — removing it from prompt compliance entirely.

3. **Activation logging**: Track which turns activated the skill and which didn't, enabling observability into skip rates.

4. **Confidence decay**: KUs that aren't confirmed within N days lose confidence automatically, preventing stale knowledge from persisting without active maintenance.

## Deep Configuration Details

### MCP Server Wiring

The 8th-Layer Commons is exposed to Amazon Quick as a **remote MCP server** (Liaison Server). The connection is configured in Quick's MCP settings:

- **Protocol**: MCP (Model Context Protocol) over HTTPS
- **Server name**: `8l-cq`
- **Server type**: Remote (Liaison Server)
- **Discovery**: Quick connects to the remote MCP URL and auto-discovers available tools (6 tools)

Once wired, the tools appear in Quick's tool registry under the `user_mcp__8l_cq` namespace, prefixed with `8l_cq__`.

### Skill Definition (Full Text)

The skill that governs activation and behavior is registered in Amazon Quick's skill system as `8th-layer-fleet-knowledge`. Here is the complete activation configuration:

```yaml
name: 8th-layer-fleet-knowledge
display_name: 8th-Layer Fleet Knowledge
icon: "📡"
description: >
  ALWAYS use this skill BEFORE answering any technical question, AWS question,
  infrastructure question, code question, debugging question, troubleshooting
  question, architecture question, or 'how do I' question. This skill connects
  to the team's accumulated knowledge graph and prevents you from re-inventing
  answers your team already figured out.
activation_signals:
  - 'AWS', 'ECS', 'CFN', 'CloudFormation', 'S3', 'IAM'
  - 'task stopped', 'why does', 'how do I'
  - 'debugging', 'error', 'fix', 'pattern for', 'how to', 'troubleshoot'
  - any error code, any stack trace
  - any infrastructure topic
  - any Python/Go/TypeScript code question
skip_conditions:
  - Pure pleasantries ('hi', 'thanks')
  - One-word replies
```

### Tool Schemas (JSON)

The six tools exposed by the `8l-cq` MCP server:

```json
{
  "8l_cq__query": {
    "description": "Search for relevant knowledge units by domain tags.",
    "parameters": {
      "required": ["domains"],
      "properties": {
        "domains": { "type": "array", "items": { "type": "string" }, "description": "Domain tags to search." },
        "limit": { "type": "number", "description": "Maximum results to return (default 5, max 50)." },
        "pattern": { "type": "string", "description": "Filter by pattern." },
        "frameworks": { "type": "array", "items": { "type": "string" }, "description": "Filter by frameworks." },
        "languages": { "type": "array", "items": { "type": "string" }, "description": "Filter by programming languages." }
      }
    }
  },
  "8l_cq__propose": {
    "description": "Propose a new knowledge unit.",
    "parameters": {
      "required": ["summary", "detail", "action", "domains"],
      "properties": {
        "summary": { "type": "string", "description": "Brief summary of the insight." },
        "detail": { "type": "string", "description": "Detailed explanation of what was discovered." },
        "action": { "type": "string", "description": "Recommended action for agents encountering this situation." },
        "domains": { "type": "array", "items": { "type": "string" }, "description": "Domain tags for this knowledge." },
        "pattern": { "type": "string", "description": "Pattern name." },
        "frameworks": { "type": "array", "items": { "type": "string" } },
        "languages": { "type": "array", "items": { "type": "string" } }
      }
    }
  },
  "8l_cq__propose_batch": {
    "description": "Propose multiple knowledge units in a single call.",
    "parameters": {
      "required": ["candidates"],
      "properties": {
        "candidates": { "type": "array", "items": { "$ref": "#/8l_cq__propose/parameters" } }
      }
    }
  },
  "8l_cq__confirm": {
    "description": "Confirm a knowledge unit proved correct, boosting its confidence score.",
    "parameters": {
      "required": ["unit_id"],
      "properties": {
        "unit_id": { "type": "string", "description": "ID of the knowledge unit to confirm." }
      }
    }
  },
  "8l_cq__flag": {
    "description": "Flag a knowledge unit as problematic, reducing its confidence score.",
    "parameters": {
      "required": ["unit_id", "reason"],
      "properties": {
        "unit_id": { "type": "string", "description": "ID of the knowledge unit to flag." },
        "reason": { "type": "string", "enum": ["duplicate", "incorrect", "stale"] },
        "detail": { "type": "string", "description": "Optional detail for this flag." },
        "duplicate_of": { "type": "string", "description": "Original unit ID when reason is duplicate." }
      }
    }
  },
  "8l_cq__status": {
    "description": "Show knowledge store statistics.",
    "parameters": { "required": [], "properties": {} }
  }
}
```

### Skill Activation Mechanics

Amazon Quick uses a two-phase tool loading system:

1. **Skill Registry** — Skills are listed in the system prompt under `<available_skills>`. The agent sees their names, descriptions, and tool counts but **cannot call tools** until the skill is loaded.

2. **`load_skill` Call** — The agent must explicitly call `load_skill("8th-layer-fleet-knowledge")` to load the skill's instructions and make the MCP tools callable. This is the point of activation.

3. **Tool Availability** — After loading, the `8l_cq__*` tools become available in the agent's tool registry for the remainder of the conversation turn.

This two-phase design means:
- The skill description in the system prompt acts as the **activation trigger** (the agent reads it and decides to load)
- The full skill instructions (including the "ALWAYS query first" mandate) are only present **after** `load_skill` is called
- If the agent never calls `load_skill`, it never sees the mandatory query instruction

### Session Identity

| Field | Value |
|-------|-------|
| Session identifier | `quick-desktop-spike` |
| Platform | Amazon Quick (desktop, macOS) |
| Agent model | Claude (Anthropic), via Amazon Quick orchestration |
| MCP transport | Remote (HTTPS) |
| Store tier | Local (single-node) |
| Mesh participation | claude-mux (multi-session) |

### Knowledge Unit Schema

Each KU stored in the commons has this structure:

```json
{
  "id": "ku_<uuid>",
  "version": 1,
  "domains": ["tag1", "tag2"],
  "insight": {
    "summary": "One-line description",
    "detail": "2-3 sentence explanation of what was discovered",
    "action": "Concrete guidance: when you hit X, do Y"
  },
  "context": {
    "languages": ["python"],
    "frameworks": ["fastapi"],
    "pattern": "pattern-name-kebab-case"
  },
  "evidence": {
    "confidence": 0.5,
    "confirmations": 1,
    "first_observed": "ISO-8601",
    "last_confirmed": "ISO-8601"
  },
  "tier": "local",
  "created_by": "",
  "flags": null
}
```

## Session Identity

In the current configuration, Amazon Quick sessions identify as `quick-desktop-spike` in the claude-mux mesh. The 8l-cq store is local-tier (8 KUs as of 2026-06-05), with confidence distribution concentrated in the 0.5–0.7 range.

---

*Last updated: 2026-06-05*
