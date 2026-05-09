/**
 * Placeholder 8th-Layer.ai wordmark.
 *
 * The final logo is still in concepting (see crosstalk-enterprise/docs/brand/
 * logo-concepts-2026-05-08.md). Until then we ship a typographic mark in
 * Fraunces — the "8" sits in the display face, "L" in mono — as a visually
 * distinctive, license-free placeholder. Fully replaceable when an SVG lands.
 */

interface Props {
  size?: "sm" | "md" | "lg"
  variant?: "full" | "compact"
}

const SIZES = {
  sm: { mark: "text-base", word: "text-xs" },
  md: { mark: "text-2xl", word: "text-sm" },
  lg: { mark: "text-5xl", word: "text-base" },
} as const

export function Wordmark({ size = "md", variant = "full" }: Props) {
  const s = SIZES[size]
  return (
    <span
      role="img"
      aria-label="8th-Layer.ai"
      className="inline-flex items-center gap-2 select-none"
    >
      <span
        aria-hidden="true"
        className={`font-display ${s.mark} text-[var(--ink)] leading-none`}
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
        className={`font-mono-brand ${s.word} text-[var(--ink-dim)] uppercase tracking-[0.22em]`}
      >
        {variant === "compact" ? "L8" : "8TH·LAYER"}
      </span>
    </span>
  )
}
