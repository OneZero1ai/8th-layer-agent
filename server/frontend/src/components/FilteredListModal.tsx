import { useEffect, useRef, useState } from "react"
import { ApiError, api } from "../api"
import type { ReviewItem } from "../types"
import { DomainTags } from "./DomainTags"
import { StatusBadge } from "./StatusBadge"

export interface ListFilter {
  title: string
  domain?: string
  confidence_min?: number
  confidence_max?: number
  status?: string
}

interface Props {
  filter: ListFilter
  onClose: () => void
  onSelectUnit: (unitId: string) => void
}

function confidenceLabel(c: number): string {
  return c.toFixed(2)
}

const MODAL_TITLE_ID = "filtered-list-title"

export function FilteredListModal({ filter, onClose, onSelectUnit }: Props) {
  const [items, setItems] = useState<ReviewItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let ignore = false
    api
      .listUnits({
        domain: filter.domain,
        confidence_min: filter.confidence_min,
        confidence_max: filter.confidence_max,
        status: filter.status,
      })
      .then((data) => {
        if (!ignore) setItems(data)
      })
      .catch((err) => {
        if (ignore) return
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError("Failed to load knowledge units.")
        }
      })
    return () => {
      ignore = true
    }
  }, [
    filter.domain,
    filter.confidence_min,
    filter.confidence_max,
    filter.status,
  ])

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
        aria-labelledby={MODAL_TITLE_ID}
        tabIndex={-1}
        className="relative brand-surface-raised w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col outline-none shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]"
      >
        <div className="flex items-center justify-between p-5 border-b border-[var(--rule)]">
          <div>
            <p className="eyebrow">Filtered list</p>
            <h2
              id={MODAL_TITLE_ID}
              className="font-display text-xl text-[var(--ink)] mt-0.5"
            >
              {filter.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-[var(--ink-mute)] hover:text-[var(--ink)] text-xl leading-none transition-colors"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {error && (
            <p className="text-[var(--rose)] text-sm text-center py-4">
              {error}
            </p>
          )}

          {!items && !error && (
            <div className="space-y-3">
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-16 animate-pulse bg-[var(--surface-hover)] rounded-lg"
                />
              ))}
            </div>
          )}

          {items && items.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <span
                aria-hidden="true"
                className="font-display text-3xl text-[var(--ink-faint)]"
              >
                ∅
              </span>
              <span className="eyebrow text-[var(--cyan)]">
                No knowledge units found
              </span>
            </div>
          )}

          {items && items.length > 0 && (
            <div className="space-y-2">
              {items.map((item) => (
                <button
                  type="button"
                  key={item.knowledge_unit.id}
                  className="w-full text-left p-3 rounded-lg border border-[var(--rule)] bg-[var(--surface)] hover:border-[var(--cyan)] hover:bg-[var(--surface-hover)] transition-colors"
                  onClick={() => onSelectUnit(item.knowledge_unit.id)}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <StatusBadge status={item.status} />
                    <span className="text-sm font-medium text-[var(--ink)] truncate">
                      {item.knowledge_unit.insight.summary}
                    </span>
                  </div>
                  <div className="flex items-center gap-3">
                    <DomainTags domains={item.knowledge_unit.domains} />
                    <span className="font-mono-brand text-[11px] text-[var(--ink-faint)] ml-auto shrink-0 tabular-nums">
                      {confidenceLabel(item.knowledge_unit.evidence.confidence)}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
