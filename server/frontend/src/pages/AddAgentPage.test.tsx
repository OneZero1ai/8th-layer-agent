/**
 * FO-4 Phase 2 — tests for the Add-Agent page (agent#194 / Decision 33).
 *
 * Covers the single-request mint flow: the form renders, a successful mint
 * replaces the form with the completion panel (token + all three install
 * paths), and an error response (409 duplicate / 422 bad input) surfaces the
 * backend message. `fetch` is stubbed per the PersonasPage test convention.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { AddAgentPage } from "./AddAgentPage"

const originalFetch = globalThis.fetch

type MockResponse = { ok: boolean; status: number; body: unknown }

/**
 * Route the fetch mock by request: the page fires a GET (listAgentKeys, on
 * mount and after a successful mint) and a POST (mintAgentKey). `list`
 * answers the GET; `mint` answers the POST.
 */
function mockApi(opts: { list?: MockResponse; mint?: MockResponse }) {
  const list: MockResponse = opts.list ?? {
    ok: true,
    status: 200,
    body: { data: [], count: 0 },
  }
  const mint: MockResponse = opts.mint ?? {
    ok: true,
    status: 201,
    body: mintResponse,
  }
  globalThis.fetch = vi
    .fn()
    .mockImplementation((_url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase()
      const resp = method === "POST" ? mint : list
      return Promise.resolve({
        ok: resp.ok,
        status: resp.status,
        json: () => Promise.resolve(resp.body),
      })
    }) as unknown as typeof fetch
}

const mintResponse = {
  id: "key-1",
  name: "campaign-researcher",
  labels: [],
  prefix: "cqa.v1.a",
  ttl: "60d",
  expires_at: "2026-07-18T10:00:00+00:00",
  created_at: "2026-05-19T10:00:00+00:00",
  last_used_at: null,
  revoked_at: null,
  is_expired: false,
  is_active: true,
  agent_username: "agent-campaign-researcher",
  token: "cqa.v1.abcdef0123456789plaintexttokenvalue",
  install: {
    join_command: "8l join --token cqa.v1.abcdef0123456789plaintexttokenvalue",
    enterprise_id: "acme",
    l2: "acme/eng",
    persona: "agent",
  },
}

beforeEach(() => {
  mockApi({})
})

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <AddAgentPage />
    </MemoryRouter>,
  )
}

/** Fill the form with valid input. */
function fillForm(name = "campaign-researcher") {
  fireEvent.change(screen.getByPlaceholderText(/campaign-researcher/i), {
    target: { value: name },
  })
}

describe("AddAgentPage", () => {
  it("renders the form with name, harness, and TTL fields", async () => {
    renderPage()
    expect(screen.getByText(/agent details/i)).toBeInTheDocument()
    expect(
      screen.getByPlaceholderText(/campaign-researcher/i),
    ).toBeInTheDocument()
    // Harness select defaults to Claude Code.
    const harness = screen.getByRole("combobox")
    expect(harness).toHaveValue("claude-code")
    // TTL defaults to 60d.
    expect(screen.getByPlaceholderText("60d")).toHaveValue("60d")
    // List call resolved — the empty agent-key table renders.
    expect(
      await screen.findByText(/no agent keys minted yet/i),
    ).toBeInTheDocument()
  })

  it("blocks Mint until the agent name is valid", () => {
    renderPage()
    const mintBtn = screen.getByRole("button", { name: /mint agent key/i })
    expect(mintBtn).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText(/campaign-researcher/i), {
      target: { value: "x" }, // 1 char — below min
    })
    expect(mintBtn).toBeDisabled()

    fillForm()
    expect(mintBtn).not.toBeDisabled()
  })

  it("blocks Mint on an invalid TTL", () => {
    renderPage()
    fillForm()
    const mintBtn = screen.getByRole("button", { name: /mint agent key/i })
    expect(mintBtn).not.toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText("60d"), {
      target: { value: "soon" }, // not <number><unit>
    })
    expect(mintBtn).toBeDisabled()
  })

  it("mint success shows the token and all three install paths", async () => {
    renderPage()
    fillForm()
    fireEvent.click(screen.getByRole("button", { name: /mint agent key/i }))

    // Completion panel replaces the form.
    await waitFor(() =>
      expect(screen.getByTestId("mint-complete")).toBeInTheDocument(),
    )
    expect(
      screen.queryByRole("button", { name: /mint agent key/i }),
    ).not.toBeInTheDocument()

    // One-time token reveal.
    expect(screen.getByTestId("agent-token").textContent).toBe(
      mintResponse.token,
    )
    expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument()

    // (a) join command.
    expect(screen.getByTestId("join-command").textContent).toBe(
      mintResponse.install.join_command,
    )
    // (b) plugin-install command.
    expect(screen.getByTestId("plugin-command").textContent).toMatch(
      /plugin marketplace add/i,
    )
    // (c) QR code — rendered as an SVG.
    const qrBlock = screen.getByTestId("install-qr")
    expect(qrBlock.querySelector("svg")).not.toBeNull()
  })

  it("shows the backend message on a 409 duplicate name", async () => {
    mockApi({
      mint: {
        ok: false,
        status: 409,
        body: { detail: "An agent named 'campaign-researcher' already exists" },
      },
    })
    renderPage()
    fillForm()
    fireEvent.click(screen.getByRole("button", { name: /mint agent key/i }))

    expect(await screen.findByTestId("mint-error")).toHaveTextContent(
      /already exists/i,
    )
    // The form stays visible — no completion panel.
    expect(screen.queryByTestId("mint-complete")).not.toBeInTheDocument()
  })

  it("shows the backend message on a 422 bad input", async () => {
    mockApi({
      mint: {
        ok: false,
        status: 422,
        body: { detail: "ttl must be a positive duration" },
      },
    })
    renderPage()
    fillForm()
    fireEvent.click(screen.getByRole("button", { name: /mint agent key/i }))

    expect(await screen.findByTestId("mint-error")).toHaveTextContent(
      /positive duration/i,
    )
  })

  it("lists existing agent keys returned by the API", async () => {
    mockApi({
      list: {
        ok: true,
        status: 200,
        body: {
          data: [
            {
              ...mintResponse,
              token: undefined,
              install: undefined,
              name: "existing-agent",
              agent_username: "agent-existing-agent",
            },
          ],
          count: 1,
        },
      },
    })
    renderPage()
    expect(await screen.findByText("existing-agent")).toBeInTheDocument()
    expect(screen.getByText("agent-existing-agent")).toBeInTheDocument()
    expect(screen.getByTestId("agent-key-table")).toBeInTheDocument()
  })
})
