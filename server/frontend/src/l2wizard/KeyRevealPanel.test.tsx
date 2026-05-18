/**
 * FO-3 Phase 3 — tests for `KeyRevealPanel` (agent#193).
 *
 * Covers the one-time key reveal: the key renders, copy-on-click uses the
 * clipboard, the "Open L2 Admin" link is gated on acknowledgement, and the
 * key is never written to localStorage / sessionStorage.
 */

import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { KeyRevealPanel } from "./KeyRevealPanel"

const RESULT = {
  l2_id: "acme/marketing",
  l2_slug: "marketing",
  admin_api_key: "cqa.v1.abcdef0123456789",
  admin_url: "https://marketing.acme.8th-layer.ai/admin",
  dns_name: "marketing.acme.8th-layer.ai",
}

const originalClipboard = navigator.clipboard

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  })
})

afterEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: originalClipboard,
  })
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe("KeyRevealPanel", () => {
  it("renders the one-time admin API key", () => {
    render(<KeyRevealPanel result={RESULT} />)
    expect(screen.getByTestId("admin-api-key").textContent).toBe(
      RESULT.admin_api_key,
    )
    expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument()
  })

  it("copies the key to the clipboard on click", async () => {
    render(<KeyRevealPanel result={RESULT} />)
    fireEvent.click(screen.getByRole("button", { name: /copy/i }))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      RESULT.admin_api_key,
    )
    expect(await screen.findByText(/copied/i)).toBeInTheDocument()
  })

  it("gates the Open L2 Admin link behind the acknowledgement checkbox", () => {
    render(<KeyRevealPanel result={RESULT} />)
    const link = screen.getByRole("link", { name: /open l2 admin/i })
    expect(link).toHaveAttribute("aria-disabled", "true")

    fireEvent.click(screen.getByRole("checkbox"))
    expect(link).toHaveAttribute("aria-disabled", "false")
    expect(link).toHaveAttribute("href", RESULT.admin_url)
  })

  it("never persists the key to web storage", () => {
    render(<KeyRevealPanel result={RESULT} />)
    fireEvent.click(screen.getByRole("button", { name: /copy/i }))
    const haystack = [
      ...Array.from({ length: localStorage.length }, (_, i) =>
        localStorage.getItem(localStorage.key(i) ?? ""),
      ),
      ...Array.from({ length: sessionStorage.length }, (_, i) =>
        sessionStorage.getItem(sessionStorage.key(i) ?? ""),
      ),
    ].join("|")
    expect(haystack).not.toContain(RESULT.admin_api_key)
  })

  it("falls back to an email notice when no inline key is returned", () => {
    render(<KeyRevealPanel result={{ admin_url: RESULT.admin_url }} />)
    expect(screen.queryByTestId("admin-api-key")).not.toBeInTheDocument()
    expect(
      screen.getByText(/did not return an inline key/i),
    ).toBeInTheDocument()
  })
})
