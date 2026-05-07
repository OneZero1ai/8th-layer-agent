import Database from "better-sqlite3";
import { join } from "path";
import { homedir } from "os";
import { mkdirSync } from "fs";

const DB_PATH = join(homedir(), ".claude-mux", "crosstalk.db");

let db = null;

export function getDb() {
  if (db) return db;
  mkdirSync(join(homedir(), ".claude-mux"), { recursive: true });
  db = new Database(DB_PATH);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 3000");
  db.exec(`
    CREATE TABLE IF NOT EXISTS threads (
      id TEXT PRIMARY KEY,
      participants TEXT NOT NULL,
      initiator TEXT NOT NULL,
      subject TEXT,
      created TEXT NOT NULL,
      updated TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      closed_by TEXT,
      close_reason TEXT,
      kind TEXT NOT NULL DEFAULT 'pair',
      policy TEXT,
      resolution TEXT
    );
    CREATE TABLE IF NOT EXISTS messages (
      id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL,
      ts TEXT NOT NULL,
      from_session TEXT NOT NULL,
      to_session TEXT NOT NULL,
      type TEXT NOT NULL,
      subject TEXT,
      content TEXT NOT NULL,
      delivered_at TEXT
    );
    CREATE TABLE IF NOT EXISTS message_deliveries (
      message_id TEXT NOT NULL,
      session TEXT NOT NULL,
      delivered_at TEXT,
      gated_by TEXT,
      PRIMARY KEY (message_id, session)
    );
    CREATE TABLE IF NOT EXISTS haiku_gate_cache (
      session TEXT NOT NULL,
      topic_hash TEXT NOT NULL,
      decision TEXT NOT NULL,
      reason TEXT,
      cached_until TEXT NOT NULL,
      PRIMARY KEY (session, topic_hash)
    );
    CREATE TABLE IF NOT EXISTS help_broadcasts (
      id TEXT PRIMARY KEY,
      topic TEXT NOT NULL,
      sender TEXT NOT NULL,
      urgency TEXT NOT NULL DEFAULT 'normal',
      opened_at TEXT NOT NULL,
      resolved_at TEXT,
      resolver TEXT,
      resolution_summary TEXT,
      recipient_count INTEGER NOT NULL DEFAULT 0,
      token_spend_usd REAL DEFAULT 0,
      FOREIGN KEY (id) REFERENCES threads(id)
    );
    CREATE INDEX IF NOT EXISTS idx_msg_recipient ON messages(to_session, delivered_at);
    CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, ts);
    CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
    CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated);
    CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON message_deliveries(session, delivered_at);
    CREATE TABLE IF NOT EXISTS session_expertise_learned (
      session TEXT NOT NULL,
      topic_tag TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0.05,
      first_resolved_at TEXT,
      last_resolved_at TEXT,
      resolution_count INTEGER DEFAULT 1,
      PRIMARY KEY (session, topic_tag)
    );
    CREATE INDEX IF NOT EXISTS idx_help_unresolved ON help_broadcasts(resolved_at, opened_at);
    CREATE INDEX IF NOT EXISTS idx_learned_topic ON session_expertise_learned(topic_tag, confidence DESC);

    -- L2 sync queue (#126): rows here are pending writes to the L2's
    -- /api/v1/crosstalk/* endpoints. Populated when CROSSTALK_BACKEND=l2-http
    -- AND the L2 is unreachable (network error, 5xx). Drained by a
    -- background timer (~30s cadence) on next successful network event.
    --
    -- The local primary path (SQLite write + inbox-file delivery) has
    -- already happened by the time a row lands here; this is purely the
    -- audit/multi-tenant sync to the L2-as-source-of-truth.
    CREATE TABLE IF NOT EXISTS pending_l2_sync (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      op TEXT NOT NULL,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      last_attempt_at TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      CHECK (op IN ('send_message', 'reply', 'close_thread'))
    );
    CREATE INDEX IF NOT EXISTS idx_pending_l2_sync_attempts
      ON pending_l2_sync(attempts, created_at);
  `);

  // Idempotent migration: add columns to threads if they don't exist yet
  // (for existing DBs created before CCP Phase 1).
  const existingCols = db.pragma("table_info(threads)").map((r) => r.name);
  if (!existingCols.includes("kind")) {
    db.exec(`ALTER TABLE threads ADD COLUMN kind TEXT NOT NULL DEFAULT 'pair'`);
  }
  if (!existingCols.includes("policy")) {
    db.exec(`ALTER TABLE threads ADD COLUMN policy TEXT`);
  }
  if (!existingCols.includes("resolution")) {
    db.exec(`ALTER TABLE threads ADD COLUMN resolution TEXT`);
  }
  // #71: subagent tag scoping
  if (!existingCols.includes("from_tag")) {
    db.exec(`ALTER TABLE threads ADD COLUMN from_tag TEXT`);
  }
  if (!existingCols.includes("to_tag")) {
    db.exec(`ALTER TABLE threads ADD COLUMN to_tag TEXT`);
  }
  const existingMsgCols = db.pragma("table_info(messages)").map((r) => r.name);
  if (!existingMsgCols.includes("from_tag")) {
    db.exec(`ALTER TABLE messages ADD COLUMN from_tag TEXT`);
  }
  if (!existingMsgCols.includes("to_tag")) {
    db.exec(`ALTER TABLE messages ADD COLUMN to_tag TEXT`);
    db.exec(`CREATE INDEX IF NOT EXISTS idx_msg_to_tag ON messages(to_session, to_tag, delivered_at)`);
  }

  // Seed message_deliveries from existing messages that haven't been migrated yet.
  // This ensures pendingForSession works after the migration for pre-existing rows.
  db.exec(`
    INSERT OR IGNORE INTO message_deliveries (message_id, session, delivered_at, gated_by)
    SELECT id, to_session, delivered_at, NULL FROM messages
  `);

  return db;
}

function s(sql) {
  return getDb().prepare(sql);
}

export function upsertThread(t) {
  s(`INSERT INTO threads (id, participants, initiator, subject, created, updated, status, closed_by, close_reason, kind, policy, resolution, from_tag, to_tag)
     VALUES (@id, @participants, @initiator, @subject, @created, @updated, @status, @closed_by, @close_reason, @kind, @policy, @resolution, @from_tag, @to_tag)
     ON CONFLICT(id) DO UPDATE SET
       updated = excluded.updated,
       status = excluded.status,
       closed_by = COALESCE(excluded.closed_by, threads.closed_by),
       close_reason = COALESCE(excluded.close_reason, threads.close_reason),
       resolution = COALESCE(excluded.resolution, threads.resolution),
       to_tag = COALESCE(excluded.to_tag, threads.to_tag)`).run({
    id: t.id,
    participants: JSON.stringify(t.participants),
    initiator: t.initiator,
    subject: t.subject || "",
    created: t.created,
    updated: t.updated,
    status: t.status || "open",
    closed_by: t.closed_by || null,
    close_reason: t.close_reason || null,
    kind: t.kind || "pair",
    policy: t.policy ? JSON.stringify(t.policy) : null,
    resolution: t.resolution ? JSON.stringify(t.resolution) : null,
    from_tag: t.from_tag || null,
    to_tag: t.to_tag || null,
  });
}

/**
 * Insert a message into the messages table and seed message_deliveries for each recipient.
 *
 * For 2-party threads: recipients = [m.to]
 * For group threads:   recipients = all participants except sender (passed as m.recipients array)
 *
 * @param {object} m - message object
 * @param {boolean} delivered - mark as already delivered (for outbound self-messages)
 * @param {string[]} [extraRecipients] - additional recipient sessions beyond m.to (for group fanout)
 * @param {Record<string,string>} [gatedByMap] - per-recipient gated_by value ('L0'|'L1'|'L2'|'L3')
 */
export function insertMessage(m, delivered = false, extraRecipients = [], gatedByMap = {}) {
  s(`INSERT OR IGNORE INTO messages (id, thread_id, ts, from_session, to_session, type, subject, content, delivered_at, from_tag, to_tag)
     VALUES (@id, @thread_id, @ts, @from_session, @to_session, @type, @subject, @content, @delivered_at, @from_tag, @to_tag)`).run({
    id: m.id,
    thread_id: m.thread_id,
    ts: m.ts,
    from_session: m.from,
    to_session: m.to,
    type: m.type || "message",
    subject: m.subject || null,
    content: m.content,
    delivered_at: delivered ? new Date().toISOString() : null,
    from_tag: m.from_tag || null,
    to_tag: m.to_tag || null,
  });

  // Seed message_deliveries for all recipients.
  const now = delivered ? new Date().toISOString() : null;
  const allRecipients = new Set([m.to, ...extraRecipients]);
  const insertDelivery = s(`INSERT OR IGNORE INTO message_deliveries (message_id, session, delivered_at, gated_by)
                            VALUES (?, ?, ?, ?)`);
  for (const recipient of allRecipients) {
    const gatedBy = gatedByMap[recipient] ?? null;
    insertDelivery.run(m.id, recipient, now, gatedBy);
  }
}

/**
 * Returns pending (undelivered) messages for the given session, using message_deliveries.
 *
 * Tag filtering (#71):
 *   tag === undefined → no filter (legacy behaviour; also what callers outside
 *     crosstalk use, e.g. file-based ingest)
 *   tag === null      → only messages with to_tag IS NULL (main-session inbox)
 *   tag === "X"       → only messages with to_tag = "X" (subagent X's inbox)
 */
export function pendingForSession(toSession, limit = 50, tag = undefined) {
  if (tag === undefined) {
    return s(`
      SELECT m.id, m.thread_id, m.ts, m.from_session, m.to_session, m.type, m.subject, m.content, m.from_tag, m.to_tag
      FROM message_deliveries d
      JOIN messages m ON m.id = d.message_id
      WHERE d.session = ? AND d.delivered_at IS NULL
      ORDER BY m.ts ASC LIMIT ?
    `).all(toSession, limit);
  }
  if (tag === null) {
    return s(`
      SELECT m.id, m.thread_id, m.ts, m.from_session, m.to_session, m.type, m.subject, m.content, m.from_tag, m.to_tag
      FROM message_deliveries d
      JOIN messages m ON m.id = d.message_id
      WHERE d.session = ? AND d.delivered_at IS NULL AND m.to_tag IS NULL
      ORDER BY m.ts ASC LIMIT ?
    `).all(toSession, limit);
  }
  return s(`
    SELECT m.id, m.thread_id, m.ts, m.from_session, m.to_session, m.type, m.subject, m.content, m.from_tag, m.to_tag
    FROM message_deliveries d
    JOIN messages m ON m.id = d.message_id
    WHERE d.session = ? AND d.delivered_at IS NULL AND m.to_tag = ?
    ORDER BY m.ts ASC LIMIT ?
  `).all(toSession, tag, limit);
}

/**
 * Pending routings (#73) — un-delivered messages addressed to a subagent tag
 * on this session. These haven't been picked up by a subagent polling that tag,
 * so the main session may want to spawn one, adopt the thread, or reject.
 */
export function pendingRoutings(toSession, limit = 20) {
  return s(`
    SELECT m.id, m.thread_id, m.ts, m.from_session, m.from_tag, m.to_tag, m.type, m.subject, m.content
    FROM message_deliveries d
    JOIN messages m ON m.id = d.message_id
    WHERE d.session = ? AND d.delivered_at IS NULL AND m.to_tag IS NOT NULL
    ORDER BY m.ts ASC LIMIT ?
  `).all(toSession, limit);
}

/**
 * Mark a message as delivered for a specific session.
 * Falls back to legacy messages.delivered_at for 2-party backward compat.
 */
export function markDelivered(id, session) {
  const now = new Date().toISOString();
  s(`UPDATE message_deliveries SET delivered_at = ? WHERE message_id = ? AND session = ?`).run(now, id, session);
  // Also update legacy column so old code paths (file-based ingest) don't re-deliver
  s(`UPDATE messages SET delivered_at = ? WHERE id = ?`).run(now, id);
}

export function getThread(id) {
  const row = s(`SELECT * FROM threads WHERE id = ?`).get(id);
  if (!row) return null;
  return {
    ...row,
    participants: JSON.parse(row.participants),
    policy: row.policy ? JSON.parse(row.policy) : null,
    resolution: row.resolution ? JSON.parse(row.resolution) : null,
  };
}

export function getMessagesForThread(threadId) {
  return s(`SELECT id, thread_id, ts, from_session as "from", to_session as "to", type, subject, content, from_tag, to_tag
            FROM messages WHERE thread_id = ? ORDER BY ts ASC`).all(threadId);
}

/**
 * Look up the most recent message TO this session in a thread — used by
 * reply() to infer where replies should be routed (the reply's to_tag
 * mirrors the incoming message's from_tag so the originator's subagent
 * receives the response).
 */
export function lastIncomingTag(threadId, session) {
  const row = s(`SELECT from_tag FROM messages
                 WHERE thread_id = ? AND to_session = ?
                 ORDER BY ts DESC LIMIT 1`).get(threadId, session);
  return row ? row.from_tag : null;
}

export function listInboxMessages(toSession, limit = 20, tag = undefined) {
  if (tag === undefined) {
    return s(`SELECT id, thread_id, ts, from_session as "from", type, subject, content, from_tag, to_tag
              FROM messages WHERE to_session = ?
              ORDER BY ts DESC LIMIT ?`).all(toSession, limit);
  }
  if (tag === null) {
    return s(`SELECT id, thread_id, ts, from_session as "from", type, subject, content, from_tag, to_tag
              FROM messages WHERE to_session = ? AND to_tag IS NULL
              ORDER BY ts DESC LIMIT ?`).all(toSession, limit);
  }
  return s(`SELECT id, thread_id, ts, from_session as "from", type, subject, content, from_tag, to_tag
            FROM messages WHERE to_session = ? AND to_tag = ?
            ORDER BY ts DESC LIMIT ?`).all(toSession, tag, limit);
}

export function listThreadsForSession(session, tag = undefined) {
  // Tag filter: thread matches if session's own side on the thread has tag=X
  //  - If session IS initiator: compare thread.from_tag
  //  - If session IS a recipient: compare thread.to_tag
  // For MVP/correctness, we filter on either side matching (union of both).
  if (tag === undefined) {
    return s(`SELECT id, participants, initiator, subject, status, updated, kind, from_tag, to_tag
              FROM threads WHERE participants LIKE ?
              ORDER BY updated DESC`).all(`%"${session}"%`);
  }
  if (tag === null) {
    return s(`SELECT id, participants, initiator, subject, status, updated, kind, from_tag, to_tag
              FROM threads
              WHERE participants LIKE ?
                AND ((initiator = ? AND from_tag IS NULL)
                  OR (initiator != ? AND (to_tag IS NULL)))
              ORDER BY updated DESC`).all(`%"${session}"%`, session, session);
  }
  return s(`SELECT id, participants, initiator, subject, status, updated, kind, from_tag, to_tag
            FROM threads
            WHERE participants LIKE ?
              AND ((initiator = ? AND from_tag = ?)
                OR (initiator != ? AND to_tag = ?))
            ORDER BY updated DESC`).all(`%"${session}"%`, session, tag, session, tag);
}

/**
 * Adopt a thread's tag on this session's side (#72).
 * - If session initiated the thread, updates from_tag.
 * - If session is a recipient, updates to_tag.
 * Pass newTag=null to promote the thread to the main-session inbox.
 */
export function adoptThread(threadId, session, newTag = null) {
  const thread = getThread(threadId);
  if (!thread) return { ok: false, reason: "thread not found" };
  if (!thread.participants.includes(session)) {
    return { ok: false, reason: "session is not a participant in this thread" };
  }
  const isInitiator = thread.initiator === session;
  const col = isInitiator ? "from_tag" : "to_tag";
  const prev = isInitiator ? thread.from_tag : thread.to_tag;
  s(`UPDATE threads SET ${col} = ?, updated = ? WHERE id = ?`)
    .run(newTag, new Date().toISOString(), threadId);
  // Cascade to all messages where this session is the recipient side
  // (so inbox filters reflect the new ownership on future polls).
  if (!isInitiator) {
    s(`UPDATE messages SET to_tag = ? WHERE thread_id = ? AND to_session = ?`)
      .run(newTag, threadId, session);
  } else {
    s(`UPDATE messages SET from_tag = ? WHERE thread_id = ? AND from_session = ?`)
      .run(newTag, threadId, session);
  }
  return { ok: true, prev, next: newTag, side: isInitiator ? "initiator" : "recipient" };
}

/**
 * Get a cached Haiku gate decision for (session, topicHash).
 * Returns null if not cached or expired.
 */
export function getHaikuCache(session, topicHash) {
  const now = new Date().toISOString();
  const row = s(`SELECT decision, reason FROM haiku_gate_cache
                 WHERE session = ? AND topic_hash = ? AND cached_until > ?`)
    .get(session, topicHash, now);
  return row || null;
}

/**
 * Store a Haiku gate decision for (session, topicHash), valid for 1 hour.
 */
export function setHaikuCache(session, topicHash, decision, reason) {
  const cachedUntil = new Date(Date.now() + 60 * 60 * 1000).toISOString();
  s(`INSERT INTO haiku_gate_cache (session, topic_hash, decision, reason, cached_until)
     VALUES (?, ?, ?, ?, ?)
     ON CONFLICT(session, topic_hash) DO UPDATE SET
       decision = excluded.decision,
       reason = excluded.reason,
       cached_until = excluded.cached_until`)
    .run(session, topicHash, decision, reason || null, cachedUntil);
}

export function insertHelpBroadcast(h) {
  s(`INSERT INTO help_broadcasts (id, topic, sender, urgency, opened_at, recipient_count, token_spend_usd)
     VALUES (@id, @topic, @sender, @urgency, @opened_at, @recipient_count, 0)`).run({
    id: h.id,
    topic: h.topic,
    sender: h.sender,
    urgency: h.urgency || "normal",
    opened_at: h.opened_at,
    recipient_count: h.recipient_count || 0,
  });
}

export function resolveHelpBroadcast(id, { resolver, summary, resolvedAt }) {
  s(`UPDATE help_broadcasts SET resolved_at = ?, resolver = ?, resolution_summary = ? WHERE id = ?`)
    .run(resolvedAt || new Date().toISOString(), resolver || null, summary || null, id);
}

export function getHelpBroadcast(id) {
  return s(`SELECT * FROM help_broadcasts WHERE id = ?`).get(id);
}

/**
 * Increment learned confidence for (session, topicTag) when a help thread resolves.
 * Confidence increments by 0.05 per resolve, capped at 1.0.
 */
export function upsertLearnedExpertise(session, topicTag) {
  const now = new Date().toISOString();
  s(`INSERT INTO session_expertise_learned
       (session, topic_tag, confidence, first_resolved_at, last_resolved_at, resolution_count)
     VALUES (?, ?, 0.05, ?, ?, 1)
     ON CONFLICT(session, topic_tag) DO UPDATE SET
       confidence = MIN(1.0, confidence + 0.05),
       last_resolved_at = excluded.last_resolved_at,
       resolution_count = resolution_count + 1`)
    .run(session, topicTag, now, now);
}

/**
 * Returns sessions with learned confidence > 0 for a given topic_tag, ordered by confidence desc.
 * Returns [{session, confidence, resolution_count}]
 */
export function getLearnedSessions(topicTag, limit = 10) {
  return s(`SELECT session, confidence, resolution_count, last_resolved_at
            FROM session_expertise_learned
            WHERE topic_tag = ?
            ORDER BY confidence DESC, last_resolved_at DESC
            LIMIT ?`).all(topicTag, limit);
}

/**
 * Returns unresolved help broadcasts older than maxAgeMs.
 */
export function staleHelpBroadcasts(maxAgeMs) {
  const cutoff = new Date(Date.now() - maxAgeMs).toISOString();
  return s(`SELECT id, topic, sender, urgency, opened_at, recipient_count
            FROM help_broadcasts
            WHERE resolved_at IS NULL AND opened_at < ?
            ORDER BY opened_at ASC`).all(cutoff);
}

// ---------------------------------------------------------------------
// L2 sync queue helpers (#126)
//
// Used by ../l2-sync.js to enqueue/drain pending L2 writes when the
// L2 is unreachable. The primary local-write path runs first (SQLite
// + inbox-file delivery); the L2 sync attempt happens after, and on
// failure the args land here to be retried later.
// ---------------------------------------------------------------------

/**
 * Enqueue a pending L2 sync operation.
 * @param {string} op - 'send_message' | 'reply' | 'close_thread'
 * @param {object} payload - args to pass to the L2 endpoint (JSON-serialized)
 * @param {string} [error] - optional error message from the failed first attempt
 * @returns {number} the inserted row id
 */
export function enqueuePendingL2Sync(op, payload, error = null) {
  const now = new Date().toISOString();
  const result = s(`
    INSERT INTO pending_l2_sync (op, payload, created_at, last_attempt_at, attempts, last_error)
    VALUES (?, ?, ?, ?, ?, ?)
  `).run(op, JSON.stringify(payload), now, error ? now : null, error ? 1 : 0, error || null);
  return result.lastInsertRowid;
}

/**
 * Fetch up to `limit` pending sync rows, oldest first, low-attempt-count first.
 * Used by the background drain to retry queued writes.
 */
export function listPendingL2Sync(limit = 50) {
  return s(`
    SELECT id, op, payload, created_at, last_attempt_at, attempts, last_error
    FROM pending_l2_sync
    ORDER BY attempts ASC, created_at ASC
    LIMIT ?
  `).all(limit);
}

/**
 * Mark a sync attempt as successful — removes the queued row.
 */
export function deletePendingL2Sync(id) {
  s(`DELETE FROM pending_l2_sync WHERE id = ?`).run(id);
}

/**
 * Mark a sync attempt as failed — increments attempts + records error.
 */
export function bumpPendingL2SyncAttempt(id, error) {
  const now = new Date().toISOString();
  s(`
    UPDATE pending_l2_sync
    SET attempts = attempts + 1,
        last_attempt_at = ?,
        last_error = ?
    WHERE id = ?
  `).run(now, error || null, id);
}

/**
 * Returns count of pending rows. Used for observability + the operator
 * "you have N unsynced messages" warning if the queue grows beyond a
 * threshold (handled by drain loop in ../l2-sync.js).
 */
export function countPendingL2Sync() {
  const row = s(`SELECT COUNT(*) AS n FROM pending_l2_sync`).get();
  return row.n;
}
