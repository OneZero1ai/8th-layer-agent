/**
 * ThemeProvider — fetches `/api/v1/theme` once on mount, exposes the resolved
 * 3-tier theme via context, and applies the brand override CSS custom
 * properties on `:root` (Decision 30).
 *
 * The CSS-token indirection (`--brand-primary`, `--brand-secondary`) defaults
 * to the platform palette in `index.css`. When the API call returns:
 *
 *   --brand-primary  ← enterprise.accent_hex   (else: platform.cyan default)
 *   --brand-secondary ← l2.subaccent_hex       (else: enterprise accent or
 *                                                 platform.violet default)
 *
 * On API failure the FE keeps the CSS-defined defaults — no flash, no error
 * dialog. `console.warn` records the failure for debugging.
 *
 * The provider also exposes a `loading` flag so consumers (Wordmark, Layout)
 * can opt to render a placeholder vs the platform-only fallback while the
 * fetch is in flight.
 */

import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react"
import type { ResolvedTheme } from "./types"

interface ThemeState {
  theme: ResolvedTheme | null
  loading: boolean
  error: Error | null
}

const ThemeContext = createContext<ThemeState | null>(null)

// Strict 6-digit hex shape — defense in depth. The backend resolver +
// migration 0020 CHECK constraint also validate (8l-reviewer MEDIUM 1
// on PR #219). Anything that fails this check stays at the CSS default.
const HEX_RE = /^#[0-9a-fA-F]{6}$/

function safeHex(value: unknown): string | null {
  if (typeof value !== "string") return null
  return HEX_RE.test(value) ? value : null
}

/**
 * Apply the resolved theme's brand overrides as CSS custom properties on
 * `document.documentElement`. Only run under `data-theme="8th-layer"` per
 * the upstream-compatibility rule (mainline-cq stays platform-fixed; see
 * issue 199 spec refinement note).
 */
function applyBrandOverrides(theme: ResolvedTheme): void {
  const root = document.documentElement
  // Defensive: only override under the 8th-layer data-theme. mainline-cq
  // is a license-friendly upstream-compatible mode and must not carry
  // customer-specific brand overrides.
  if (root.getAttribute("data-theme") !== "8th-layer") return

  const enterpriseAccent = safeHex(theme.enterprise.accent_hex)
  const l2Subaccent = safeHex(theme.l2.subaccent_hex)

  if (enterpriseAccent) {
    root.style.setProperty("--brand-primary", enterpriseAccent)
  }
  // Sub-accent precedence: explicit L2 sub-accent wins; otherwise the
  // Enterprise accent gets used as the secondary too (matches Decision
  // 30's CSS example: `--brand-secondary: var(--l2-subaccent,
  // --enterprise-accent)`).
  const secondary = l2Subaccent || enterpriseAccent
  if (secondary) {
    root.style.setProperty("--brand-secondary", secondary)
  }
}

/**
 * Test seam — exposed so unit tests can inject a fixture without hitting
 * `fetch`. Not exported from the package index.
 */
export interface ThemeProviderProps {
  children: ReactNode
  initialTheme?: ResolvedTheme
  fetcher?: () => Promise<ResolvedTheme>
}

async function defaultFetcher(): Promise<ResolvedTheme> {
  const resp = await fetch("/api/v1/theme", { credentials: "include" })
  if (!resp.ok) {
    throw new Error(`theme fetch failed: HTTP ${resp.status}`)
  }
  return resp.json()
}

export function ThemeProvider({
  children,
  initialTheme,
  fetcher = defaultFetcher,
}: ThemeProviderProps) {
  const [theme, setTheme] = useState<ResolvedTheme | null>(initialTheme ?? null)
  const [loading, setLoading] = useState<boolean>(!initialTheme)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (initialTheme) {
      applyBrandOverrides(initialTheme)
      return
    }
    let cancelled = false
    fetcher()
      .then((resolved) => {
        if (cancelled) return
        setTheme(resolved)
        applyBrandOverrides(resolved)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const e = err instanceof Error ? err : new Error(String(err))
        setError(e)
        // Soft-fail: keep CSS defaults, log for debugging. The shell stays
        // usable on the platform palette.
        console.warn("[ThemeProvider] falling back to CSS defaults:", e)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [fetcher, initialTheme])

  return (
    <ThemeContext.Provider value={{ theme, loading, error }}>
      {children}
    </ThemeContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- standard React context pattern.
export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error("useTheme must be used within ThemeProvider")
  }
  return ctx
}
