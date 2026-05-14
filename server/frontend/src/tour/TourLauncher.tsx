/**
 * The `?` button in the header — manual tour replay.
 *
 * Sits in the nav so it's available everywhere the Layout renders.
 * Single button, single click, restarts the tour from step 1.
 */

import { useTour } from "./useTour"

export function TourLauncher() {
  const { start, active } = useTour()
  if (active) return null // hide while the tour is showing — avoid restart loops
  return (
    <button
      type="button"
      onClick={start}
      aria-label="Replay onboarding tour"
      title="Replay tour"
      className="font-mono-brand text-[12px] w-6 h-6 inline-flex items-center justify-center rounded-full border border-[var(--rule-strong)] text-[var(--ink-mute)] hover:text-[var(--ink)] hover:border-[var(--brand-primary,#5bd0ff)] transition-colors"
    >
      ?
    </button>
  )
}
