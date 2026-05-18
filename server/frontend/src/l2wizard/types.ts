/**
 * FO-3 Phase 3 — Create-L2 wizard type contract (agent#193 / Decision 32).
 *
 * Mirrors the cq-server L2-provision proxy (PR #292 on OneZero1ai/8th-layer-agent):
 *
 *   POST /api/v1/admin/l2s
 *     request  — {l2_slug, description, aws_region}
 *     response — {job_id, l2_id, status, poll_url, stream_url}  (HTTP 202)
 *
 *   GET /api/v1/admin/l2s/jobs/{job_id}/stream  (text/event-stream)
 *     Each SSE frame is a named event whose `data:` line carries a JSON
 *     job-state object. Event names emitted by the proxy:
 *       open       — {job_id, status: "STREAM_OPEN"}
 *       phase      — {job_id, status, phase, phase_label, progress_pct}
 *       heartbeat  — {job_id, note?} / {job_id, ts}
 *       completed  — {job_id, status: "COMPLETED", result}
 *       failed     — {job_id, status, error}
 *
 * The wizard never sends `enterprise_id` or AWS credentials — the proxy
 * resolves the caller's Enterprise server-side from their session.
 */

/** POST /api/v1/admin/l2s request body. */
export interface CreateL2Request {
  l2_slug: string
  description: string
  aws_region: string
}

/** 202 response from POST /api/v1/admin/l2s. */
export interface CreateL2Response {
  job_id: string
  l2_id: string
  status: string
  /** Enterprise-scoped plain-poll URL (the wizard uses `stream_url` instead). */
  poll_url: string
  /** SSE endpoint the wizard opens with a browser `EventSource`. */
  stream_url: string
}

/**
 * The job `result` carried by the terminal `completed` SSE event.
 *
 * Decision 32 #2: the new L2's `cqa.v1.*` admin key is shown once in the
 * completion panel (and emailed as a backup). `admin_url` is the new L2's
 * admin-shell URL for the "Open L2 Admin" link.
 */
export interface L2ProvisionResult {
  l2_id?: string
  l2_slug?: string
  /** One-time admin API key — held in React state only, never persisted. */
  admin_api_key?: string
  /** Admin-shell URL of the freshly provisioned L2. */
  admin_url?: string
  dns_name?: string
}

/**
 * Decoded job-state payload from any SSE frame. Every field is optional —
 * which fields are present depends on the event name (see module docstring).
 */
export interface L2JobState {
  job_id: string
  /** STREAM_OPEN | PROVISIONING | COMPLETED | FAILED | STREAM_TIMEOUT. */
  status?: string
  /** Numeric phase index (1-based) of the ~8-phase standup. */
  phase?: number | null
  /** Human-readable phase label, e.g. "ACM certificate". */
  phase_label?: string | null
  /** Server-reported completion percentage, 0–100. */
  progress_pct?: number | null
  /** Present on the terminal `completed` event. */
  result?: L2ProvisionResult | null
  /** Present on the terminal `failed` event. */
  error?: string | null
  /** Soft-warning note on a `heartbeat` event after a transient poll error. */
  note?: string | null
}

/** SSE event names the proxy emits (see PR #292 `_sse_event` calls). */
export type L2SseEventName =
  | "open"
  | "phase"
  | "heartbeat"
  | "completed"
  | "failed"

/** Terminal lifecycle states for the wizard's progress step. */
export type L2ProvisioningPhase =
  | "connecting"
  | "streaming"
  | "completed"
  | "failed"

/**
 * The ~8 standup phases (Decision 32 §Shape). Used to render the progress
 * bar's phase ticks when the proxy has not yet reported a `phase_label`.
 * The proxy's `phase` index is authoritative when present; this list is the
 * static fallback labelling.
 */
export const L2_STANDUP_PHASES: readonly string[] = [
  "ACM certificate",
  "ECS cluster",
  "Application Load Balancer",
  "EFS file system",
  "CloudFormation stack",
  "SSM parameter seed",
  "Directory record",
  "Persona allowlist",
] as const
