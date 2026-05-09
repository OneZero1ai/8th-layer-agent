import type { Selection } from "../types"

const TAG_STYLES: Record<string, string> = {
  neutral:
    "bg-[color-mix(in_srgb,var(--violet)_12%,transparent)] text-[var(--violet)] border border-[color-mix(in_srgb,var(--violet)_22%,transparent)]",
  approve:
    "bg-[color-mix(in_srgb,var(--emerald)_12%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_24%,transparent)]",
  reject:
    "bg-[color-mix(in_srgb,var(--rose)_12%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_24%,transparent)]",
  skip: "bg-[var(--surface-hover)] text-[var(--ink-dim)] border border-[var(--rule-strong)]",
}

interface Props {
  domains: string[]
  variant?: Selection
}

export function DomainTags({ domains, variant }: Props) {
  const style = TAG_STYLES[variant ?? "neutral"]
  return (
    <div className="flex flex-wrap gap-1.5">
      {[...domains].sort().map((d) => (
        <span
          key={d}
          className={`rounded-full px-2.5 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.14em] ${style}`}
        >
          {d}
        </span>
      ))}
    </div>
  )
}
