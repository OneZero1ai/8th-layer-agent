// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { CrosstalkPage } from "./CrosstalkPage"

const originalFetch = globalThis.fetch

type MockResponse = {
  ok: boolean
  status: number
  body: unknown
}

// Tiny URL-routing harness — the page issues:
//   GET /api/v1/crosstalk/threads
//   GET /api/v1/consults/inbox
//   GET /api/v1/activity?limit=500
// Plus per-thread fetches under /api/v1/crosstalk/threads/<id>.
function routeResponses(routes: Array<[RegExp, MockResponse]>) {
  globalThis.fetch = vi.fn().mockImplementation((url: string) => {
    for (const [matcher, resp] of routes) {
      if (matcher.test(url)) {
        return Promise.resolve({
          ok: resp.ok,
          status: resp.status,
          json: () => Promise.resolve(resp.body),
        })
      }
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ items: [], count: 0, next_cursor: null }),
    })
  }) as unknown as typeof fetch
}

const sampleThread = {
  id: "thread_abc123def456",
  subject: "Refactor AIGRP runtime",
  status: "open",
  created_at: "2026-05-08T12:00:00+00:00",
  created_by_username: "alice",
  participants: ["alice", "bob"],
}

const sampleConsult = {
  thread_id: "consult_xyz789",
  from_l2_id: "peer-co/engineering",
  from_persona: "carol",
  to_l2_id: "8th-layer-corp/engineering",
  to_persona: "alice",
  subject: "How do you handle key rotation?",
  status: "received",
  claimed_by: null,
  created_at: "2026-05-08T10:00:00+00:00",
  closed_at: null,
  resolution_summary: null,
}

const consultOpenActivity = {
  id: "act_001",
  ts: "2026-05-08T11:00:00+00:00",
  tenant_enterprise: "8th-layer-corp",
  tenant_group: "engineering",
  persona: "alice",
  human: null,
  event_type: "consult_open",
  payload: {
    thread_id: "outbound_thread_1",
    to_l2_id: "peer-co/engineering",
    to_persona: "dave",
    subject: "Bedrock model availability",
  },
  result_summary: null,
  thread_or_chain_id: "outbound_thread_1",
}

describe("CrosstalkPage", () => {
  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it("renders empty states when nothing is loaded", async () => {
    routeResponses([
      [
        /\/crosstalk\/threads/,
        { ok: true, status: 200, body: { items: [], count: 0 } },
      ],
      [
        /\/consults\/inbox/,
        {
          ok: true,
          status: 200,
          body: {
            self_l2_id: "8th-layer-corp/engineering",
            self_persona: "alice",
            threads: [],
          },
        },
      ],
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: { items: [], count: 0, next_cursor: null },
        },
      ],
    ])
    render(<CrosstalkPage />)
    expect(await screen.findByText(/no threads yet/i)).toBeInTheDocument()
  })

  it("lists in-L2 threads with subject + participant count", async () => {
    routeResponses([
      [
        /\/crosstalk\/threads/,
        {
          ok: true,
          status: 200,
          body: { items: [sampleThread], count: 1 },
        },
      ],
      [
        /\/consults\/inbox/,
        {
          ok: true,
          status: 200,
          body: {
            self_l2_id: "8th-layer-corp/engineering",
            self_persona: "alice",
            threads: [],
          },
        },
      ],
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: { items: [], count: 0, next_cursor: null },
        },
      ],
    ])
    render(<CrosstalkPage />)
    await waitFor(() => {
      expect(screen.getByText("Refactor AIGRP runtime")).toBeInTheDocument()
    })
    // Truncated thread id in the cell.
    expect(screen.getByText(/thread_a/i)).toBeInTheDocument()
  })

  it("filters threads by search query", async () => {
    routeResponses([
      [
        /\/crosstalk\/threads/,
        {
          ok: true,
          status: 200,
          body: {
            items: [
              sampleThread,
              { ...sampleThread, id: "thread_other", subject: "Other topic" },
            ],
            count: 2,
          },
        },
      ],
      [
        /\/consults\/inbox/,
        {
          ok: true,
          status: 200,
          body: {
            self_l2_id: "8th-layer-corp/engineering",
            self_persona: "alice",
            threads: [],
          },
        },
      ],
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: { items: [], count: 0, next_cursor: null },
        },
      ],
    ])
    render(<CrosstalkPage />)
    await screen.findByText("Refactor AIGRP runtime")
    fireEvent.change(screen.getByLabelText(/search crosstalk threads/i), {
      target: { value: "AIGRP" },
    })
    expect(screen.getByText("Refactor AIGRP runtime")).toBeInTheDocument()
    expect(screen.queryByText("Other topic")).not.toBeInTheDocument()
  })

  it("renders consult inbox sub-tab with peer Enterprise rows", async () => {
    routeResponses([
      [
        /\/crosstalk\/threads/,
        { ok: true, status: 200, body: { items: [], count: 0 } },
      ],
      [
        /\/consults\/inbox/,
        {
          ok: true,
          status: 200,
          body: {
            self_l2_id: "8th-layer-corp/engineering",
            self_persona: "alice",
            threads: [sampleConsult],
          },
        },
      ],
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: { items: [], count: 0, next_cursor: null },
        },
      ],
    ])
    render(<CrosstalkPage />)
    // Switch to Inbox sub-tab.
    fireEvent.click(
      await screen.findByRole("button", { name: /consult inbox/i }),
    )
    await waitFor(() => {
      expect(
        screen.getByText("How do you handle key rotation?"),
      ).toBeInTheDocument()
    })
    expect(screen.getByText("peer-co/engineering")).toBeInTheDocument()
  })

  it("derives the consult outbox from activity log rows", async () => {
    routeResponses([
      [
        /\/crosstalk\/threads/,
        { ok: true, status: 200, body: { items: [], count: 0 } },
      ],
      [
        /\/consults\/inbox/,
        {
          ok: true,
          status: 200,
          body: {
            self_l2_id: "8th-layer-corp/engineering",
            self_persona: "alice",
            threads: [],
          },
        },
      ],
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: {
            items: [consultOpenActivity],
            count: 1,
            next_cursor: null,
          },
        },
      ],
    ])
    render(<CrosstalkPage />)
    fireEvent.click(
      await screen.findByRole("button", { name: /consult outbox/i }),
    )
    await waitFor(() => {
      expect(screen.getByText("Bedrock model availability")).toBeInTheDocument()
    })
    expect(screen.getByText(/peer-co\/engineering/)).toBeInTheDocument()
  })
})
