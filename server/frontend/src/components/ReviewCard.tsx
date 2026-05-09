import { forwardRef } from "react"
import type { DragState, PointerHandlers } from "../hooks/useCardDrag"
import {
  FLY_OFF_MS,
  MAX_ROTATION_DEG,
  SNAP_BACK_MS,
} from "../hooks/useCardDrag"
import type { KnowledgeUnit, Selection } from "../types"
import { timeAgo } from "../utils"
import { DomainTags } from "./DomainTags"

interface Props {
  unit: KnowledgeUnit
  selection: Selection
  drag: DragState
  pointerHandlers: PointerHandlers
}

const CARD_STYLES: Record<string, string> = {
  neutral: "border-[var(--rule-strong)] bg-[var(--surface-raised)]",
  approve:
    "border-[color-mix(in_srgb,var(--emerald)_60%,transparent)] bg-[color-mix(in_srgb,var(--emerald)_8%,transparent)]",
  reject:
    "border-[color-mix(in_srgb,var(--rose)_60%,transparent)] bg-[color-mix(in_srgb,var(--rose)_8%,transparent)]",
  skip: "border-[var(--rule-strong)] bg-[var(--surface-hover)]",
}

const ACTION_BOX_STYLES: Record<string, string> = {
  neutral:
    "bg-[color-mix(in_srgb,var(--cyan)_8%,transparent)] border-[var(--cyan)] text-[var(--cyan)]",
  approve:
    "bg-[color-mix(in_srgb,var(--emerald)_10%,transparent)] border-[var(--emerald)] text-[var(--emerald)]",
  reject:
    "bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] border-[var(--rose)] text-[var(--rose)]",
  skip: "bg-[var(--surface-hover)] border-[var(--rule-strong)] text-[var(--ink-dim)]",
}

function confidenceColor(c: number): string {
  if (c < 0.3) return "text-[var(--rose)]"
  if (c < 0.5) return "text-[var(--gold)]"
  if (c < 0.7) return "text-[var(--gold)]"
  return "text-[var(--emerald)]"
}

export const ReviewCard = forwardRef<HTMLDivElement, Props>(function ReviewCard(
  { unit, selection, drag, pointerHandlers },
  ref,
) {
  const activeState =
    drag.isDragging || drag.isFlyingOff ? drag.dragAction : selection
  const cardStyle = CARD_STYLES[activeState ?? "neutral"]
  const actionBoxStyle = ACTION_BOX_STYLES[activeState ?? "neutral"]

  const rotation = drag.isDragging
    ? (drag.offset.x / 300) * MAX_ROTATION_DEG
    : 0
  const shadowScale = drag.isDragging ? 1 + drag.dragProgress * 0.5 : 1
  const transform = `translate(${drag.offset.x}px, ${drag.offset.y}px) rotate(${rotation}deg)`
  const transition = drag.isDragging
    ? "none"
    : drag.isFlyingOff
      ? `transform ${FLY_OFF_MS}ms ease-in, box-shadow ${FLY_OFF_MS}ms ease-in`
      : `transform ${SNAP_BACK_MS}ms ease-out, box-shadow ${SNAP_BACK_MS}ms ease-out`
  // Slightly stronger shadow on dark for the floating-card effect.
  const shadow = `0 ${4 * shadowScale}px ${28 * shadowScale}px rgba(0,0,0,${0.45 * shadowScale}), 0 0 0 1px rgba(255,255,255,0.02)`

  return (
    <div
      ref={ref}
      className={`relative z-0 border rounded-2xl p-7 max-w-xl mx-auto select-none touch-none backdrop-blur-sm ${cardStyle}`}
      style={{ transform, transition, boxShadow: shadow }}
      {...pointerHandlers}
    >
      <div className="flex items-center justify-between mb-4">
        <DomainTags domains={unit.domains} variant={activeState} />
        {unit.evidence.first_observed && (
          <span className="font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)]">
            {timeAgo(unit.evidence.first_observed)}
          </span>
        )}
      </div>

      <h2 className="font-display text-2xl text-[var(--ink)] mb-3 leading-snug">
        {unit.insight.summary}
      </h2>

      <p className="text-[var(--ink-dim)] mb-5 leading-relaxed text-[15px]">
        {unit.insight.detail}
      </p>

      <div
        className={`border-l-2 rounded-r-lg px-4 py-3 mb-6 ${actionBoxStyle}`}
      >
        <span
          className="eyebrow"
          style={{ color: "currentcolor", opacity: 0.85 }}
        >
          Action
        </span>
        <p className="text-[var(--ink)] text-sm mt-1.5 leading-relaxed">
          {unit.insight.action}
        </p>
      </div>

      <div className="flex gap-6 text-sm border-t border-[var(--rule)] pt-4">
        <span className="text-[var(--ink-mute)]">
          <span className="eyebrow mr-1">Confidence</span>
          <strong
            className={`font-mono-brand ${confidenceColor(unit.evidence.confidence)}`}
          >
            {unit.evidence.confidence.toFixed(2)}
          </strong>
        </span>
        <span className="text-[var(--ink-mute)]">
          <span className="eyebrow mr-1">Confirmations</span>
          <strong className="font-mono-brand text-[var(--ink)]">
            {unit.evidence.confirmations}
          </strong>
        </span>
      </div>
    </div>
  )
})
