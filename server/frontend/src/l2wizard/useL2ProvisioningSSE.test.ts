/**
 * FO-3 Phase 3 — tests for `useL2ProvisioningSSE` (agent#193).
 *
 * happy-dom ships no `EventSource`, so we install a controllable mock
 * global: tests drive named SSE events into the hook and assert on the
 * resulting job state, lifecycle phase, error, and connection close.
 */

import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { useL2ProvisioningSSE } from "./useL2ProvisioningSSE"

// ---------------------------------------------------------------------------
// Mock EventSource
// ---------------------------------------------------------------------------

type Listener = (e: { data: string }) => void

class MockEventSource {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  static instances: MockEventSource[] = []

  url: string
  withCredentials: boolean
  readyState = MockEventSource.OPEN
  onmessage: Listener | null = null
  onerror: (() => void) | null = null
  private listeners: Record<string, Listener[]> = {}

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url
    this.withCredentials = init?.withCredentials ?? false
    MockEventSource.instances.push(this)
  }

  addEventListener(name: string, fn: Listener) {
    const bucket = this.listeners[name] ?? []
    bucket.push(fn)
    this.listeners[name] = bucket
  }

  close() {
    this.readyState = MockEventSource.CLOSED
  }

  /** Test helper — dispatch a named SSE event with a JSON payload. */
  emit(name: string, data: unknown) {
    const payload = { data: JSON.stringify(data) }
    for (const fn of this.listeners[name] ?? []) fn(payload)
  }

  /** Test helper — fire a terminal connection error. */
  fail() {
    this.readyState = MockEventSource.CLOSED
    this.onerror?.()
  }
}

const STREAM_URL = "/api/v1/admin/l2s/jobs/job-1/stream"

beforeEach(() => {
  MockEventSource.instances = []
  ;(globalThis as unknown as { EventSource: unknown }).EventSource =
    MockEventSource
})

afterEach(() => {
  ;(globalThis as unknown as { EventSource?: unknown }).EventSource = undefined
})

function lastSource(): MockEventSource {
  const s = MockEventSource.instances.at(-1)
  if (!s) throw new Error("no EventSource opened")
  return s
}

describe("useL2ProvisioningSSE", () => {
  it("stays idle with no streamUrl and opens no connection", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(null))
    expect(MockEventSource.instances).toHaveLength(0)
    expect(result.current.phase).toBe("connecting")
    expect(result.current.jobState).toBeNull()
  })

  it("opens a credentialed EventSource for a streamUrl", () => {
    renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    expect(MockEventSource.instances).toHaveLength(1)
    expect(lastSource().url).toBe(STREAM_URL)
    expect(lastSource().withCredentials).toBe(true)
  })

  it("advances to streaming on a phase event and records job state", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    act(() => {
      lastSource().emit("phase", {
        job_id: "job-1",
        status: "PROVISIONING",
        phase: 2,
        phase_label: "ECS cluster",
        progress_pct: 40,
      })
    })
    expect(result.current.phase).toBe("streaming")
    expect(result.current.jobState?.phase).toBe(2)
    expect(result.current.jobState?.phase_label).toBe("ECS cluster")
  })

  it("captures the result and closes on a completed event", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    const source = lastSource()
    act(() => {
      source.emit("completed", {
        job_id: "job-1",
        status: "COMPLETED",
        result: { admin_api_key: "cqa.v1.secret", admin_url: "https://x" },
      })
    })
    expect(result.current.phase).toBe("completed")
    expect(result.current.jobState?.result?.admin_api_key).toBe("cqa.v1.secret")
    expect(source.readyState).toBe(MockEventSource.CLOSED)
  })

  it("surfaces the error and closes on a failed event", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    const source = lastSource()
    act(() => {
      source.emit("failed", {
        job_id: "job-1",
        status: "FAILED",
        error: "phase 5: CFN rollback",
      })
    })
    expect(result.current.phase).toBe("failed")
    expect(result.current.error).toBe("phase 5: CFN rollback")
    expect(source.readyState).toBe(MockEventSource.CLOSED)
  })

  it("treats a heartbeat as keep-alive without regressing job state", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    act(() => {
      lastSource().emit("phase", { job_id: "job-1", phase: 3 })
    })
    act(() => {
      lastSource().emit("heartbeat", { job_id: "job-1", note: "poll retry" })
    })
    expect(result.current.phase).toBe("streaming")
    expect(result.current.jobState?.phase).toBe(3)
  })

  it("ignores a malformed (non-JSON) frame", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    act(() => {
      // Directly invoke a listener with junk — emit() always JSON-stringifies.
      const source = lastSource()
      source.onmessage?.({ data: "not json {{{" })
    })
    expect(result.current.jobState).toBeNull()
  })

  it("marks a permanently-closed connection as failed", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    act(() => {
      lastSource().fail()
    })
    expect(result.current.phase).toBe("failed")
    expect(result.current.error).toMatch(/interrupted/i)
  })

  it("does not overwrite a completed stream when the socket later closes", () => {
    const { result } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    const source = lastSource()
    act(() => {
      source.emit("completed", {
        job_id: "job-1",
        status: "COMPLETED",
        result: {},
      })
    })
    act(() => {
      source.onerror?.()
    })
    expect(result.current.phase).toBe("completed")
  })

  it("closes the connection on unmount", () => {
    const { unmount } = renderHook(() => useL2ProvisioningSSE(STREAM_URL))
    const source = lastSource()
    expect(source.readyState).toBe(MockEventSource.OPEN)
    unmount()
    expect(source.readyState).toBe(MockEventSource.CLOSED)
  })
})
