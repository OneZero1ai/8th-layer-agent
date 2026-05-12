import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PersonasPage } from "./PersonasPage"

type MockResponse = {
  ok: boolean
  status: number
  body: unknown
}

function queueResponses(responses: MockResponse[]) {
  let i = 0
  globalThis.fetch = vi.fn().mockImplementation(() => {
    const resp = responses[i] ?? responses[responses.length - 1]
    i += 1
    return Promise.resolve({
      ok: resp.ok,
      status: resp.status,
      json: () => Promise.resolve(resp.body),
    })
  }) as unknown as typeof fetch
}

const emptyList = { items: [], total: 0, limit: 50, offset: 0 }

const aliceAssignment = {
  username: "alice",
  email: "alice@example.com",
  persona: "viewer",
  assigned_at: "2026-05-12T10:00:00+00:00",
  assigned_by: "admin@8th-layer",
  disabled_at: null,
}

describe("PersonasPage", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("renders the empty state when no personas exist", async () => {
    queueResponses([{ ok: true, status: 200, body: emptyList }])
    render(<PersonasPage />)
    expect(await screen.findByText(/no personas yet/i)).toBeInTheDocument()
  })

  it("lists personas returned by the API", async () => {
    const listWithAlice = {
      items: [aliceAssignment],
      total: 1,
      limit: 50,
      offset: 0,
    }
    queueResponses([{ ok: true, status: 200, body: listWithAlice }])
    render(<PersonasPage />)
    expect(await screen.findByText("alice")).toBeInTheDocument()
    expect(screen.getByText("alice@example.com")).toBeInTheDocument()
    expect(screen.getByText("viewer")).toBeInTheDocument()
  })

  it("create modal submits POST and refreshes list", async () => {
    const createResponse = {
      username: "bob",
      email: "bob@example.com",
      persona: "agent",
      assigned_at: "2026-05-12T11:00:00+00:00",
      assigned_by: "admin@8th-layer",
      invite_sent: true,
    }
    const listAfterCreate = {
      items: [
        {
          username: "bob",
          email: "bob@example.com",
          persona: "agent",
          assigned_at: "2026-05-12T11:00:00+00:00",
          assigned_by: "admin@8th-layer",
          disabled_at: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    }
    queueResponses([
      { ok: true, status: 200, body: emptyList },
      { ok: true, status: 201, body: createResponse },
      { ok: true, status: 200, body: listAfterCreate },
    ])

    render(<PersonasPage />)
    await screen.findByText(/no personas yet/i)

    // Open create modal.
    fireEvent.click(screen.getByRole("button", { name: /\+ add persona/i }))
    expect(
      await screen.findByRole("dialog", { name: /invite a human/i }),
    ).toBeInTheDocument()

    // Fill in and submit.
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "bob@example.com" },
    })
    fireEvent.change(screen.getByLabelText(/username/i), {
      target: { value: "bob" },
    })
    const form = screen
      .getByRole("button", { name: /create & invite/i })
      .closest("form")
    if (!form) throw new Error("create form not found")
    fireEvent.submit(form)

    // Dialog closes and list updates.
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    )
    expect(await screen.findByText("bob")).toBeInTheDocument()
  })

  it("edit modal patches persona and refreshes list", async () => {
    const listWithAlice = {
      items: [aliceAssignment],
      total: 1,
      limit: 50,
      offset: 0,
    }
    const patchResponse = {
      username: "alice",
      persona: "admin",
      assigned_at: "2026-05-12T12:00:00+00:00",
      assigned_by: "admin@8th-layer",
    }
    const listAfterPatch = {
      items: [{ ...aliceAssignment, persona: "admin" }],
      total: 1,
      limit: 50,
      offset: 0,
    }
    queueResponses([
      { ok: true, status: 200, body: listWithAlice },
      { ok: true, status: 200, body: patchResponse },
      { ok: true, status: 200, body: listAfterPatch },
    ])

    render(<PersonasPage />)
    await screen.findByText("alice")

    // Click edit.
    fireEvent.click(screen.getByLabelText(/edit persona for alice/i))
    // The dialog's accessible name comes from the heading which shows the username.
    const editDialog = await screen.findByRole("dialog", { name: /alice/i })
    expect(editDialog).toBeInTheDocument()

    // Change persona and save.
    const select = screen.getByRole("combobox")
    fireEvent.change(select, { target: { value: "admin" } })
    const form = screen
      .getByRole("button", { name: /^save$/i })
      .closest("form")
    if (!form) throw new Error("save form not found")
    fireEvent.submit(form)

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    )
    expect(await screen.findByText("admin")).toBeInTheDocument()
  })

  it("disable confirm dialog triggers POST disable and greys row", async () => {
    const listWithAlice = {
      items: [aliceAssignment],
      total: 1,
      limit: 50,
      offset: 0,
    }
    const disableResponse = {
      username: "alice",
      disabled_at: "2026-05-12T13:00:00+00:00",
    }
    const listAfterDisable = {
      items: [
        { ...aliceAssignment, disabled_at: "2026-05-12T13:00:00+00:00" },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    }
    queueResponses([
      { ok: true, status: 200, body: listWithAlice },
      { ok: true, status: 200, body: disableResponse },
      { ok: true, status: 200, body: listAfterDisable },
    ])

    render(<PersonasPage />)
    await screen.findByText("alice")

    // Click disable.
    fireEvent.click(screen.getByLabelText(/disable persona for alice/i))
    const disableDialog = await screen.findByRole("dialog")
    expect(disableDialog).toBeInTheDocument()

    // Confirm.
    const confirmButton = Array.from(
      disableDialog.querySelectorAll("button"),
    ).find((b) => b.textContent?.trim() === "Disable")
    if (!confirmButton) throw new Error("confirm button not found")
    fireEvent.click(confirmButton)

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    )
    // Row should now show "Disabled" badge.
    expect(
      await screen.findByText("Disabled", { selector: "span" }),
    ).toBeInTheDocument()
    // Edit and Disable action buttons should be gone for disabled row.
    expect(
      screen.queryByLabelText(/edit persona for alice/i),
    ).not.toBeInTheDocument()
  })
})
