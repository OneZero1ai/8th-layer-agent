/**
 * FO-3 Phase 3 — `useL2ProvisioningSSE` (agent#193 / Decision 32).
 *
 * A React hook wrapping a browser `EventSource` on the cq-server proxy's
 * L2-create progress stream (`GET /api/v1/admin/l2s/jobs/{job_id}/stream`,
 * PR #292). The proxy emits named SSE events — `open`, `phase`, `heartbeat`,
 * `completed`, `failed` — each carrying a JSON job-state payload.
 *
 * The hook:
 *   - opens the stream when given a non-null `streamUrl`;
 *   - parses every named event into an `L2JobState`;
 *   - surfaces the latest job state, a `phase` lifecycle flag, and an error;
 *   - closes the `EventSource` on a terminal event (`completed` / `failed`)
 *     and on unmount — no dangling connections.
 *
 * The terminal `completed` event's `result.admin_api_key` is the one-time
 * key the reveal panel renders. The hook keeps it in React state only; it is
 * never written to localStorage or the URL.
 *
 * `EventSource` sends the `cq_session` HttpOnly cookie automatically for a
 * same-origin URL (the admin shell and the proxy share an origin), so no
 * credential plumbing is needed here.
 */

import { useEffect, useRef, useState } from "react"
import type { L2JobState, L2ProvisioningPhase } from "./types"

/** Named SSE events the proxy emits (PR #292 `_sse_event` calls). */
const NAMED_EVENTS = ["open", "phase", "heartbeat", "completed", "failed"]

export interface L2ProvisioningStream {
  /** Lifecycle flag for the progress UI. */
  phase: L2ProvisioningPhase
  /** Most recent decoded job state, or null before the first event. */
  jobState: L2JobState | null
  /** Error string once the stream has terminally failed, else null. */
  error: string | null
}

/**
 * Parse one SSE `data:` payload into an `L2JobState`. A malformed frame
 * (non-JSON, or JSON that is not an object) yields null — the caller skips
 * it rather than crashing the stream.
 */
function parseJobState(raw: string): L2JobState | null {
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === "object") {
      return parsed as L2JobState
    }
    return null
  } catch {
    return null
  }
}

/**
 * Subscribe to an L2-create job's SSE progress stream.
 *
 * @param streamUrl  the proxy's `stream_url` from the 202 create response,
 *                   or null to keep the hook idle (no connection opened).
 */
export function useL2ProvisioningSSE(
  streamUrl: string | null,
): L2ProvisioningStream {
  const [phase, setPhase] = useState<L2ProvisioningPhase>("connecting")
  const [jobState, setJobState] = useState<L2JobState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!streamUrl) {
      return
    }

    // Reset state for a fresh stream (e.g. a retried provision).
    setPhase("connecting")
    setJobState(null)
    setError(null)

    const source = new EventSource(streamUrl, { withCredentials: true })
    sourceRef.current = source

    function close() {
      source.close()
      if (sourceRef.current === source) {
        sourceRef.current = null
      }
    }

    function handle(eventName: string, data: string) {
      const state = parseJobState(data)
      if (!state) return

      if (eventName === "completed") {
        setJobState(state)
        setPhase("completed")
        close()
        return
      }
      if (eventName === "failed") {
        setJobState(state)
        setError(state.error || "Provisioning failed.")
        setPhase("failed")
        close()
        return
      }
      // open / phase / heartbeat — keep the connection live. The `open`
      // event flips the lifecycle out of `connecting`; `heartbeat` carries
      // no phase data so it does not regress the displayed job state.
      if (eventName === "heartbeat") {
        setPhase((p) => (p === "connecting" ? "streaming" : p))
        return
      }
      setJobState(state)
      setPhase("streaming")
    }

    // The proxy always names its events, so `onmessage` (the unnamed-event
    // handler) is not relied on — but wire it defensively in case an
    // intermediary strips the `event:` line.
    source.onmessage = (e) => handle("phase", e.data)

    for (const name of NAMED_EVENTS) {
      source.addEventListener(name, (e) => {
        handle(name, (e as MessageEvent).data)
      })
    }

    source.onerror = () => {
      // EventSource auto-reconnects on a transient drop; only treat the
      // error as terminal once the connection is permanently CLOSED. A
      // terminal event already closed the stream in that case — guard so a
      // post-completion CLOSED state does not overwrite a success.
      if (source.readyState === EventSource.CLOSED) {
        setPhase((p) => {
          if (p === "completed" || p === "failed") return p
          setError(
            "The progress stream was interrupted. Your L2 may still be " +
              "provisioning — check your email for the admin key.",
          )
          return "failed"
        })
      }
    }

    return () => {
      // Unmount / streamUrl change — always release the connection.
      close()
    }
  }, [streamUrl])

  return { phase, jobState, error }
}
