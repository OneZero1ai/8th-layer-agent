import type { Selection } from "../types"

interface Props {
  selection: Selection
  onSelect: (s: Selection) => void
  onConfirm: () => void
  disabled: boolean
}

// Brand-mapped action button styles. Each row is [base, selected, deemphasized].
const STYLES = {
  reject: {
    selected:
      "bg-[var(--rose)] text-[#1a0a10] ring-2 ring-[color-mix(in_srgb,var(--rose)_45%,transparent)]",
    base: "bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_28%,transparent)] hover:bg-[color-mix(in_srgb,var(--rose)_18%,transparent)]",
    dim: "bg-[color-mix(in_srgb,var(--rose)_6%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_18%,transparent)] opacity-40",
  },
  skip: {
    selected:
      "bg-[var(--ink-dim)] text-[var(--bg-via)] ring-2 ring-[color-mix(in_srgb,var(--ink-dim)_30%,transparent)]",
    base: "bg-[var(--surface-hover)] text-[var(--ink-dim)] border border-[var(--rule-strong)] hover:bg-[color-mix(in_srgb,var(--ink-dim)_10%,transparent)]",
    dim: "bg-[var(--surface)] text-[var(--ink-mute)] border border-[var(--rule)] opacity-40",
  },
  approve: {
    selected:
      "bg-[var(--emerald)] text-[#04140c] ring-2 ring-[color-mix(in_srgb,var(--emerald)_45%,transparent)]",
    base: "bg-[color-mix(in_srgb,var(--emerald)_10%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_28%,transparent)] hover:bg-[color-mix(in_srgb,var(--emerald)_18%,transparent)]",
    dim: "bg-[color-mix(in_srgb,var(--emerald)_6%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_18%,transparent)] opacity-40",
  },
} as const

function buttonClass(
  variant: keyof typeof STYLES,
  selection: Selection,
): string {
  const s = STYLES[variant]
  if (selection === variant) return s.selected
  if (selection) return s.dim
  return s.base
}

export function ReviewActions({
  selection,
  onSelect,
  onConfirm,
  disabled,
}: Props) {
  return (
    <div className="max-w-xl mx-auto mt-6 hidden pointer-fine:flex flex-col items-center gap-3">
      <div className="flex gap-3 justify-center">
        <button
          type="button"
          onClick={() => {
            if (selection === "reject") onConfirm()
            else onSelect("reject")
          }}
          disabled={disabled}
          className={`px-7 py-2.5 rounded-lg font-mono-brand text-[11px] uppercase tracking-[0.18em] transition-all duration-200 disabled:opacity-50 ${buttonClass("reject", selection)}`}
        >
          {selection === "reject" ? "Confirm Reject" : "← Reject"}
        </button>
        <button
          type="button"
          onClick={() => {
            if (selection === "skip") onConfirm()
            else onSelect("skip")
          }}
          disabled={disabled}
          className={`px-5 py-2.5 rounded-lg font-mono-brand text-[11px] uppercase tracking-[0.18em] transition-all duration-200 disabled:opacity-50 ${buttonClass("skip", selection)}`}
        >
          {selection === "skip" ? "Confirm Skip" : "↑↓ Skip"}
        </button>
        <button
          type="button"
          onClick={() => {
            if (selection === "approve") onConfirm()
            else onSelect("approve")
          }}
          disabled={disabled}
          className={`px-7 py-2.5 rounded-lg font-mono-brand text-[11px] uppercase tracking-[0.18em] transition-all duration-200 disabled:opacity-50 ${buttonClass("approve", selection)}`}
        >
          {selection === "approve" ? "Confirm Approve" : "Approve →"}
        </button>
      </div>
      <p
        className={`text-center font-mono-brand text-[10px] uppercase tracking-[0.18em] ${
          selection ? "text-[var(--ink-dim)]" : "text-[var(--ink-faint)]"
        }`}
      >
        {selection
          ? "Click again or press Space/Enter to confirm · Esc to cancel"
          : "Arrow keys to select · Space/Enter to confirm"}
      </p>
    </div>
  )
}
