// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PersonasPage } from "./PersonasPage"

const originalFetch = globalThis.fetch

type MockResponse = {
  ok: boolean
  status: number
  body: unknown
}

// Tiny URL-routing harness — the page issues:
//   GET /api/v1/activity?limit=500       (directory derivation)
//   GET /api/v1/review/units             (KU joins, called twice)
// The drawer also issues GET /api/v1/activity?persona=<name>&...
// Match on URL substrings so call-order changes don't churn the test.
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

const aliceRow = {
  id: "act_001",
  ts: "2026-05-08T12:00:00+00:00",
  tenant_enterprise: "ent_8thlayer",
  tenant_group: "engineering",
  persona: "alice",
  human: null,
  event_type: "propose",
  payload: { unit_id: "ku_001" },
  result_summary: null,
  thread_or_chain_id: null,
}

const bobRow = {
  ...aliceRow,
  id: "act_002",
  ts: "2026-04-01T12:00:00+00:00",
  persona: "bob",
  tenant_group: "ops",
}

const aliceKu = {
  knowledge_unit: {
    id: "ku_001",
    version: 1,
    domains: ["lambda", "iam"],
    insight: {
      summary: "Lambda timeouts surface as 504",
      detail: "...",
      action: "...",
    },
    context: { languages: [], frameworks: [], pattern: "" },
    evidence: {
      confidence: 0.7,
      confirmations: 3,
      first_observed: "2026-05-01T00:00:00+00:00",
      last_confirmed: "2026-05-07T00:00:00+00:00",
    },
    tier: "private",
    created_by: "alice",
    superseded_by: null,
    flags: [],
  },
  status: "approved",
  reviewed_by: "admin",
  reviewed_at: "2026-05-02T00:00:00+00:00",
}

describe("PersonasPage", () => {
  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it("renders the empty state when no activity rows exist", async () => {
    routeResponses([
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: { items: [], count: 0, next_cursor: null },
        },
      ],
      [/\/review\/units/, { ok: true, status: 200, body: [] }],
    ])
    render(<PersonasPage />)
    expect(await screen.findByText(/no personas yet/i)).toBeInTheDocument()
  })

  it("derives a directory row per persona from activity", async () => {
    routeResponses([
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: {
            items: [aliceRow, bobRow],
            count: 2,
            next_cursor: null,
          },
        },
      ],
      [/\/review\/units/, { ok: true, status: 200, body: [aliceKu] }],
    ])

    render(<PersonasPage />)

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument()
      expect(screen.getByText("bob")).toBeInTheDocument()
    })
    // KU count column — alice has one, bob has zero.
    const aliceRowEl = screen.getByText("alice").closest("tr")
    const bobRowEl = screen.getByText("bob").closest("tr")
    expect(aliceRowEl?.textContent).toContain("1")
    expect(bobRowEl?.textContent).toContain("0")
  })

  it("filters by search query against persona name", async () => {
    routeResponses([
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: {
            items: [aliceRow, bobRow],
            count: 2,
            next_cursor: null,
          },
        },
      ],
      [/\/review\/units/, { ok: true, status: 200, body: [] }],
    ])

    render(<PersonasPage />)
    await screen.findByText("alice")
    fireEvent.change(screen.getByLabelText(/search personas/i), {
      target: { value: "ali" },
    })
    expect(screen.getByText("alice")).toBeInTheDocument()
    expect(screen.queryByText("bob")).not.toBeInTheDocument()
  })

  it("opens the detail drawer when a row is clicked", async () => {
    routeResponses([
      [
        /\/activity/,
        {
          ok: true,
          status: 200,
          body: {
            items: [aliceRow],
            count: 1,
            next_cursor: null,
          },
        },
      ],
      [/\/review\/units/, { ok: true, status: 200, body: [aliceKu] }],
    ])

    render(<PersonasPage />)
    const aliceCell = await screen.findByText("alice")
    fireEvent.click(aliceCell)
    // Drawer heading uses the persona name. Look for the AAISN-scoped
    // path which only renders inside the drawer.
    await waitFor(() => {
      expect(
        screen.getByText(/ent_8thlayer.*engineering.*alice/i),
      ).toBeInTheDocument()
    })
    expect(screen.getByText(/KU contributions/i)).toBeInTheDocument()
  })
})
