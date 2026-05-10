/**
 * Three-tier brand wordmark (FO-1d, Decision 30).
 *
 * Renders horizontally as `<Enterprise> · <L2-label> · 8th-Layer.ai`. The
 * Enterprise display name takes the primary visual weight (Fraunces);
 * the L2 label sits as a secondary mono-tagged segment; the platform
 * mark is rendered small + dim as the always-visible co-branding badge.
 *
 * Pre-theme-load fallback: if the theme context hasn't resolved yet (or
 * the API call failed), we render the platform-only mark — preserves
 * the original placeholder visuals so the topbar never goes blank.
 *
 * The 8th-Layer mark itself uses platform tokens (`--ink`, `--ink-dim`)
 * directly — it's the platform identity and never re-tints under
 * customer brand overrides.
 */

import { useTheme } from "../theme"

interface Props {
  size?: "sm" | "md" | "lg"
  variant?: "full" | "compact"
}

const SIZES = {
  sm: { mark: "text-base", word: "text-xs", enterprise: "text-sm" },
  md: { mark: "text-2xl", word: "text-sm", enterprise: "text-lg" },
  lg: { mark: "text-5xl", word: "text-base", enterprise: "text-2xl" },
} as const

function PlatformMark({
  size,
  variant = "full",
  dim = false,
}: {
  size: Props["size"]
  variant?: Props["variant"]
  dim?: boolean
}) {
  const s = SIZES[size ?? "md"]
  return (
    <span
      role="img"
      aria-label="8th-Layer.ai"
      className="inline-flex items-center gap-2 select-none"
    >
      <span
        aria-hidden="true"
        className={`font-display ${s.mark} ${dim ? "text-[var(--ink-dim)]" : "text-[var(--ink)]"} leading-none`}
        style={{
          fontVariantNumeric: "tabular-nums",
          fontWeight: 200,
          letterSpacing: "-0.04em",
        }}
      >
        8
      </span>
      <span
        aria-hidden="true"
        className={`font-mono-brand ${s.word} ${dim ? "text-[var(--ink-faint)]" : "text-[var(--ink-dim)]"} uppercase tracking-[0.22em]`}
      >
        {variant === "compact" ? "L8" : "8TH·LAYER"}
      </span>
    </span>
  )
}

export function Wordmark({ size = "md", variant = "full" }: Props) {
  const { theme } = useTheme()
  const s = SIZES[size]

  // Theme not yet loaded (or failed) → fall back to the platform-only
  // mark. Keeps the placeholder identity visible during the brief
  // window before /api/v1/theme resolves.
  if (!theme) {
    return <PlatformMark size={size} variant={variant} />
  }

  const enterprise = theme.enterprise.display_name
  const l2 = theme.l2.label

  return (
    <span
      role="img"
      aria-label={`${enterprise} · ${l2} · 8th-Layer.ai`}
      className="inline-flex items-center gap-3 select-none"
    >
      {/* Tier 2 — Enterprise display name (primary). Fraunces, full ink. */}
      <span
        className={`font-display ${s.enterprise} text-[var(--ink)] leading-none`}
        style={{ fontWeight: 300, letterSpacing: "-0.01em" }}
      >
        {enterprise}
      </span>
      {/* Tier 3 — L2 label (secondary). Mono, dim, tracked uppercase. */}
      <span
        aria-hidden="true"
        className="font-mono-brand text-[10px] text-[var(--ink-faint)]"
      >
        ·
      </span>
      <span
        className={`font-mono-brand ${s.word} text-[var(--ink-mute)] uppercase tracking-[0.18em]`}
      >
        {l2}
      </span>
      {/* Tier 1 — platform co-branding mark. Always present, always dim. */}
      <span
        aria-hidden="true"
        className="font-mono-brand text-[10px] text-[var(--ink-faint)]"
      >
        ·
      </span>
      <PlatformMark size="sm" variant="compact" dim />
    </span>
  )
}
