// SPDX-License-Identifier: Apache-2.0
//
// Per-persona detail drawer (#170). Side drawer pattern matching the
// existing review/api-keys flow — slides in from the right, click
// outside or Esc to close.
//
// Sections (top to bottom):
//   - Identity (AAISN-scoped name, joined, last seen sparkline)
//   - Activity timeline (filterable, paginated)
//   - KU contributions (totals, per-domain breakdown, confidence histo)
//   - Sessions / API keys (V1: backend gap surfaced inline)
//   - Lifecycle events (V1: backend gap — derived from activity)

import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "../api"
import type {
  ActivityRow,
  PersonaStatus,
  PersonaSummary,
  ReviewItem,
} from "../types"
import { timeAgo } from "../utils"

const TIMELINE_PAGE_LIMIT = 50
const SPARKLINE_DAYS = 30

// [label, color-token] tuples. Combined to keep the bundle slim — one
// table instead of two parallel objects. Fallbacks: raw event_type +
// neutral ink-mute when an unknown event_type appears.
const EVENT_META: Record<string, [string, string]> = {
  query: ["Queried", "var(--ink-mute)"],
  propose: ["Proposed", "var(--cyan)"],
  confirm: ["Confirmed", "var(--emerald)"],
  flag: ["Flagged", "var(--rose)"],
  review_start: ["Review started", "var(--gold)"],
  review_resolve: ["Review resolved", "var(--emerald)"],
  crosstalk_send: ["Crosstalk sent", "var(--violet)"],
  crosstalk_reply: ["Crosstalk reply", "var(--violet)"],
  crosstalk_close: ["Crosstalk closed", "var(--ink-mute)"],
  consult_open: ["Consult opened", "var(--cyan)"],
  consult_reply: ["Consult reply", "var(--cyan)"],
  consult_close: ["Consult closed", "var(--ink-mute)"],
}

function statusBadgeClasses(status: PersonaStatus): string {
  switch (status) {
    case "active":
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
    case "idle":
      return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_28%,transparent)]"
    case "departed":
      return "bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]"
    case "suspended":
      return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
  }
}

// Render a 30-day cyan sparkline from the persona's activity rows.
// Each bar = 1px wide, height = log-scaled count for that day.
function ActivitySparkline({ rows }: { rows: ActivityRow[] }) {
  const buckets = useMemo(() => {
    const now = Date.now()
    const day = 86_400_000
    const arr = new Array<number>(SPARKLINE_DAYS).fill(0)
    for (const row of rows) {
      const ageDays = Math.floor((now - new Date(row.ts).getTime()) / day)
      if (ageDays < 0 || ageDays >= SPARKLINE_DAYS) continue
      const idx = SPARKLINE_DAYS - 1 - ageDays
      arr[idx] += 1
    }
    return arr
  }, [rows])

  const max = Math.max(1, ...buckets)
  const todayCount = buckets[SPARKLINE_DAYS - 1]
  const hasActivity = buckets.some((c) => c > 0)

  return (
    <div className="flex items-end gap-px h-10" aria-hidden="true">
      {buckets.map((count, i) => {
        const isToday = i === SPARKLINE_DAYS - 1
        const heightPct =
          count === 0 ? 4 : Math.max(8, Math.round((count / max) * 100))
        return (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: positional sparkline bar
            key={i}
            className={`w-px ${isToday && todayCount > 0 ? "bg-[var(--gold)]" : count > 0 ? "bg-[var(--cyan)]" : "bg-[var(--rule-strong)]"}`}
            style={{ height: `${heightPct}%` }}
          />
        )
      })}
      <span className="ml-2 self-center font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]">
        {hasActivity ? `${SPARKLINE_DAYS}d` : "no activity"}
      </span>
    </div>
  )
}

function ConfidenceHistogram({ units }: { units: ReviewItem[] }) {
  const buckets = useMemo(() => {
    const b = { low: 0, mid: 0, high: 0, top: 0 }
    for (const u of units) {
      const c = u.knowledge_unit.evidence.confidence
      if (c < 0.3) b.low += 1
      else if (c < 0.6) b.mid += 1
      else if (c < 0.8) b.high += 1
      else b.top += 1
    }
    return b
  }, [units])

  const total = units.length
  if (total === 0) {
    return (
      <p className="text-sm text-[var(--ink-mute)]">No KUs to histogram yet.</p>
    )
  }

  const rows = [
    {
      label: "0.0–0.3",
      count: buckets.low,
      bar: "bg-[color-mix(in_srgb,var(--rose)_55%,transparent)]",
    },
    {
      label: "0.3–0.6",
      count: buckets.mid,
      bar: "bg-[color-mix(in_srgb,var(--gold)_55%,transparent)]",
    },
    {
      label: "0.6–0.8",
      count: buckets.high,
      bar: "bg-[color-mix(in_srgb,var(--emerald)_45%,transparent)]",
    },
    {
      label: "0.8–1.0",
      count: buckets.top,
      bar: "bg-[var(--emerald)]",
    },
  ]
  const max = Math.max(1, ...rows.map((r) => r.count))

  return (
    <div className="space-y-1.5">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-3">
          <span className="w-16 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]">
            {r.label}
          </span>
          <div className="flex-1 h-2 rounded-full bg-[var(--surface-hover)] overflow-hidden">
            <div
              className={`h-full ${r.bar}`}
              style={{ width: `${(r.count / max) * 100}%` }}
            />
          </div>
          <span className="w-8 text-right font-mono-brand tabular-nums text-[11px] text-[var(--ink-dim)]">
            {r.count}
          </span>
        </div>
      ))}
    </div>
  )
}

interface DomainBreakdownProps {
  units: ReviewItem[]
}

function DomainBreakdown({ units }: DomainBreakdownProps) {
  const counts = useMemo(() => {
    const map = new Map<string, number>()
    for (const u of units) {
      for (const d of u.knowledge_unit.domains) {
        map.set(d, (map.get(d) ?? 0) + 1)
      }
    }
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1])
  }, [units])

  if (counts.length === 0) {
    return (
      <p className="text-sm text-[var(--ink-mute)]">
        No domains tagged on this persona's KUs.
      </p>
    )
  }

  return (
    <table className="w-full text-sm">
      <thead className="border-b border-[var(--rule)]">
        <tr>
          <th
            scope="col"
            className="text-left py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
          >
            Domain
          </th>
          <th
            scope="col"
            className="text-right py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
          >
            KUs
          </th>
        </tr>
      </thead>
      <tbody>
        {counts.map(([domain, count]) => (
          <tr key={domain} className="border-t border-[var(--rule)]">
            <td className="py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--violet)]">
              {domain}
            </td>
            <td className="py-1.5 text-right font-mono-brand tabular-nums text-[var(--ink-dim)]">
              {count}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

interface ActivityTimelineProps {
  persona: string
}

function ActivityTimeline({ persona }: ActivityTimelineProps) {
  const [rows, setRows] = useState<ActivityRow[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(true)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<string>("all")

  // Initial-load effect: reset state and pull the first page whenever
  // the persona changes. Inlined fetch (rather than a useCallback)
  // avoids the depend-on-cursor / re-trigger-on-cursor-change loop.
  useEffect(() => {
    let cancelled = false
    setRows([])
    setCursor(null)
    setHasMore(true)
    setLoading(true)
    api
      .listActivity({ persona, limit: TIMELINE_PAGE_LIMIT })
      .then((resp) => {
        if (cancelled) return
        setRows(resp.items)
        setCursor(resp.next_cursor)
        setHasMore(resp.next_cursor !== null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [persona])

  // "Load more" — uses the latest cursor closure. Distinct from the
  // initial-load effect so that effect doesn't loop on cursor changes.
  const loadMore = useCallback(async () => {
    if (cursor === null) return
    setLoading(true)
    try {
      const resp = await api.listActivity({
        persona,
        limit: TIMELINE_PAGE_LIMIT,
        cursor,
      })
      setRows((prev) => [...prev, ...resp.items])
      setCursor(resp.next_cursor)
      setHasMore(resp.next_cursor !== null)
    } finally {
      setLoading(false)
    }
  }, [persona, cursor])

  const filtered = useMemo(() => {
    if (filter === "all") return rows
    return rows.filter((r) => r.event_type === filter)
  }, [rows, filter])

  const eventTypesPresent = useMemo(() => {
    const set = new Set(rows.map((r) => r.event_type))
    return Array.from(set).sort()
  }, [rows])

  return (
    <div className="space-y-3">
      {eventTypesPresent.length > 1 && (
        <div className="flex flex-wrap gap-1.5">
          <button
            type="button"
            onClick={() => setFilter("all")}
            className={`rounded-full px-2.5 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.14em] border transition-colors ${
              filter === "all"
                ? "bg-[color-mix(in_srgb,var(--cyan)_22%,transparent)] text-[var(--cyan)] border-[color-mix(in_srgb,var(--cyan)_45%,transparent)]"
                : "text-[var(--ink-dim)] border-[var(--rule-strong)] hover:bg-[var(--surface-hover)]"
            }`}
          >
            All
          </button>
          {eventTypesPresent.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setFilter(t)}
              className={`rounded-full px-2.5 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.14em] border transition-colors ${
                filter === t
                  ? "bg-[color-mix(in_srgb,var(--cyan)_22%,transparent)] text-[var(--cyan)] border-[color-mix(in_srgb,var(--cyan)_45%,transparent)]"
                  : "text-[var(--ink-dim)] border-[var(--rule-strong)] hover:bg-[var(--surface-hover)]"
              }`}
            >
              {EVENT_META[t]?.[0] ?? t}
            </button>
          ))}
        </div>
      )}

      {filtered.length === 0 && !loading ? (
        <p className="text-sm text-[var(--ink-mute)]">
          No activity events recorded for this persona.
        </p>
      ) : (
        <ol className="space-y-2 border-l border-[var(--rule-strong)] pl-4">
          {filtered.map((row) => {
            const meta = EVENT_META[row.event_type]
            const label = meta?.[0] ?? row.event_type
            const color = meta?.[1] ?? "var(--ink-mute)"
            return (
              <li key={row.id} className="relative">
                <span
                  aria-hidden="true"
                  className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full"
                  style={{ background: color }}
                />
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span
                    className="font-mono-brand text-[10px] uppercase tracking-[0.16em]"
                    style={{ color }}
                  >
                    {label}
                  </span>
                  <span
                    className="font-mono-brand text-[10px] text-[var(--ink-mute)]"
                    title={new Date(row.ts).toLocaleString()}
                  >
                    {timeAgo(row.ts)}
                  </span>
                  {row.thread_or_chain_id && (
                    <code className="rounded bg-[var(--surface-hover)] border border-[var(--rule)] px-1.5 py-0.5 font-mono-brand text-[10px] text-[var(--ink-mute)]">
                      {row.thread_or_chain_id.slice(0, 12)}…
                    </code>
                  )}
                </div>
                {row.payload && Object.keys(row.payload).length > 0 && (
                  <p className="mt-0.5 text-xs text-[var(--ink-dim)] truncate">
                    {summarisePayload(row.payload)}
                  </p>
                )}
              </li>
            )
          })}
        </ol>
      )}

      {hasMore && (
        <button
          type="button"
          onClick={() => loadMore()}
          disabled={loading}
          className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"
        >
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  )
}

function summarisePayload(payload: Record<string, unknown>): string {
  // Cheap human-readable rendering of the payload blob — keeps the
  // timeline readable without a per-event-type renderer matrix.
  const interesting = ["unit_id", "domain", "topic", "subject", "thread_id"]
  for (const key of interesting) {
    const v = payload[key]
    if (typeof v === "string" && v) return `${key}: ${v}`
  }
  return Object.keys(payload).slice(0, 3).join(", ")
}

interface Props {
  persona: PersonaSummary
  units: ReviewItem[]
  onClose: () => void
}

export function PersonaDetailDrawer({ persona, units, onClose }: Props) {
  const [activityForSparkline, setActivityForSparkline] = useState<
    ActivityRow[]
  >([])

  useEffect(() => {
    let cancelled = false
    api
      .listActivity({ persona: persona.name, limit: 500 })
      .then((resp) => {
        if (!cancelled) setActivityForSparkline(resp.items)
      })
      .catch(() => {
        // Sparkline is decorative; failure is non-fatal.
      })
    return () => {
      cancelled = true
    }
  }, [persona.name])

  // Esc to close, mirrors the modal pattern in ApiKeysPage.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  // Lifecycle events — V1 derives "joined" / "last_seen" from activity
  // alone since lifecycle_events isn't surfaced through the read API
  // yet. Keeps the section visible (operator expectation) but
  // truthful about the limitation.
  const lifecycleEvents = useMemo(() => {
    const events: Array<{ kind: string; ts: string; detail: string }> = []
    if (persona.joined) {
      events.push({
        kind: "joined",
        ts: persona.joined,
        detail: "First activity event recorded",
      })
    }
    if (persona.status === "departed" && persona.last_seen) {
      events.push({
        kind: "departed",
        ts: persona.last_seen,
        detail: `No activity for ${Math.floor(
          (Date.now() - new Date(persona.last_seen).getTime()) / 86_400_000,
        )}d (derived)`,
      })
    }
    return events
  }, [persona])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="persona-detail-heading"
      className="fixed inset-0 z-30 flex justify-end"
    >
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <button
        type="button"
        aria-label="Close persona detail"
        className="flex-1 bg-black/65 backdrop-blur-sm"
        onClick={onClose}
      />
      <aside className="w-full max-w-xl overflow-y-auto bg-[var(--bg-via)] border-l border-[var(--rule-strong)] p-6 shadow-[0_0_80px_rgba(0,0,0,0.7)]">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="eyebrow">Persona</p>
            <h2
              id="persona-detail-heading"
              className="font-display text-2xl text-[var(--ink)] mt-1 break-all"
            >
              {persona.name}
            </h2>
            <p className="mt-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {persona.enterprise || "—"}
              {persona.group ? ` / ${persona.group}` : ""} / {persona.name}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            Close
          </button>
        </div>

        {/* Identity panel. */}
        <section className="mt-6 brand-surface-raised p-5 space-y-4">
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="eyebrow">Status</p>
              <span
                className={`mt-1 inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                  persona.status,
                )}`}
              >
                {persona.status}
              </span>
            </div>
            <div>
              <p className="eyebrow">Joined</p>
              <p
                className="mt-1 text-[var(--ink-dim)]"
                title={
                  persona.joined
                    ? new Date(persona.joined).toLocaleString()
                    : ""
                }
              >
                {persona.joined ? timeAgo(persona.joined) : "—"}
              </p>
            </div>
            <div>
              <p className="eyebrow">Last seen</p>
              <p
                className="mt-1 text-[var(--ink-dim)]"
                title={
                  persona.last_seen
                    ? new Date(persona.last_seen).toLocaleString()
                    : ""
                }
              >
                {persona.last_seen ? timeAgo(persona.last_seen) : "never"}
              </p>
            </div>
            <div>
              <p className="eyebrow">KUs authored</p>
              <p className="mt-1 font-mono-brand tabular-nums text-[var(--ink)]">
                {units.length}
              </p>
            </div>
          </div>
          <div>
            <p className="eyebrow mb-1.5">Activity (last 30d)</p>
            <ActivitySparkline rows={activityForSparkline} />
          </div>
        </section>

        {/* Activity timeline. */}
        <section className="mt-6">
          <p className="eyebrow">Timeline</p>
          <h3 className="font-display text-lg text-[var(--ink)] mt-1">
            Activity
          </h3>
          <div className="mt-3">
            <ActivityTimeline persona={persona.name} />
          </div>
        </section>

        {/* KU contributions. */}
        <section className="mt-8">
          <p className="eyebrow">Knowledge</p>
          <h3 className="font-display text-lg text-[var(--ink)] mt-1">
            KU contributions
          </h3>
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <div className="brand-surface p-4">
              <p className="eyebrow">Total</p>
              <p className="font-display font-light text-3xl text-[var(--cyan)] tabular-nums mt-1">
                {units.length}
              </p>
            </div>
            <div className="brand-surface p-4">
              <p className="eyebrow">Tier split</p>
              <div className="mt-1 grid grid-cols-2 gap-2 text-sm">
                <div>
                  <p className="font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]">
                    Promoted
                  </p>
                  <p className="font-mono-brand tabular-nums text-[var(--emerald)]">
                    {
                      units.filter((u) => u.knowledge_unit.tier !== "local")
                        .length
                    }
                  </p>
                </div>
                <div>
                  <p className="font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]">
                    Local-only
                  </p>
                  <p className="font-mono-brand tabular-nums text-[var(--ink-dim)]">
                    {
                      units.filter((u) => u.knowledge_unit.tier === "local")
                        .length
                    }
                  </p>
                </div>
              </div>
            </div>
          </div>
          <div className="mt-4 brand-surface p-4">
            <p className="eyebrow">Confidence distribution</p>
            <div className="mt-2">
              <ConfidenceHistogram units={units} />
            </div>
          </div>
          <div className="mt-4 brand-surface p-4">
            <p className="eyebrow">Domain breakdown</p>
            <div className="mt-2">
              <DomainBreakdown units={units} />
            </div>
          </div>
          {units.length > 0 && (
            <div className="mt-4 brand-surface p-4">
              <p className="eyebrow">Top by confirmations</p>
              <ul className="mt-2 space-y-1.5 text-sm">
                {[...units]
                  .sort(
                    (a, b) =>
                      b.knowledge_unit.evidence.confirmations -
                      a.knowledge_unit.evidence.confirmations,
                  )
                  .slice(0, 5)
                  .map((u) => (
                    <li
                      key={u.knowledge_unit.id}
                      className="flex items-baseline justify-between gap-3"
                    >
                      <span className="truncate text-[var(--ink-dim)]">
                        {u.knowledge_unit.insight.summary}
                      </span>
                      <span className="shrink-0 font-mono-brand tabular-nums text-[10px] uppercase tracking-[0.16em] text-[var(--emerald)]">
                        {u.knowledge_unit.evidence.confirmations}×
                      </span>
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </section>

        {/* Sessions / API keys. */}
        <section className="mt-8">
          <p className="eyebrow">Access</p>
          <h3 className="font-display text-lg text-[var(--ink)] mt-1">
            Sessions &amp; API keys
          </h3>
          <div className="mt-3 brand-surface p-4">
            <p className="text-sm text-[var(--ink-dim)]">
              Per-persona key enumeration requires the admin
              <code className="mx-1 font-mono-brand text-[var(--cyan)]">
                /admin/personas/&lt;name&gt;/api-keys
              </code>
              endpoint, which is not yet exposed. Tracked as a follow-up backend
              issue.
            </p>
          </div>
        </section>

        {/* Lifecycle. */}
        <section className="mt-8">
          <p className="eyebrow">Lifecycle</p>
          <h3 className="font-display text-lg text-[var(--ink)] mt-1">
            Events
          </h3>
          {lifecycleEvents.length === 0 ? (
            <p className="mt-3 text-sm text-[var(--ink-mute)]">
              No lifecycle events derived yet.
            </p>
          ) : (
            <ol className="mt-3 space-y-2 border-l border-[var(--rule-strong)] pl-4">
              {lifecycleEvents.map((e) => (
                <li key={`${e.kind}-${e.ts}`} className="relative">
                  <span
                    aria-hidden="true"
                    className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-[var(--violet)]"
                  />
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--violet)]">
                      {e.kind}
                    </span>
                    <span
                      className="font-mono-brand text-[10px] text-[var(--ink-mute)]"
                      title={new Date(e.ts).toLocaleString()}
                    >
                      {timeAgo(e.ts)}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-[var(--ink-dim)]">
                    {e.detail}
                  </p>
                </li>
              ))}
            </ol>
          )}
        </section>
      </aside>
    </div>
  )
}
