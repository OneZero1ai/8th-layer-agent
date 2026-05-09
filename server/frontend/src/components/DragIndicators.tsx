import type { DragState } from "../hooks/useCardDrag"
import { BADGE_APPEAR_RATIO } from "../hooks/useCardDrag"

const INDICATORS = {
  approve: {
    symbol: "✓",
    bg: "bg-[var(--emerald)] text-[#04140c]",
    label: "Approve",
    color: "text-[var(--emerald)]",
  },
  reject: {
    symbol: "✕",
    bg: "bg-[var(--rose)] text-[#1a0a10]",
    label: "Reject",
    color: "text-[var(--rose)]",
  },
  skip: {
    symbol: "—",
    bg: "bg-[var(--ink-dim)] text-[var(--bg-via)]",
    label: "Skip",
    color: "text-[var(--ink-dim)]",
  },
} as const

interface Props {
  drag: DragState
}

function indicatorOpacity(drag: DragState, action: string): number {
  if (!drag.isDragging || drag.dragAction !== action) return 0
  if (drag.dragProgress < BADGE_APPEAR_RATIO) return 0
  return Math.min(
    (drag.dragProgress - BADGE_APPEAR_RATIO) / (1 - BADGE_APPEAR_RATIO),
    1,
  )
}

export function DragIndicators({ drag }: Props) {
  if (!drag.isDragging) return null

  return (
    <>
      {/* Right edge — approve. */}
      <div
        className="fixed right-3 top-1/2 -translate-y-1/2 z-10 pointer-events-none flex flex-col items-center gap-1"
        style={{
          opacity: indicatorOpacity(drag, "approve"),
          transition: "opacity 100ms ease-out",
        }}
      >
        <div
          className={`w-10 h-10 ${INDICATORS.approve.bg} rounded-full flex items-center justify-center text-lg font-semibold shadow-lg`}
        >
          {INDICATORS.approve.symbol}
        </div>
        <span
          className={`font-mono-brand text-[10px] uppercase tracking-[0.18em] ${INDICATORS.approve.color}`}
        >
          {INDICATORS.approve.label}
        </span>
      </div>

      {/* Left edge — reject. */}
      <div
        className="fixed left-3 top-1/2 -translate-y-1/2 z-10 pointer-events-none flex flex-col items-center gap-1"
        style={{
          opacity: indicatorOpacity(drag, "reject"),
          transition: "opacity 100ms ease-out",
        }}
      >
        <div
          className={`w-10 h-10 ${INDICATORS.reject.bg} rounded-full flex items-center justify-center text-lg font-semibold shadow-lg`}
        >
          {INDICATORS.reject.symbol}
        </div>
        <span
          className={`font-mono-brand text-[10px] uppercase tracking-[0.18em] ${INDICATORS.reject.color}`}
        >
          {INDICATORS.reject.label}
        </span>
      </div>

      {/* Top edge — skip (drag up). */}
      <div
        className="fixed top-3 left-1/2 -translate-x-1/2 z-10 pointer-events-none flex flex-col items-center gap-1"
        style={{
          opacity: indicatorOpacity(drag, "skip"),
          transition: "opacity 100ms ease-out",
        }}
      >
        <div
          className={`w-10 h-10 ${INDICATORS.skip.bg} rounded-full flex items-center justify-center text-lg font-semibold shadow-lg`}
        >
          {INDICATORS.skip.symbol}
        </div>
        <span
          className={`font-mono-brand text-[10px] uppercase tracking-[0.18em] ${INDICATORS.skip.color}`}
        >
          {INDICATORS.skip.label}
        </span>
      </div>

      {/* Bottom edge — skip (drag down). */}
      <div
        className="fixed bottom-3 left-1/2 -translate-x-1/2 z-10 pointer-events-none flex flex-col items-center gap-1"
        style={{
          opacity: indicatorOpacity(drag, "skip"),
          transition: "opacity 100ms ease-out",
        }}
      >
        <div
          className={`w-10 h-10 ${INDICATORS.skip.bg} rounded-full flex items-center justify-center text-lg font-semibold shadow-lg`}
        >
          {INDICATORS.skip.symbol}
        </div>
        <span
          className={`font-mono-brand text-[10px] uppercase tracking-[0.18em] ${INDICATORS.skip.color}`}
        >
          {INDICATORS.skip.label}
        </span>
      </div>
    </>
  )
}
