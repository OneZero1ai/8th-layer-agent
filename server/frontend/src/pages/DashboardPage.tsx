import { useCallback, useEffect, useState } from "react"
import { Link, useOutletContext } from "react-router"
import {
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { api } from "../api"
import {
  FilteredListModal,
  type ListFilter,
} from "../components/FilteredListModal"
import { KnowledgeUnitModal } from "../components/KnowledgeUnitModal"
import { StatusBadge } from "../components/StatusBadge"
import type { ReviewStatsResponse } from "../types"
import { timeAgo } from "../utils"

const CONFIDENCE_COLORS: Record<string, string> = {
  "0.0-0.3": "bg-[color-mix(in_srgb,var(--rose)_55%,transparent)]",
  "0.3-0.6": "bg-[color-mix(in_srgb,var(--gold)_55%,transparent)]",
  "0.6-0.8": "bg-[color-mix(in_srgb,var(--emerald)_45%,transparent)]",
  "0.8-1.0": "bg-[var(--emerald)]",
}

// Brand-tuned recharts palette: cyan / emerald / rose to mirror the apex SPA.
const CHART_COLORS = {
  proposed: "#5bd0ff", // var(--cyan)
  approved: "#10b981", // var(--emerald)
  rejected: "#ff5c7c", // var(--rose)
  axis: "rgba(230,230,230,0.42)",
  grid: "rgba(255,255,255,0.06)",
}

function EmptyGlyph({ label }: { label: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-10 gap-3">
      <span
        aria-hidden="true"
        className="font-display text-3xl text-[var(--ink-faint)]"
      >
        ∅
      </span>
      <span className="eyebrow text-[var(--brand-primary)]">{label}</span>
    </div>
  )
}

export function DashboardPage() {
  const { setPendingCount } = useOutletContext<{
    setPendingCount: (n: number) => void
  }>()
  const [stats, setStats] = useState<ReviewStatsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null)
  const [listFilter, setListFilter] = useState<ListFilter | null>(null)
  const closeModal = useCallback(() => setSelectedUnitId(null), [])
  const closeListModal = useCallback(() => setListFilter(null), [])

  useEffect(() => {
    function fetchStats() {
      api
        .reviewStats()
        .then((s) => {
          setStats(s)
          setPendingCount(s.counts.pending)
          setError(null)
        })
        .catch(() => setError("Failed to load dashboard. Retrying..."))
    }
    fetchStats()
    const interval = setInterval(fetchStats, 15_000)
    return () => clearInterval(interval)
  }, [setPendingCount])

  const trendData = stats?.trends.daily ?? []

  if (!stats && !error) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="brand-surface p-4">
              <div className="h-3 w-16 animate-pulse bg-[var(--rule-strong)] rounded mb-2" />
              <div className="h-8 w-12 animate-pulse bg-[var(--rule-strong)] rounded" />
            </div>
          ))}
        </div>
        {[1, 2].map((i) => (
          <div key={i} className="brand-surface h-40 animate-pulse" />
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4 text-center">
          <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
            {error}
          </p>
        </div>
      )}

      {stats && (
        <>
          {/* Count tiles. */}
          <div className="grid grid-cols-3 gap-4">
            <Link
              to="/review"
              className="brand-surface-raised p-5 text-center hover:border-[var(--gold)] transition-colors group"
            >
              <p className="font-display font-light text-4xl text-[var(--gold)] tabular-nums">
                {stats.counts.pending}
              </p>
              <p className="eyebrow mt-2 group-hover:text-[var(--gold)] transition-colors">
                Pending
              </p>
            </Link>
            <div className="brand-surface-raised p-5 text-center">
              <p className="font-display font-light text-4xl text-[var(--emerald)] tabular-nums">
                {stats.counts.approved}
              </p>
              <p className="eyebrow mt-2">Approved</p>
            </div>
            <button
              type="button"
              className="brand-surface-raised p-5 text-center hover:border-[var(--rose)] transition-colors group w-full"
              onClick={() =>
                setListFilter({ title: "Rejected", status: "rejected" })
              }
            >
              <p className="font-display font-light text-4xl text-[var(--rose)] tabular-nums">
                {stats.counts.rejected}
              </p>
              <p className="eyebrow mt-2 group-hover:text-[var(--rose)] transition-colors">
                Rejected
              </p>
            </button>
          </div>

          {/* Domains + confidence. */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="brand-surface-raised p-5">
              <h3 className="eyebrow mb-4">Domains</h3>
              <div className="space-y-3 max-h-48 overflow-y-auto">
                {Object.entries(stats.domains).length === 0 ? (
                  <EmptyGlyph label="No domains yet" />
                ) : (
                  Object.entries(stats.domains)
                    .sort(([, a], [, b]) => b - a)
                    .map(([domain, count]) => {
                      const maxCount = Math.max(...Object.values(stats.domains))
                      return (
                        <button
                          type="button"
                          key={domain}
                          className="flex items-center gap-3 w-full text-left rounded hover:bg-[var(--surface-hover)] transition-colors -mx-1 px-1 py-1"
                          onClick={() =>
                            setListFilter({
                              title: `Domain: ${domain}`,
                              domain,
                              status: "approved",
                            })
                          }
                        >
                          <span className="text-sm text-[var(--ink-dim)] w-24 truncate">
                            {domain}
                          </span>
                          <div className="flex-1 h-1 bg-[var(--rule)] rounded-full overflow-hidden">
                            <div
                              className="h-full bg-gradient-to-r from-[var(--brand-secondary)] to-[var(--brand-primary)] rounded-full"
                              style={{ width: `${(count / maxCount) * 100}%` }}
                            />
                          </div>
                          <span className="font-mono-brand text-[11px] text-[var(--ink-mute)] w-6 text-right tabular-nums">
                            {count}
                          </span>
                        </button>
                      )
                    })
                )}
              </div>
            </div>

            <div className="brand-surface-raised p-5">
              <h3 className="eyebrow mb-4">Confidence</h3>
              {(() => {
                const maxCount = Math.max(
                  ...Object.values(stats.confidence_distribution),
                  1,
                )
                const totalConfidence = Object.values(
                  stats.confidence_distribution,
                ).reduce((a, b) => a + b, 0)
                if (totalConfidence === 0) {
                  return <EmptyGlyph label="No data yet" />
                }
                return (
                  <div className="flex gap-2">
                    {Object.entries(stats.confidence_distribution).map(
                      ([bucket, count]) => {
                        const [minStr, maxStr] = bucket.split("-")
                        const max = parseFloat(maxStr)
                        return (
                          <button
                            type="button"
                            key={bucket}
                            className="flex-1 flex flex-col items-center gap-1 rounded hover:bg-[var(--surface-hover)] transition-colors cursor-pointer disabled:cursor-default"
                            disabled={count === 0}
                            onClick={() =>
                              setListFilter({
                                title: `Confidence: ${bucket}`,
                                confidence_min: parseFloat(minStr),
                                confidence_max: max >= 1.0 ? undefined : max,
                                status: "approved",
                              })
                            }
                          >
                            <span className="font-mono-brand text-[11px] text-[var(--ink-dim)] tabular-nums">
                              {count}
                            </span>
                            <div className="w-full h-24 flex items-end">
                              <div
                                className={`w-full rounded-t ${CONFIDENCE_COLORS[bucket] ?? "bg-[var(--rule-strong)]"}`}
                                style={{
                                  height:
                                    maxCount > 0
                                      ? `${(count / maxCount) * 100}%`
                                      : "0",
                                  minHeight: count > 0 ? "8px" : "0",
                                }}
                              />
                            </div>
                            <span className="font-mono-brand text-[10px] text-[var(--ink-faint)] truncate w-full text-center tabular-nums">
                              {bucket}
                            </span>
                          </button>
                        )
                      },
                    )}
                  </div>
                )
              })()}
            </div>
          </div>

          {/* Submissions trend. */}
          <div className="brand-surface-raised p-5">
            <h3 className="eyebrow mb-4">Submissions</h3>
            {trendData.length === 0 ? (
              <EmptyGlyph label="No submission data yet" />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={trendData}>
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 10, fill: CHART_COLORS.axis }}
                    stroke={CHART_COLORS.grid}
                  />
                  <YAxis
                    tick={{ fontSize: 10, fill: CHART_COLORS.axis }}
                    stroke={CHART_COLORS.grid}
                    allowDecimals={false}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "rgba(7,7,11,0.95)",
                      border: "1px solid rgba(255,255,255,0.14)",
                      borderRadius: "8px",
                      fontSize: 12,
                      color: "#e6e6e6",
                    }}
                    labelStyle={{ color: "#e6e6e6" }}
                  />
                  <Legend
                    wrapperStyle={{
                      fontSize: 11,
                      color: CHART_COLORS.axis,
                      paddingTop: "8px",
                    }}
                  />
                  <Line
                    type="monotone"
                    dataKey="proposed"
                    stroke={CHART_COLORS.proposed}
                    strokeWidth={2}
                    dot={trendData.length <= 7}
                    name="Submitted"
                  />
                  <Line
                    type="monotone"
                    dataKey="approved"
                    stroke={CHART_COLORS.approved}
                    strokeWidth={2}
                    dot={trendData.length <= 7}
                    name="Approved"
                  />
                  <Line
                    type="monotone"
                    dataKey="rejected"
                    stroke={CHART_COLORS.rejected}
                    strokeWidth={2}
                    dot={trendData.length <= 7}
                    name="Rejected"
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* Recent activity. */}
          <div className="brand-surface-raised p-5">
            <h3 className="eyebrow mb-4">Recent Activity</h3>
            <div className="max-h-72 overflow-y-auto">
              {stats.recent_activity.length === 0 ? (
                <EmptyGlyph label="No activity yet" />
              ) : (
                <ul>
                  <li className="grid grid-cols-[5rem_1fr_5rem_4rem] gap-3 border-b border-[var(--rule)] py-1.5 eyebrow">
                    <span>Status</span>
                    <span>Summary</span>
                    <span className="text-right">Reviewer</span>
                    <span className="text-right">Time</span>
                  </li>
                  {stats.recent_activity.map((event) => (
                    <li key={event.unit_id}>
                      <button
                        type="button"
                        onClick={() => setSelectedUnitId(event.unit_id)}
                        className="grid grid-cols-[5rem_1fr_5rem_4rem] gap-3 items-center w-full text-left border-b border-[var(--rule)] last:border-0 cursor-pointer hover:bg-[var(--surface-hover)] transition-colors py-2.5"
                      >
                        <span>
                          <StatusBadge status={event.type} />
                        </span>
                        <span className="text-sm text-[var(--ink-dim)] truncate min-w-0">
                          {event.summary}
                        </span>
                        <span className="font-mono-brand text-[10px] uppercase tracking-[0.14em] text-[var(--ink-mute)] whitespace-nowrap text-right">
                          {event.reviewed_by ?? ""}
                        </span>
                        <span className="font-mono-brand text-[10px] text-[var(--ink-faint)] whitespace-nowrap text-right">
                          {event.timestamp ? timeAgo(event.timestamp) : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}

      {listFilter && (
        <FilteredListModal
          key={listFilter.title}
          filter={listFilter}
          onClose={closeListModal}
          onSelectUnit={(id) => {
            setListFilter(null)
            setSelectedUnitId(id)
          }}
        />
      )}

      {selectedUnitId && (
        <KnowledgeUnitModal
          key={selectedUnitId}
          unitId={selectedUnitId}
          onClose={closeModal}
        />
      )}
    </div>
  )
}
