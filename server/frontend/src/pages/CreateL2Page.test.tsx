/**
 * FO-3 Phase 3 — tests for the Create-L2 wizard page (agent#193).
 *
 * Covers the 5-step state machine, slug validation + the debounced
 * availability probe, the DNS preview, and the provision → progress
 * transition. `useTheme` and `EventSource` are mocked; `fetch` is stubbed
 * per the ApiKeysPage test convention.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

// --- mock the theme hook so the page renders without a ThemeProvider ------
vi.mock("../theme", () => ({
  useTheme: () => ({
    theme: {
      platform: { name: "8th-Layer.ai", version: "1.0.0", tokens: {} },
      enterprise: {
        id: "acme",
        display_name: "Acme",
        logo_url: null,
        accent_hex: null,
        dark_mode_only: true,
      },
      l2: {
        id: "acme/eng",
        label: "eng",
        subaccent_hex: null,
        hero_motif: null,
      },
    },
    loading: false,
    error: null,
  }),
}))

import { CreateL2Page, L2_SLUG_PATTERN } from "./CreateL2Page"

// --- mock EventSource (happy-dom ships none) ------------------------------
class MockEventSource {
  static CLOSED = 2
  static instances: MockEventSource[] = []
  readyState = 1
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  url: string
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  addEventListener() {}
  close() {
    this.readyState = MockEventSource.CLOSED
  }
}

const originalFetch = globalThis.fetch

type MockResponse = { ok: boolean; status: number; body: unknown }

/**
 * Route the fetch mock by request — the wizard fires two distinct calls (a
 * debounced GET slug probe and the POST create) that must not collide in a
 * sequential queue. `slugProbe` answers the GET; `create` answers the POST.
 */
function mockApi(opts: { slugProbe?: MockResponse; create?: MockResponse }) {
  const slugProbe: MockResponse = opts.slugProbe ?? {
    ok: false,
    status: 404,
    body: {},
  }
  const create: MockResponse = opts.create ?? {
    ok: true,
    status: 202,
    body: {
      job_id: "job-1",
      l2_id: "acme/marketing",
      status: "PROVISIONING",
      poll_url: "/p",
      stream_url: "/api/v1/admin/l2s/jobs/job-1/stream",
    },
  }
  globalThis.fetch = vi
    .fn()
    .mockImplementation((_url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase()
      const resp = method === "POST" ? create : slugProbe
      return Promise.resolve({
        ok: resp.ok,
        status: resp.status,
        json: () => Promise.resolve(resp.body),
      })
    }) as unknown as typeof fetch
}

beforeEach(() => {
  MockEventSource.instances = []
  ;(globalThis as unknown as { EventSource: unknown }).EventSource =
    MockEventSource
  // Default: slug-availability probe 404s (route not deployed) → "unknown".
  mockApi({})
})

afterEach(() => {
  globalThis.fetch = originalFetch
  ;(globalThis as unknown as { EventSource?: unknown }).EventSource = undefined
  vi.restoreAllMocks()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <CreateL2Page />
    </MemoryRouter>,
  )
}

/** Walk all 5 config steps with valid input, leaving the wizard on Review. */
async function advanceToReview() {
  fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
    target: { value: "marketing" },
  })
  fireEvent.click(await screen.findByRole("button", { name: /continue/i }))

  fireEvent.change(screen.getByPlaceholderText(/knowledge domain/i), {
    target: { value: "Campaign agents for the marketing team." },
  })
  fireEvent.click(screen.getByRole("button", { name: /continue/i }))

  // Region step — default us-east-1 is valid.
  fireEvent.click(screen.getByRole("button", { name: /continue/i }))

  // DNS step — confirm the checkbox.
  fireEvent.click(screen.getByRole("checkbox"))
  fireEvent.click(screen.getByRole("button", { name: /continue/i }))
}

describe("L2_SLUG_PATTERN", () => {
  it("accepts valid slugs and rejects invalid ones", () => {
    expect(L2_SLUG_PATTERN.test("marketing")).toBe(true)
    expect(L2_SLUG_PATTERN.test("a1b-c")).toBe(true)
    expect(L2_SLUG_PATTERN.test("Ab")).toBe(false) // uppercase
    expect(L2_SLUG_PATTERN.test("1abc")).toBe(false) // leading digit
    expect(L2_SLUG_PATTERN.test("ab")).toBe(false) // too short
    expect(L2_SLUG_PATTERN.test(`a${"x".repeat(31)}`)).toBe(false) // too long
  })
})

describe("CreateL2Page", () => {
  it("starts on the name step", () => {
    renderPage()
    expect(screen.getByText(/name your l2/i)).toBeInTheDocument()
  })

  it("blocks Continue until the slug matches the pattern", () => {
    renderPage()
    const continueBtn = screen.getByRole("button", { name: /continue/i })
    expect(continueBtn).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "Ab" }, // invalid
    })
    expect(continueBtn).toBeDisabled()
    expect(screen.getByText(/must match/i)).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    expect(continueBtn).not.toBeDisabled()
  })

  it("runs the debounced availability probe and shows a taken slug", async () => {
    mockApi({ slugProbe: { ok: false, status: 409, body: {} } })
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "taken-slug" },
    })
    expect(await screen.findByText(/already in use/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled()
  })

  it("allows Continue on an unknown probe result (route not deployed)", async () => {
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    expect(
      await screen.findByText(/confirmed when you provision/i),
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /continue/i })).not.toBeDisabled()
  })

  it("enforces the 5–500 char description bound on step 2", async () => {
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    fireEvent.click(await screen.findByRole("button", { name: /continue/i }))

    const continueBtn = screen.getByRole("button", { name: /continue/i })
    fireEvent.change(screen.getByPlaceholderText(/knowledge domain/i), {
      target: { value: "tiny" }, // 4 chars — below min
    })
    expect(continueBtn).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText(/knowledge domain/i), {
      target: { value: "A valid purpose string." },
    })
    expect(continueBtn).not.toBeDisabled()
  })

  it("renders the DNS preview from slug + enterprise slug", async () => {
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    fireEvent.click(await screen.findByRole("button", { name: /continue/i }))
    fireEvent.change(screen.getByPlaceholderText(/knowledge domain/i), {
      target: { value: "Campaign agents." },
    })
    fireEvent.click(screen.getByRole("button", { name: /continue/i }))
    fireEvent.click(screen.getByRole("button", { name: /continue/i }))

    expect(screen.getByTestId("dns-preview").textContent).toBe(
      "marketing.acme.8th-layer.ai",
    )
  })

  it("blocks the DNS step Continue until the name is confirmed", async () => {
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    fireEvent.click(await screen.findByRole("button", { name: /continue/i }))
    fireEvent.change(screen.getByPlaceholderText(/knowledge domain/i), {
      target: { value: "Campaign agents." },
    })
    fireEvent.click(screen.getByRole("button", { name: /continue/i }))
    fireEvent.click(screen.getByRole("button", { name: /continue/i }))

    const continueBtn = screen.getByRole("button", { name: /continue/i })
    expect(continueBtn).toBeDisabled()
    fireEvent.click(screen.getByRole("checkbox"))
    expect(continueBtn).not.toBeDisabled()
  })

  it("supports Back navigation through the step machine", async () => {
    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/marketing/i), {
      target: { value: "marketing" },
    })
    fireEvent.click(await screen.findByRole("button", { name: /continue/i }))
    expect(screen.getByText(/describe its purpose/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /back/i }))
    expect(screen.getByText(/name your l2/i)).toBeInTheDocument()
  })

  it("reaches the review step and shows the summary", async () => {
    renderPage()
    await advanceToReview()
    expect(screen.getByText(/review & provision/i)).toBeInTheDocument()
    expect(screen.getByText("marketing.acme.8th-layer.ai")).toBeInTheDocument()
  })

  it("provisions and transitions to the progress step", async () => {
    // Default mockApi already returns the 202 create body with stream_url.
    renderPage()
    await advanceToReview()
    fireEvent.click(screen.getByRole("button", { name: /provision l2/i }))

    await waitFor(() =>
      expect(screen.getByTestId("progress-step")).toBeInTheDocument(),
    )
    // The progress step opened the SSE stream from the response.
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe(
      "/api/v1/admin/l2s/jobs/job-1/stream",
    )
  })

  it("bounces back to the name step on a 409 at provision time", async () => {
    mockApi({
      create: {
        ok: false,
        status: 409,
        body: { detail: "L2 slug already in use", code: "L2_SLUG_TAKEN" },
      },
    })
    renderPage()
    await advanceToReview()
    fireEvent.click(screen.getByRole("button", { name: /provision l2/i }))

    await waitFor(() =>
      expect(screen.getByText(/name your l2/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/already in use/i)).toBeInTheDocument()
  })
})
