/**
 * FO-3 Phase 3 — `PhaseProgressBar` (agent#193 / Decision 32).
 *
 * Renders the ~8-phase L2-standup progress for the wizard's progress step.
 * The cq-server proxy reports a 1-based `phase` index, an optional
 * `phase_label`, and an optional `progress_pct` per SSE `phase` event.
 *
 * Display rules:
 *   - The fill width tracks `progressPct` when the proxy reports it; else it
 *     is derived from `phase / totalPhases`.
 *   - Each of the eight standup phases gets a tick: filled (done), active
 *     (current), or pending. The active tick shows the live `phaseLabel`.
 *   - `failed` tints the bar + the active tick rose; `completed` fills it.
 *
 * Visual style matches the admin shell's brand tokens (see ApiKeysPage).
 */

import { L2_STANDUP_PHASES, type L2ProvisioningPhase } from "./types"

interface PhaseProgressBarProps {
  /** Lifecycle flag from `useL2ProvisioningSSE`. */
  lifecycle: L2ProvisioningPhase
  /** 1-based current phase index from the proxy, or null before the first event. */
  phase: number | null
  /** Human-readable label for the current phase, or null. */
  phaseLabel: string | null
  /** Server-reported completion percentage 0–100, or null. */
  progressPct: number | null
}

const TOTAL_PHASES = L2_STANDUP_PHASES.length

/** Clamp a number into [0, 100]. */
function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value))
}

export function PhaseProgressBar({
  lifecycle,
  phase,
  phaseLabel,
  progressPct,
}: PhaseProgressBarProps) {
  const failed = lifecycle === "failed"
  const completed = lifecycle === "completed"

  // Current 1-based phase (0 while still connecting). On completion, treat
  // every phase as done regardless of the last reported index.
  const currentPhase = completed ? TOTAL_PHASES : (phase ?? 0)

  // Fill width: prefer the proxy's progress_pct; else derive from the phase
  // index. Completion always fills to 100%.
  let fillPct: number
  if (completed) {
    fillPct = 100
  } else if (progressPct != null) {
    fillPct = clampPct(progressPct)
  } else {
    fillPct = clampPct((currentPhase / TOTAL_PHASES) * 100)
  }

  const fillColor = failed
    ? "var(--rose)"
    : completed
      ? "var(--emerald)"
      : "var(--brand-primary)"

  return (
    <div className="space-y-4" data-testid="phase-progress">
      <div className="flex items-center justify-between">
        <span className="eyebrow">
          {completed
            ? "Provisioning complete"
            : failed
              ? "Provisioning failed"
              : `Phase ${Math.max(currentPhase, 1)} of ${TOTAL_PHASES}`}
        </span>
        <span className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
          {Math.round(fillPct)}%
        </span>
      </div>

      <div
        className="h-2 w-full overflow-hidden rounded-full bg-[var(--surface-hover)]"
        role="progressbar"
        aria-valuenow={Math.round(fillPct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="L2 provisioning progress"
      >
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${fillPct}%`, backgroundColor: fillColor }}
        />
      </div>

      <ol className="grid gap-1.5">
        {L2_STANDUP_PHASES.map((label, idx) => {
          const oneBased = idx + 1
          const isDone = completed || oneBased < currentPhase
          const isActive = !completed && !failed && oneBased === currentPhase
          const isFailedHere = failed && oneBased === currentPhase

          let dotClass =
            "h-1.5 w-1.5 rounded-full bg-[var(--ink-faint)] shrink-0"
          if (isDone) {
            dotClass = "h-1.5 w-1.5 rounded-full bg-[var(--emerald)] shrink-0"
          } else if (isActive) {
            dotClass =
              "h-1.5 w-1.5 rounded-full bg-[var(--brand-primary)] shrink-0 animate-pulse"
          } else if (isFailedHere) {
            dotClass = "h-1.5 w-1.5 rounded-full bg-[var(--rose)] shrink-0"
          }

          let textClass = "text-[var(--ink-faint)]"
          if (isDone) textClass = "text-[var(--ink-dim)]"
          else if (isActive) textClass = "text-[var(--ink)]"
          else if (isFailedHere) textClass = "text-[var(--rose)]"

          return (
            <li
              key={label}
              className={`flex items-center gap-2 text-xs ${textClass}`}
            >
              <span aria-hidden="true" className={dotClass} />
              <span>
                {isActive && phaseLabel ? phaseLabel : label}
                {isActive && (
                  <span className="ml-2 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--brand-primary)]">
                    in progress
                  </span>
                )}
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
