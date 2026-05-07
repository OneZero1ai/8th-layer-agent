/**
 * L2 sync layer (#126) — dual-write to 8th-layer-agent's L2.
 *
 * This module is the bridge between the local-first SQLite path
 * (server.js handlers) and the L2's /api/v1/crosstalk/* endpoints.
 *
 * Architecture (operator-validated dual-write):
 *
 *   1. Local primary path (always runs, regardless of CROSSTALK_BACKEND):
 *      send_message handler INSERTs into local SQLite + writes to recipient's
 *      inbox file. The recipient's pane gets the message via the existing
 *      claude-mux inbox-file polling. This works whether L2 is up or down.
 *
 *   2. L2 secondary path (only when CROSSTALK_BACKEND=l2-http):
 *      After the local write succeeds, the handler calls one of the
 *      sync* functions in this module. On L2 success → done. On L2
 *      failure → enqueue to pending_l2_sync; retry later via drain loop.
 *
 *   3. Drain loop:
 *      Background timer (default 30s) wakes up, reads pending_l2_sync
 *      rows in attempt-count-asc order, retries each. Successful
 *      retries delete the row; failed retries bump attempts + record
 *      last_error. After N failures (cap 10), the row is left in place
 *      with a "stuck" flag for operator inspection.
 *
 * Idempotency: every sync call provides client-generated thread_id +
 * message_id. The L2 accepts these as idempotency keys (commit f0905f4
 * in 8th-layer-agent), so retries never duplicate. See the propose
 * endpoint comments for full semantics.
 *
 * Env-var contract:
 *   CROSSTALK_BACKEND   ∈ {local-sqlite, l2-http}, default local-sqlite
 *   CROSSTALK_L2_URL    base URL (defaults to CQ_ADDR)
 *   CROSSTALK_L2_API_KEY bearer token (defaults to CQ_API_KEY)
 */

import {
  bumpPendingL2SyncAttempt,
  countPendingL2Sync,
  deletePendingL2Sync,
  enqueuePendingL2Sync,
  listPendingL2Sync,
} from "./db.js";

const MAX_ATTEMPTS = 10;
const DRAIN_INTERVAL_MS = 30_000;

/**
 * Returns true if l2-http backend is enabled. Cheap; reads env every call
 * so a runtime change to CROSSTALK_BACKEND takes effect on the next call
 * without a restart (useful for testing).
 */
export function l2BackendEnabled() {
  return (process.env.CROSSTALK_BACKEND || "local-sqlite").toLowerCase() === "l2-http";
}

function l2BaseUrl() {
  // Trim trailing slash so endpoint suffixes can be appended cleanly.
  const url = process.env.CROSSTALK_L2_URL || process.env.CQ_ADDR || "";
  return url.replace(/\/+$/, "");
}

function l2ApiKey() {
  return process.env.CROSSTALK_L2_API_KEY || process.env.CQ_API_KEY || "";
}

/**
 * One HTTP call to an L2 endpoint. Returns parsed JSON on 2xx,
 * throws Error on network failure or non-2xx response. The caller
 * decides whether to enqueue on failure.
 */
async function l2Post(path, body) {
  const url = `${l2BaseUrl()}/crosstalk${path}`;
  const apiKey = l2ApiKey();
  if (!url || !apiKey) {
    throw new Error("L2 sync called but CROSSTALK_L2_URL or CROSSTALK_L2_API_KEY not configured");
  }
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`L2 ${path} returned ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------
// Per-operation sync functions
//
// Each one wraps the L2 call in try/catch. On success, return.
// On failure, enqueue to pending_l2_sync with the original args + the
// error message. The caller (server.js handler) is unaffected — the
// local primary path already succeeded.
// ---------------------------------------------------------------------

/**
 * Sync a send_message to the L2.
 * @param {object} args
 * @param {string} args.thread_id     - local-generated, used as idempotency key
 * @param {string} args.message_id    - local-generated, used as idempotency key
 * @param {string} args.to            - recipient username
 * @param {string} args.content
 * @param {string} [args.persona]
 * @param {string} [args.subject]
 */
export async function syncMessageToL2(args) {
  if (!l2BackendEnabled()) return;
  try {
    await l2Post("/messages", {
      thread_id: args.thread_id,
      message_id: args.message_id,
      to: args.to,
      content: args.content,
      persona: args.persona,
      subject: args.subject,
    });
  } catch (err) {
    enqueuePendingL2Sync("send_message", args, String(err.message || err));
  }
}

/**
 * Sync a reply to the L2.
 * @param {object} args
 * @param {string} args.thread_id
 * @param {string} args.message_id
 * @param {string} args.content
 * @param {string} [args.persona]
 */
export async function syncReplyToL2(args) {
  if (!l2BackendEnabled()) return;
  try {
    await l2Post(`/threads/${encodeURIComponent(args.thread_id)}/messages`, {
      message_id: args.message_id,
      content: args.content,
      persona: args.persona,
    });
  } catch (err) {
    enqueuePendingL2Sync("reply", args, String(err.message || err));
  }
}

/**
 * Sync a close to the L2.
 * @param {object} args
 * @param {string} args.thread_id
 * @param {string} [args.reason]
 */
export async function syncCloseToL2(args) {
  if (!l2BackendEnabled()) return;
  try {
    await l2Post(`/threads/${encodeURIComponent(args.thread_id)}/close`, {
      reason: args.reason,
    });
  } catch (err) {
    enqueuePendingL2Sync("close_thread", args, String(err.message || err));
  }
}

// ---------------------------------------------------------------------
// Drain loop
// ---------------------------------------------------------------------

/**
 * Drain pending sync rows. Called by the background timer (and by
 * tests directly). Returns {drained, remaining, stuck} counts for
 * observability.
 *
 * Drain order: lowest-attempts-first, oldest-first within. Skips rows
 * that have hit MAX_ATTEMPTS (left for operator inspection).
 */
export async function drainPendingSync() {
  if (!l2BackendEnabled()) {
    return { drained: 0, remaining: 0, stuck: 0 };
  }

  const rows = listPendingL2Sync(50);
  let drained = 0;
  let stuck = 0;

  for (const row of rows) {
    if (row.attempts >= MAX_ATTEMPTS) {
      stuck += 1;
      continue;
    }

    let payload;
    try {
      payload = JSON.parse(row.payload);
    } catch (err) {
      // Malformed payload — won't recover; mark stuck by bumping
      // attempts to MAX so it stops being retried.
      bumpPendingL2SyncAttempt(row.id, `malformed payload: ${err.message}`);
      stuck += 1;
      continue;
    }

    try {
      if (row.op === "send_message") {
        await l2Post("/messages", {
          thread_id: payload.thread_id,
          message_id: payload.message_id,
          to: payload.to,
          content: payload.content,
          persona: payload.persona,
          subject: payload.subject,
        });
      } else if (row.op === "reply") {
        await l2Post(`/threads/${encodeURIComponent(payload.thread_id)}/messages`, {
          message_id: payload.message_id,
          content: payload.content,
          persona: payload.persona,
        });
      } else if (row.op === "close_thread") {
        await l2Post(`/threads/${encodeURIComponent(payload.thread_id)}/close`, {
          reason: payload.reason,
        });
      } else {
        bumpPendingL2SyncAttempt(row.id, `unknown op: ${row.op}`);
        stuck += 1;
        continue;
      }
      deletePendingL2Sync(row.id);
      drained += 1;
    } catch (err) {
      bumpPendingL2SyncAttempt(row.id, String(err.message || err));
    }
  }

  return {
    drained,
    remaining: countPendingL2Sync(),
    stuck,
  };
}

let drainTimer = null;

/**
 * Start the background drain timer. Idempotent — calling twice has no
 * effect. Stop with stopDrainLoop().
 */
export function startDrainLoop(intervalMs = DRAIN_INTERVAL_MS) {
  if (drainTimer || !l2BackendEnabled()) return;
  drainTimer = setInterval(() => {
    drainPendingSync().catch((err) => {
      // Drain failures are silent; they retry on next tick. The
      // pending_l2_sync row's last_error already records the cause.
      // Avoid log spam on persistent network outages.
      if (process.env.CROSSTALK_DEBUG) {
        // eslint-disable-next-line no-console
        console.error("crosstalk-mcp drain error:", err.message);
      }
    });
  }, intervalMs);
  // Don't keep the process alive solely for the timer.
  drainTimer.unref?.();
}

export function stopDrainLoop() {
  if (drainTimer) {
    clearInterval(drainTimer);
    drainTimer = null;
  }
}
