#!/usr/bin/env node
/**
 * @8th-layer/crosstalk-mcp — l2-only mode (the canonical install path).
 *
 * Thin MCP server that translates the inter-agent messaging tool surface
 * into HTTP calls against an 8th-Layer L2's /api/v1/crosstalk/* endpoints.
 * No local SQLite. No inbox files. No background workers. The L2 is the
 * source of truth.
 *
 * This is the default mode the marketplace plugin install gets the
 * vanilla user. Per the universe (Pass 2 Part 2 Ch 8, Plan 19 v4 Phase 3):
 *
 *   - L2 is the conversation broker
 *   - SQLite-as-cache is an optimization for the power-user case (hybrid mode)
 *   - Vanilla users never see the local cache
 *
 * MCP tool surface (5 tools — the L2-shaped subset of claude-mux's crosstalk
 * primitives; group/help/find_expert deferred to Phase 2):
 *
 *   crosstalk__send_message    POST /crosstalk/messages
 *   crosstalk__reply           POST /crosstalk/threads/{id}/messages
 *   crosstalk__check_inbox     GET  /crosstalk/inbox
 *   crosstalk__list_threads    GET  /crosstalk/threads
 *   crosstalk__close_thread    POST /crosstalk/threads/{id}/close
 *
 * Wire-format compatibility: client-generated thread_id + message_id are
 * passed through as idempotency keys (8th-layer-agent commit f0905f4),
 * so retries on flaky network are safe.
 *
 * Env-var contract:
 *
 *   CROSSTALK_L2_URL        L2 base URL (defaults to CQ_ADDR)
 *   CROSSTALK_L2_API_KEY    Bearer token (defaults to CQ_API_KEY)
 *   CROSSTALK_SESSION       Optional persona attribution; defaults to ''
 *                           (the L2 will use the API-key-bound user as
 *                           the message author; this just adds a
 *                           persona label for fine-grained attribution)
 *
 * Config is read once at server startup. A runtime config change
 * requires a server restart (matches claude-mux's MCP convention; the
 * Claude Code plugin manages restarts).
 *
 * Errors are returned to the agent as `mcp.NewToolResultError` so the
 * agent can react. Non-2xx HTTP responses are never silently swallowed.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { randomBytes } from "node:crypto";

// ---------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------

function l2BaseUrl() {
  const url = process.env.CROSSTALK_L2_URL || process.env.CQ_ADDR || "";
  // Trim trailing slash for clean concatenation.
  return url.replace(/\/+$/, "");
}

function l2ApiKey() {
  return process.env.CROSSTALK_L2_API_KEY || process.env.CQ_API_KEY || "";
}

function callerPersona() {
  return process.env.CROSSTALK_SESSION || process.env.CQ_SESSION || null;
}

function configReady() {
  return Boolean(l2BaseUrl() && l2ApiKey());
}

// ---------------------------------------------------------------------
// HTTP layer
// ---------------------------------------------------------------------

async function l2Request(method, path, body) {
  if (!configReady()) {
    throw new Error(
      "crosstalk-mcp l2-only: CROSSTALK_L2_URL/CQ_ADDR or CROSSTALK_L2_API_KEY/CQ_API_KEY not configured",
    );
  }
  const url = `${l2BaseUrl()}/crosstalk${path}`;
  const init = {
    method,
    headers: {
      Authorization: `Bearer ${l2ApiKey()}`,
      "Content-Type": "application/json",
    },
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(url, init);
  const text = await resp.text().catch(() => "");
  if (!resp.ok) {
    throw new Error(`L2 ${method} ${path} returned ${resp.status}: ${text.slice(0, 300)}`);
  }
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function newThreadId() {
  return `thread_${randomBytes(16).toString("hex")}`;
}

function newMessageId() {
  return `msg_${randomBytes(16).toString("hex")}`;
}

// ---------------------------------------------------------------------
// MCP tool definitions
// ---------------------------------------------------------------------

const TOOLS = [
  {
    name: "send_message",
    description:
      "Start a new conversation with another 8th-layer user (creates a new thread). " +
      "Use this when there's no existing thread with the recipient. Replies use the reply tool.",
    inputSchema: {
      type: "object",
      properties: {
        to: {
          type: "string",
          description: "Recipient username (must exist in the same Enterprise tenancy).",
        },
        content: {
          type: "string",
          description: "Message body.",
        },
        subject: {
          type: "string",
          description: "Optional thread subject. Used only on new-thread create.",
        },
      },
      required: ["to", "content"],
    },
  },
  {
    name: "reply",
    description:
      "Reply on an existing thread. Pass the thread_id from the inbound message or list_threads.",
    inputSchema: {
      type: "object",
      properties: {
        thread_id: {
          type: "string",
          description: "Thread ID to reply on.",
        },
        content: {
          type: "string",
          description: "Reply body.",
        },
      },
      required: ["thread_id", "content"],
    },
  },
  {
    name: "check_inbox",
    description:
      "Read your unread messages. Pass mark_read=true to atomically mark them read after fetch.",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: "Max messages to return (default 50).",
        },
        mark_read: {
          type: "boolean",
          description: "If true, populate read_at on returned messages.",
        },
      },
    },
  },
  {
    name: "list_threads",
    description:
      "List threads you participate in (or all threads in your tenant if you're an admin), most-recent first.",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: "Max threads to return (default 20).",
        },
      },
    },
  },
  {
    name: "close_thread",
    description:
      "Mark a thread as complete. Optional reason recorded in the thread metadata + activity log.",
    inputSchema: {
      type: "object",
      properties: {
        thread_id: {
          type: "string",
          description: "Thread ID to close.",
        },
        reason: {
          type: "string",
          description: "Optional close reason.",
        },
      },
      required: ["thread_id"],
    },
  },
];

// ---------------------------------------------------------------------
// Tool handlers
// ---------------------------------------------------------------------

async function handleSendMessage(args) {
  const thread_id = newThreadId();
  const message_id = newMessageId();
  const persona = callerPersona();
  const result = await l2Request("POST", "/messages", {
    thread_id,
    message_id,
    to: args.to,
    content: args.content,
    subject: args.subject ?? "",
    persona,
  });
  // L2 returns { thread_id, message_id, sent_at }; pass through.
  return {
    thread_id: result?.thread_id ?? thread_id,
    message_id: result?.message_id ?? message_id,
    sent_at: result?.sent_at,
  };
}

async function handleReply(args) {
  const message_id = newMessageId();
  const persona = callerPersona();
  const result = await l2Request(
    "POST",
    `/threads/${encodeURIComponent(args.thread_id)}/messages`,
    {
      message_id,
      content: args.content,
      persona,
    },
  );
  return {
    thread_id: args.thread_id,
    message_id: result?.message_id ?? message_id,
    sent_at: result?.sent_at,
  };
}

async function handleCheckInbox(args) {
  const limit = Math.max(1, Math.min(200, args.limit ?? 50));
  const markRead = args.mark_read === true;
  const qs = new URLSearchParams({
    limit: String(limit),
    mark_read: markRead ? "true" : "false",
  });
  return l2Request("GET", `/inbox?${qs}`);
}

async function handleListThreads(args) {
  const limit = Math.max(1, Math.min(200, args.limit ?? 20));
  const qs = new URLSearchParams({ limit: String(limit) });
  return l2Request("GET", `/threads?${qs}`);
}

async function handleCloseThread(args) {
  return l2Request("POST", `/threads/${encodeURIComponent(args.thread_id)}/close`, {
    reason: args.reason ?? null,
  });
}

const HANDLERS = {
  send_message: handleSendMessage,
  reply: handleReply,
  check_inbox: handleCheckInbox,
  list_threads: handleListThreads,
  close_thread: handleCloseThread,
};

// ---------------------------------------------------------------------
// MCP server wiring
// ---------------------------------------------------------------------

const server = new Server(
  { name: "crosstalk", version: "0.1.0-l2-only" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: TOOLS,
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;
  const handler = HANDLERS[name];
  if (!handler) {
    return {
      isError: true,
      content: [
        { type: "text", text: `Unknown tool: ${name}` },
      ],
    };
  }
  try {
    const result = await handler(args);
    return {
      content: [
        {
          type: "text",
          text: typeof result === "string" ? result : JSON.stringify(result, null, 2),
        },
      ],
    };
  } catch (err) {
    return {
      isError: true,
      content: [
        { type: "text", text: String(err.message || err) },
      ],
    };
  }
});

// Boot: stdio transport, log a one-line readiness signal to stderr so
// the harness can verify startup, then wait.
const transport = new StdioServerTransport();
process.stderr.write(
  `crosstalk-mcp l2-only: connecting to ${l2BaseUrl() || "(unconfigured)"} ` +
    `(persona=${callerPersona() ?? "(none)"})\n`,
);
await server.connect(transport);
