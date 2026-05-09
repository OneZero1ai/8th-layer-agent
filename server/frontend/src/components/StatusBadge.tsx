/**
 * Status pill — proposed / approved / rejected. Uses the brand palette tokens
 * (--gold, --emerald, --rose) so it harmonises in either theme.
 */

const STYLES: Record<string, string> = {
  proposed:
    "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_28%,transparent)]",
  approved:
    "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]",
  rejected:
    "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]",
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${STYLES[status] ?? "bg-[var(--surface-hover)] text-[var(--ink-dim)] border border-[var(--rule)]"}`}
    >
      {status}
    </span>
  )
}
