/**
 * Spotlight + popover engine for the Founder's First L2 walk-through.
 *
 * Why custom (not react-joyride / intro.js): the admin shell uses
 * project-specific brand tokens (Fraunces / cyan-violet gradient on
 * dark) and a custom-property theme system. A library would import its
 * own CSS and ship default chrome that wouldn't match. ~250 LOC of
 * brand-native overlay is cheaper than restyling someone else's component.
 *
 * Topology:
 *
 *   <TourOverlay>                  ← portal mounted at document.body
 *     <backdrop mask />            ← darkened rect-with-hole around target
 *     <Popover />                  ← positioned next to the target
 *
 * The mask is a single full-viewport <svg> with a <mask> that punches a
 * rounded rectangle around the target. Cheap, no per-pixel work.
 *
 * Positioning: we put the popover ABOVE the target if it fits, else
 * BELOW. Horizontal — clamp to the viewport with an 8px margin. No
 * arrow on v1; the spotlight makes the connection obvious.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import type { TourStep } from "./steps"
import { useTour } from "./useTour"

interface Rect {
  top: number
  left: number
  width: number
  height: number
}

const PADDING = 8 // spotlight inflate, px
const POPOVER_GAP = 14 // space between spotlight and popover, px
const POPOVER_WIDTH = 320 // popover width, px

function useTargetRect(targetId: string | undefined): Rect | null {
  const [rect, setRect] = useState<Rect | null>(null)

  useLayoutEffect(() => {
    if (!targetId) {
      setRect(null)
      return
    }
    function measure() {
      const el = document.querySelector<HTMLElement>(
        `[data-tour-target="${targetId}"]`,
      )
      if (!el) {
        setRect(null)
        return
      }
      const r = el.getBoundingClientRect()
      setRect({ top: r.top, left: r.left, width: r.width, height: r.height })
    }
    // Two RAFs — first paints React's nav changes, second measures the
    // post-paint layout. Without this the first step often measures the
    // pre-navigation DOM and the spotlight lands on stale geometry.
    requestAnimationFrame(() => requestAnimationFrame(measure))
    window.addEventListener("resize", measure)
    window.addEventListener("scroll", measure, true)
    return () => {
      window.removeEventListener("resize", measure)
      window.removeEventListener("scroll", measure, true)
    }
  }, [targetId])

  return rect
}

function popoverPosition(rect: Rect | null): {
  top: number
  left: number
  anchor: "above" | "below" | "center"
} {
  if (!rect) {
    return {
      top: Math.max(120, window.innerHeight / 2 - 100),
      left: Math.max(16, window.innerWidth / 2 - POPOVER_WIDTH / 2),
      anchor: "center",
    }
  }
  const fitsBelow =
    rect.top + rect.height + POPOVER_GAP + 220 < window.innerHeight
  const anchor: "above" | "below" = fitsBelow ? "below" : "above"
  const top =
    anchor === "below"
      ? rect.top + rect.height + POPOVER_GAP
      : Math.max(16, rect.top - POPOVER_GAP - 220)
  const naiveLeft = rect.left + rect.width / 2 - POPOVER_WIDTH / 2
  const left = Math.max(
    16,
    Math.min(window.innerWidth - POPOVER_WIDTH - 16, naiveLeft),
  )
  return { top, left, anchor }
}

function Spotlight({ rect }: { rect: Rect | null }) {
  if (!rect) {
    return (
      <div
        className="fixed inset-0 bg-black/60 pointer-events-auto"
        aria-hidden="true"
      />
    )
  }
  // SVG-mask approach — one rect + one cutout, hardware composited.
  const pad = PADDING
  return (
    <svg
      className="fixed inset-0 w-full h-full pointer-events-auto"
      aria-hidden="true"
      style={{ width: "100vw", height: "100vh" }}
    >
      <defs>
        <mask id="tour-mask">
          <rect x="0" y="0" width="100%" height="100%" fill="white" />
          <rect
            x={rect.left - pad}
            y={rect.top - pad}
            width={rect.width + pad * 2}
            height={rect.height + pad * 2}
            rx="10"
            fill="black"
          />
        </mask>
      </defs>
      <rect
        x="0"
        y="0"
        width="100%"
        height="100%"
        fill="rgba(4, 8, 16, 0.72)"
        mask="url(#tour-mask)"
      />
      <rect
        x={rect.left - pad}
        y={rect.top - pad}
        width={rect.width + pad * 2}
        height={rect.height + pad * 2}
        rx="10"
        fill="none"
        stroke="rgba(91, 208, 255, 0.55)"
        strokeWidth="1.5"
      />
    </svg>
  )
}

function Popover({
  step,
  index,
  total,
  position,
  onNext,
  onSkip,
}: {
  step: TourStep
  index: number
  total: number
  position: { top: number; left: number }
  onNext: () => void
  onSkip: () => void
}) {
  const isLast = index === total - 1
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={`tour-step-${step.id}-title`}
      className="fixed z-[60] pointer-events-auto"
      style={{
        top: position.top,
        left: position.left,
        width: POPOVER_WIDTH,
      }}
    >
      <div
        className="rounded-xl border border-[var(--rule-strong)] bg-[color-mix(in_srgb,var(--bg-via)_94%,transparent)] backdrop-blur-lg p-5 shadow-2xl"
        style={{ boxShadow: "0 20px 60px rgba(0,0,0,0.5)" }}
      >
        <div className="flex items-center justify-between mb-2">
          <span className="font-mono-brand text-[10px] uppercase tracking-[0.22em] text-[var(--ink-mute)]">
            Step {index + 1} / {total}
          </span>
          <button
            type="button"
            onClick={onSkip}
            aria-label="Skip tour"
            className="font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)] hover:text-[var(--rose)] transition-colors"
          >
            Skip
          </button>
        </div>
        <h2
          id={`tour-step-${step.id}-title`}
          className="font-serif-brand text-lg text-[var(--ink)] mb-2"
        >
          {step.title}
        </h2>
        <p className="text-sm text-[var(--ink-dim)] leading-relaxed mb-5">
          {step.body}
        </p>
        <div className="flex items-center justify-between gap-3">
          <div className="flex gap-1.5">
            {Array.from({ length: total }, (_, i) => (
              <span
                // biome-ignore lint/suspicious/noArrayIndexKey: pure-display dots
                key={i}
                className={`h-1.5 w-1.5 rounded-full transition-colors ${
                  i <= index
                    ? "bg-[var(--brand-primary,#5bd0ff)]"
                    : "bg-[var(--rule-strong)]"
                }`}
              />
            ))}
          </div>
          <button
            type="button"
            onClick={onNext}
            className="px-4 py-2 rounded-md font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[#0a0612] bg-gradient-to-r from-[var(--cyan,#5bd0ff)] to-[var(--violet,#a685ff)] hover:brightness-110 transition-all"
          >
            {step.ctaLabel ?? (isLast ? "Finish" : "Next")}
          </button>
        </div>
      </div>
    </div>
  )
}

export function TourOverlay() {
  const { active, currentStep, currentIndex, total, next, dismiss } = useTour()
  const rect = useTargetRect(active && currentStep ? currentStep.id : undefined)
  const positionRef = useRef(popoverPosition(rect))
  positionRef.current = popoverPosition(rect)

  // ESC to dismiss
  useEffect(() => {
    if (!active) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") dismiss()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [active, dismiss])

  if (!active || !currentStep) return null
  return createPortal(
    <div className="fixed inset-0 z-[55]">
      <Spotlight rect={rect} />
      <Popover
        step={currentStep}
        index={currentIndex}
        total={total}
        position={positionRef.current}
        onNext={next}
        onSkip={dismiss}
      />
    </div>,
    document.body,
  )
}
