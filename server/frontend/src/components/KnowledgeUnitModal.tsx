import { useEffect, useRef, useState } from "react"
import { ApiError, api } from "../api"
import type { ReviewItem } from "../types"
import { timeAgo } from "../utils"
import { DomainTags } from "./DomainTags"
import { StatusBadge } from "./StatusBadge"

interface Props {
  unitId: string
  onClose: () => void
}

function confidenceColor(c: number): string {
  if (c < 0.3) return "text-[var(--rose)]"
  if (c < 0.5) return "text-[var(--gold)]"
  if (c < 0.7) return "text-[var(--gold)]"
  return "text-[var(--emerald)]"
}

const MODAL_TITLE_ID = "ku-modal-title"

export function KnowledgeUnitModal({ unitId, onClose }: Props) {
  const [item, setItem] = useState<ReviewItem | null>(null)
  const [error, setError] = useState<string | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let ignore = false
    api
      .getUnit(unitId)
      .then((data) => {
        if (!ignore) setItem(data)
      })
      .catch((err) => {
        if (ignore) return
        if (err instanceof ApiError && err.status === 404) {
          setError("Knowledge unit not found.")
        } else {
          setError("Failed to load knowledge unit.")
        }
      })
    return () => {
      ignore = true
    }
  }, [unitId])

  useEffect(() => {
    dialogRef.current?.focus()
  }, [])

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", handleKey)
    return () => window.removeEventListener("keydown", handleKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <button
        type="button"
        tabIndex={-1}
        aria-hidden="true"
        onClick={onClose}
        className="absolute inset-0 bg-black/65 backdrop-blur-sm cursor-default"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={item ? MODAL_TITLE_ID : undefined}
        tabIndex={-1}
        className="relative brand-surface-raised w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto outline-none shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]"
      >
        {error && (
          <div className="p-6 text-center">
            <p className="text-[var(--rose)] text-sm">{error}</p>
            <button
              type="button"
              onClick={onClose}
              className="mt-3 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--ink-mute)] hover:text-[var(--ink)] transition-colors"
            >
              Close
            </button>
          </div>
        )}

        {!item && !error && (
          <div className="p-6 space-y-3">
            <div className="h-3 w-32 animate-pulse bg-[var(--rule-strong)] rounded" />
            <div className="h-6 w-48 animate-pulse bg-[var(--rule-strong)] rounded" />
            <div className="h-16 w-full animate-pulse bg-[var(--rule-strong)] rounded" />
          </div>
        )}

        {item && (
          <div className="p-6 space-y-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="eyebrow mb-1.5">Knowledge unit</p>
                <h2
                  id={MODAL_TITLE_ID}
                  className="font-display text-xl text-[var(--ink)] leading-snug"
                >
                  {item.knowledge_unit.insight.summary}
                </h2>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="text-[var(--ink-mute)] hover:text-[var(--ink)] text-xl leading-none shrink-0 transition-colors"
                aria-label="Close"
              >
                ×
              </button>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <StatusBadge status={item.status} />
              {item.reviewed_by && (
                <span className="font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]">
                  by {item.reviewed_by}
                </span>
              )}
              {item.reviewed_at && (
                <span className="font-mono-brand text-[10px] text-[var(--ink-faint)]">
                  {timeAgo(item.reviewed_at)}
                </span>
              )}
            </div>

            <DomainTags domains={item.knowledge_unit.domains} />

            <p className="text-[var(--ink-dim)] leading-relaxed">
              {item.knowledge_unit.insight.detail}
            </p>

            <div className="border-l-2 rounded-r-lg px-4 py-3 bg-[color-mix(in_srgb,var(--cyan)_8%,transparent)] border-[var(--cyan)]">
              <span className="eyebrow text-[var(--cyan)]">Action</span>
              <p className="text-[var(--ink)] text-sm mt-1.5 leading-relaxed">
                {item.knowledge_unit.insight.action}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-lg p-3 bg-[var(--surface)] border border-[var(--rule)]">
                <span className="eyebrow">Confidence</span>
                <p
                  className={`font-display font-light text-2xl mt-1 ${confidenceColor(item.knowledge_unit.evidence.confidence)} tabular-nums`}
                >
                  {item.knowledge_unit.evidence.confidence.toFixed(2)}
                </p>
              </div>
              <div className="rounded-lg p-3 bg-[var(--surface)] border border-[var(--rule)]">
                <span className="eyebrow">Confirmations</span>
                <p className="font-display font-light text-2xl mt-1 text-[var(--ink)] tabular-nums">
                  {item.knowledge_unit.evidence.confirmations}
                </p>
              </div>
            </div>

            {(item.knowledge_unit.context.languages.length > 0 ||
              item.knowledge_unit.context.frameworks.length > 0) && (
              <div className="text-sm text-[var(--ink-mute)]">
                {item.knowledge_unit.context.languages.length > 0 && (
                  <span>
                    <span className="eyebrow mr-1">Languages</span>
                    {item.knowledge_unit.context.languages.join(", ")}
                  </span>
                )}
                {item.knowledge_unit.context.languages.length > 0 &&
                  item.knowledge_unit.context.frameworks.length > 0 && (
                    <span className="mx-2 text-[var(--ink-faint)]">·</span>
                  )}
                {item.knowledge_unit.context.frameworks.length > 0 && (
                  <span>
                    <span className="eyebrow mr-1">Frameworks</span>
                    {item.knowledge_unit.context.frameworks.join(", ")}
                  </span>
                )}
              </div>
            )}

            <div className="flex items-center justify-between font-mono-brand text-[10px] text-[var(--ink-faint)] pt-3 border-t border-[var(--rule)]">
              <span className="truncate">{item.knowledge_unit.id}</span>
              {item.knowledge_unit.evidence.first_observed && (
                <span>
                  {timeAgo(item.knowledge_unit.evidence.first_observed)}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
