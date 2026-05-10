import { render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ThemeProvider, useTheme } from "./ThemeProvider"
import type { ResolvedTheme } from "./types"

const PLATFORM_FIXTURE: ResolvedTheme = {
  platform: {
    name: "8th-Layer.ai",
    version: "1.0.0",
    tokens: { cyan: "#5bd0ff", violet: "#a685ff" },
  },
  enterprise: {
    id: "8th-layer-corp",
    display_name: "8th-layer-corp",
    logo_url: null,
    accent_hex: null,
    dark_mode_only: true,
  },
  l2: {
    id: "8th-layer-corp/engineering",
    label: "engineering",
    subaccent_hex: null,
    hero_motif: null,
  },
}

const OVERRIDE_FIXTURE: ResolvedTheme = {
  ...PLATFORM_FIXTURE,
  enterprise: {
    ...PLATFORM_FIXTURE.enterprise,
    accent_hex: "#ff8800",
  },
  l2: {
    ...PLATFORM_FIXTURE.l2,
    subaccent_hex: "#0088ff",
  },
}

function ThemeReadout() {
  const { theme, loading, error } = useTheme()
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ? error.message : ""}</span>
      <span data-testid="enterprise">
        {theme ? theme.enterprise.id : "no-theme"}
      </span>
    </div>
  )
}

describe("ThemeProvider", () => {
  beforeEach(() => {
    document.documentElement.setAttribute("data-theme", "8th-layer")
    document.documentElement.style.removeProperty("--brand-primary")
    document.documentElement.style.removeProperty("--brand-secondary")
  })

  afterEach(() => {
    document.documentElement.removeAttribute("data-theme")
  })

  it("fetches the theme on mount and exposes it via context", async () => {
    const fetcher = vi.fn().mockResolvedValue(PLATFORM_FIXTURE)
    render(
      <ThemeProvider fetcher={fetcher}>
        <ThemeReadout />
      </ThemeProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false")
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("enterprise").textContent).toBe("8th-layer-corp")
  })

  it("applies brand override CSS custom properties when set", async () => {
    const fetcher = vi.fn().mockResolvedValue(OVERRIDE_FIXTURE)
    render(
      <ThemeProvider fetcher={fetcher}>
        <ThemeReadout />
      </ThemeProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false")
    })
    expect(
      document.documentElement.style.getPropertyValue("--brand-primary"),
    ).toBe("#ff8800")
    expect(
      document.documentElement.style.getPropertyValue("--brand-secondary"),
    ).toBe("#0088ff")
  })

  it("does NOT touch CSS overrides under data-theme=mainline-cq", async () => {
    document.documentElement.setAttribute("data-theme", "mainline-cq")
    const fetcher = vi.fn().mockResolvedValue(OVERRIDE_FIXTURE)
    render(
      <ThemeProvider fetcher={fetcher}>
        <ThemeReadout />
      </ThemeProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false")
    })
    expect(
      document.documentElement.style.getPropertyValue("--brand-primary"),
    ).toBe("")
  })

  it("falls back gracefully on fetch failure", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const fetcher = vi.fn().mockRejectedValue(new Error("boom"))
    render(
      <ThemeProvider fetcher={fetcher}>
        <ThemeReadout />
      </ThemeProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false")
    })
    expect(screen.getByTestId("error").textContent).toBe("boom")
    expect(screen.getByTestId("enterprise").textContent).toBe("no-theme")
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it("uses initialTheme when supplied (test seam, no fetch)", () => {
    const fetcher = vi.fn()
    render(
      <ThemeProvider initialTheme={PLATFORM_FIXTURE} fetcher={fetcher}>
        <ThemeReadout />
      </ThemeProvider>,
    )
    expect(fetcher).not.toHaveBeenCalled()
    expect(screen.getByTestId("loading").textContent).toBe("false")
    expect(screen.getByTestId("enterprise").textContent).toBe("8th-layer-corp")
  })
})
