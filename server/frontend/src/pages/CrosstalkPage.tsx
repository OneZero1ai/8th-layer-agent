// SPDX-License-Identifier: Apache-2.0
//
// Crosstalk tab — three sub-tabs (#171):
//   1. In-L2 threads      → /crosstalk/threads
//   2. Consult inbox      → /consults/inbox (peer-opened against this L2)
//   3. Consult outbox     → derived from /activity (no backend endpoint yet)
//
// Pattern matches PersonasPage (#170): row table → click row → drawer.
// Reuses the cyan-eyebrow brand chrome from PR #181 — no new design
// primitives.
//
// Backend gaps surfaced:
//   - GET /consults/outbox does not exist. Outbox is reconstructed from
//     /activity event_type=consult_open by grouping rows where the
//     caller is the requester. Best-effort; loses status fidelity (the
//     activity-log payload doesn't carry the receiver's claimed_by /
//     resolution state). Tracked as a follow-up backend issue.
//   - ThreadSummary lacks message_count + last_message_at. The list
//     view shows "—" until the thread is opened; the drawer shows the
//     authoritative count.

import { useCallback, useEffect, useMemo, useState } from "react"
import { ApiError, api } from "../api"
import { CrosstalkThreadDetailDrawer } from "../components/CrosstalkThreadDetailDrawer"
import type {
  ActivityRow,
  ConsultThread,
  CrosstalkThreadSummary,
} from "../types"
import { timeAgo } from "../utils"

type SubTab = "threads" | "inbox" | "outbox"

// "Live" = any participant emitted an activity row in the last 5
// minutes. Cheap heuristic; backend doesn't expose presence.
const LIVE_THRESHOLD_MS = 5 * 60_000

const ACTIVITY_PAGE_LIMIT = 500

function statusBadgeClasses(status: string): string {
  switch (status) {
    case "open":
    case "received":
    case "acknowledged":
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
    case "replied":
    case "claimed":
      return "bg-[color-mix(in_srgb,var(--cyan)_18%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_38%,transparent)]"
    case "closed":
    case "resolved":
      return "bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]"
    default:
      return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_28%,transparent)]"
  }
}

function truncateThreadId(id: string): string {
  if (id.length <= 16) return id
  return `${id.slice(0, 8)}…${id.slice(-4)}`
}

interface SubTabSwitcherProps {
  active: SubTab
  onChange: (tab: SubTab) => void
  inLCount: number | null
  inboxCount: number | null
  outboxCount: number | null
}

function SubTabSwitcher({
  active,
  onChange,
  inLCount,
  inboxCount,
  outboxCount,
}: SubTabSwitcherProps) {
  const tabs: Array<[SubTab, string, number | null]> = [
    ["threads", "In-L2 threads", inLCount],
    ["inbox", "Consult inbox", inboxCount],
    ["outbox", "Consult outbox", outboxCount],
  ]
  return (
    <fieldset
      aria-label="Crosstalk view"
      className="inline-flex overflow-hidden rounded-lg border border-[var(--rule-strong)] bg-[var(--surface)] text-sm"
    >
      {tabs.map(([value, label, count]) => (
        <button
          key={value}
          type="button"
          onClick={() => onChange(value)}
          aria-pressed={active === value}
          className={`px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.16em] transition-colors ${
            active === value
              ? "bg-[color-mix(in_srgb,var(--cyan)_22%,transparent)] text-[var(--cyan)]"
              : "text-[var(--ink-dim)] hover:bg-[var(--surface-hover)]"
          }`}
        >
          {label}
          {count !== null && (
            <span className="ml-1.5 text-[var(--ink-mute)]">({count})</span>
          )}
        </button>
      ))}
    </fieldset>
  )
}

// ---------------------------------------------------------------------------
// In-L2 threads view
// ---------------------------------------------------------------------------

interface InLThreadsViewProps {
  threads: CrosstalkThreadSummary[] | null
  liveSet: Set<string>
  onSelect: (threadId: string) => void
  search: string
  setSearch: (s: string) => void
  statusFilter: string
  setStatusFilter: (s: string) => void
}

function InLThreadsView({
  threads,
  liveSet,
  onSelect,
  search,
  setSearch,
  statusFilter,
  setStatusFilter,
}: InLThreadsViewProps) {
  const filtered = useMemo(() => {
    if (!threads) return null
    let rows = threads
    if (statusFilter !== "all") {
      rows = rows.filter((t) => t.status === statusFilter)
    }
    if (search) {
      const needle = search.toLowerCase()
      rows = rows.filter(
        (t) =>
          t.subject.toLowerCase().includes(needle) ||
          t.id.toLowerCase().includes(needle) ||
          t.created_by_username.toLowerCase().includes(needle) ||
          t.participants.some((p) => p.toLowerCase().includes(needle)),
      )
    }
    return [...rows].sort((a, b) =>
      a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0,
    )
  }, [threads, search, statusFilter])

  const total = threads?.length ?? 0
  const open = threads?.filter((t) => t.status === "open").length ?? 0
  const closed = threads?.filter((t) => t.status === "closed").length ?? 0

  return (
    <section>
      <div className="flex flex-wrap items-center gap-3">
        <fieldset
          aria-label="Filter threads"
          className="inline-flex overflow-hidden rounded-lg border border-[var(--rule-strong)] bg-[var(--surface)] text-sm"
        >
          {(
            [
              ["all", `All (${total})`],
              ["open", `Open (${open})`],
              ["closed", `Closed (${closed})`],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setStatusFilter(value)}
              aria-pressed={statusFilter === value}
              className={`px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.16em] transition-colors ${
                statusFilter === value
                  ? "bg-[color-mix(in_srgb,var(--cyan)_22%,transparent)] text-[var(--cyan)]"
                  : "text-[var(--ink-dim)] hover:bg-[var(--surface-hover)]"
              }`}
            >
              {label}
            </button>
          ))}
        </fieldset>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search subject, thread ID, or participant…"
          aria-label="Search crosstalk threads"
          className="brand-input min-w-40 flex-1 text-sm"
        />
      </div>

      {filtered === null ? (
        <p className="mt-4 text-sm text-[var(--ink-mute)]">Loading…</p>
      ) : threads?.length === 0 ? (
        <div className="mt-6 brand-surface flex flex-col items-center justify-center py-12 gap-3">
          <span
            aria-hidden="true"
            className="font-display text-3xl text-[var(--ink-faint)]"
          >
            ∅
          </span>
          <span className="eyebrow text-[var(--cyan)]">No threads yet</span>
          <span className="text-sm text-[var(--ink-mute)] max-w-prose text-center">
            Inter-session crosstalk threads inside this L2 will surface here as
            soon as the first agent sends a message.
          </span>
        </div>
      ) : filtered.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--ink-mute)]">
          No threads match the current filter.
        </p>
      ) : (
        <div className="mt-4 overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
          <table className="w-full text-sm">
            <thead className="border-b border-[var(--rule)] bg-[color-mix(in_srgb,var(--bg-from)_40%,transparent)]">
              <tr>
                <th
                  scope="col"
                  className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  Thread
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  Subject
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  Participants
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  Status
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  Opened
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t) => {
                const isLive = liveSet.has(t.id)
                return (
                  <tr
                    key={t.id}
                    className="border-t border-[var(--rule)] hover:bg-[var(--surface-hover)] cursor-pointer transition-colors"
                    onClick={() => onSelect(t.id)}
                  >
                    <td className="px-4 py-3">
                      <code
                        className="font-mono-brand text-[11px] text-[var(--ink-dim)]"
                        title={t.id}
                      >
                        {truncateThreadId(t.id)}
                      </code>
                    </td>
                    <td className="px-4 py-3">
                      <span className="font-display text-base text-[var(--ink)]">
                        {t.subject || (
                          <span className="text-[var(--ink-mute)] italic">
                            (no subject)
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-mono-brand text-[11px] tabular-nums text-[var(--ink-dim)]">
                          {t.participants.length}
                        </span>
                        {isLive && (
                          <span
                            className="inline-flex items-center gap-1 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--emerald)]"
                            title="Active in last 5 minutes"
                          >
                            <span className="h-1.5 w-1.5 rounded-full bg-[var(--emerald)] animate-pulse" />
                            live
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                          t.status,
                        )}`}
                      >
                        {t.status}
                      </span>
                    </td>
                    <td
                      className="px-4 py-3 text-[var(--ink-mute)]"
                      title={new Date(t.created_at).toLocaleString()}
                    >
                      {timeAgo(t.created_at)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Consult inbox view
// ---------------------------------------------------------------------------

interface ConsultInboxViewProps {
  threads: ConsultThread[] | null
  selfL2Id: string | null
  onSelect: (threadId: string) => void
}

function ConsultInboxView({
  threads,
  selfL2Id,
  onSelect,
}: ConsultInboxViewProps) {
  if (threads === null) {
    return <p className="mt-4 text-sm text-[var(--ink-mute)]">Loading…</p>
  }
  if (threads.length === 0) {
    return (
      <div className="mt-6 brand-surface flex flex-col items-center justify-center py-12 gap-3">
        <span
          aria-hidden="true"
          className="font-display text-3xl text-[var(--ink-faint)]"
        >
          ∅
        </span>
        <span className="eyebrow text-[var(--cyan)]">Inbox is empty</span>
        <span className="text-sm text-[var(--ink-mute)] max-w-prose text-center">
          Consults opened against this L2{selfL2Id ? ` (${selfL2Id})` : ""} by
          peer Enterprises will arrive here. None yet.
        </span>
      </div>
    )
  }
  return (
    <div className="mt-4 overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
      <table className="w-full text-sm">
        <thead className="border-b border-[var(--rule)] bg-[color-mix(in_srgb,var(--bg-from)_40%,transparent)]">
          <tr>
            <th
              scope="col"
              className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
            >
              From
            </th>
            <th
              scope="col"
              className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
            >
              Topic
            </th>
            <th
              scope="col"
              className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
            >
              Status
            </th>
            <th
              scope="col"
              className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
            >
              Opened
            </th>
          </tr>
        </thead>
        <tbody>
          {threads.map((t) => (
            <tr
              key={t.thread_id}
              className="border-t border-[var(--rule)] hover:bg-[var(--surface-hover)] cursor-pointer transition-colors"
              onClick={() => onSelect(t.thread_id)}
            >
              <td className="px-4 py-3">
                <p className="font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--violet)]">
                  {t.from_l2_id}
                </p>
                <p className="font-mono-brand text-[11px] text-[var(--ink-dim)] mt-0.5">
                  {t.from_persona}
                </p>
              </td>
              <td className="px-4 py-3">
                <p className="font-display text-base text-[var(--ink)]">
                  {t.subject || (
                    <span className="text-[var(--ink-mute)] italic">
                      (no subject)
                    </span>
                  )}
                </p>
              </td>
              <td className="px-4 py-3">
                <span
                  className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                    t.status,
                  )}`}
                >
                  {t.status}
                </span>
              </td>
              <td
                className="px-4 py-3 text-[var(--ink-mute)]"
                title={new Date(t.created_at).toLocaleString()}
              >
                {timeAgo(t.created_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Consult outbox view (derived from activity log)
// ---------------------------------------------------------------------------

// Derived shape — the activity log doesn't preserve receiver-side
// status, so we mark all rows as "opened" and let the operator chase
// the responder side via the peer L2's UI. Keeps the view honest.
export interface DerivedOutboxRow {
  thread_id: string
  to_l2_id: string
  to_persona: string
  topic: string
  opened_at: string
  opened_by_persona: string | null
}

function deriveOutboxFromActivity(rows: ActivityRow[]): DerivedOutboxRow[] {
  const byThread = new Map<string, DerivedOutboxRow>()
  for (const row of rows) {
    if (row.event_type !== "consult_open") continue
    const threadId =
      row.thread_or_chain_id ??
      (typeof row.payload.thread_id === "string"
        ? (row.payload.thread_id as string)
        : null)
    if (!threadId) continue
    if (byThread.has(threadId)) continue
    const toL2 =
      typeof row.payload.to_l2_id === "string"
        ? (row.payload.to_l2_id as string)
        : ""
    const toPersona =
      typeof row.payload.to_persona === "string"
        ? (row.payload.to_persona as string)
        : ""
    const topic =
      typeof row.payload.subject === "string"
        ? (row.payload.subject as string)
        : typeof row.payload.topic === "string"
          ? (row.payload.topic as string)
          : "(no subject)"
    byThread.set(threadId, {
      thread_id: threadId,
      to_l2_id: toL2,
      to_persona: toPersona,
      topic,
      opened_at: row.ts,
      opened_by_persona: row.persona,
    })
  }
  return Array.from(byThread.values()).sort((a, b) =>
    a.opened_at < b.opened_at ? 1 : a.opened_at > b.opened_at ? -1 : 0,
  )
}

interface ConsultOutboxViewProps {
  rows: DerivedOutboxRow[] | null
}

function ConsultOutboxView({ rows }: ConsultOutboxViewProps) {
  if (rows === null) {
    return <p className="mt-4 text-sm text-[var(--ink-mute)]">Loading…</p>
  }
  if (rows.length === 0) {
    return (
      <div className="mt-6 brand-surface flex flex-col items-center justify-center py-12 gap-3">
        <span
          aria-hidden="true"
          className="font-display text-3xl text-[var(--ink-faint)]"
        >
          ∅
        </span>
        <span className="eyebrow text-[var(--cyan)]">Outbox is empty</span>
        <span className="text-sm text-[var(--ink-mute)] max-w-prose text-center">
          When agents on this L2 open a consult against a peer Enterprise, the
          outbound thread will surface here.
        </span>
      </div>
    )
  }
  return (
    <>
      <p className="mt-3 text-xs text-[var(--ink-mute)] max-w-prose">
        Derived from the activity log (no <code>/consults/outbox</code>{" "}
        endpoint). Status reflects the moment the consult was opened — the
        peer-side timeline is authoritative for replies and resolution.
      </p>
      <div className="mt-3 overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
        <table className="w-full text-sm">
          <thead className="border-b border-[var(--rule)] bg-[color-mix(in_srgb,var(--bg-from)_40%,transparent)]">
            <tr>
              <th
                scope="col"
                className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
              >
                To
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
              >
                Topic
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
              >
                Opened by
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
              >
                Opened
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.thread_id} className="border-t border-[var(--rule)]">
                <td className="px-4 py-3">
                  <p className="font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--violet)]">
                    {r.to_l2_id || "—"}
                  </p>
                  <p className="font-mono-brand text-[11px] text-[var(--ink-dim)] mt-0.5">
                    {r.to_persona || "—"}
                  </p>
                </td>
                <td className="px-4 py-3">
                  <span className="font-display text-base text-[var(--ink)]">
                    {r.topic}
                  </span>
                </td>
                <td className="px-4 py-3 font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--ink-dim)]">
                  {r.opened_by_persona ?? "—"}
                </td>
                <td
                  className="px-4 py-3 text-[var(--ink-mute)]"
                  title={new Date(r.opened_at).toLocaleString()}
                >
                  {timeAgo(r.opened_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Page shell
// ---------------------------------------------------------------------------

export function CrosstalkPage() {
  const [subTab, setSubTab] = useState<SubTab>("threads")
  const [threads, setThreads] = useState<CrosstalkThreadSummary[] | null>(null)
  const [consultInbox, setConsultInbox] = useState<ConsultThread[] | null>(null)
  const [selfL2Id, setSelfL2Id] = useState<string | null>(null)
  const [outboxRows, setOutboxRows] = useState<DerivedOutboxRow[] | null>(null)
  const [recentActivity, setRecentActivity] = useState<ActivityRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [statusFilter, setStatusFilter] = useState<string>("all")
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null)
  const [selectedConsultId, setSelectedConsultId] = useState<string | null>(
    null,
  )

  const load = useCallback(async () => {
    setError(null)
    try {
      // Three parallel reads. /consults/inbox returns 401 only on a
      // missing JWT (admins + users both get their own inbox); failure
      // here degrades gracefully so the in-L2 threads view still loads.
      const [threadsResp, inboxResp, activityResp] = await Promise.all([
        api.listCrosstalkThreads(100).catch((err) => {
          if (err instanceof ApiError && err.status === 403) return null
          throw err
        }),
        api.consultInbox(true, 100).catch((err) => {
          // Some deploys gate consults behind admin role.
          if (err instanceof ApiError && err.status === 403) return null
          throw err
        }),
        api.listActivity({ limit: ACTIVITY_PAGE_LIMIT }).catch(() => ({
          items: [] as ActivityRow[],
          count: 0,
          next_cursor: null,
        })),
      ])
      setThreads(threadsResp?.items ?? [])
      setConsultInbox(inboxResp?.threads ?? [])
      setSelfL2Id(inboxResp?.self_l2_id ?? null)
      setRecentActivity(activityResp.items)
      setOutboxRows(deriveOutboxFromActivity(activityResp.items))
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load crosstalk")
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Compute the "live" set for in-L2 threads from recent activity. A
  // thread is "live" if any crosstalk_send / crosstalk_reply event for
  // that thread arrived in the last LIVE_THRESHOLD_MS.
  const liveSet = useMemo(() => {
    const cutoff = Date.now() - LIVE_THRESHOLD_MS
    const live = new Set<string>()
    for (const row of recentActivity) {
      if (
        row.event_type !== "crosstalk_send" &&
        row.event_type !== "crosstalk_reply"
      ) {
        continue
      }
      if (!row.thread_or_chain_id) continue
      if (new Date(row.ts).getTime() < cutoff) continue
      live.add(row.thread_or_chain_id)
    }
    return live
  }, [recentActivity])

  return (
    <div className="space-y-8">
      <section>
        <p className="eyebrow">Communication</p>
        <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
          Crosstalk
        </h1>
        <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
          Inter-session crosstalk threads inside this L2, plus the cross-
          Enterprise consult inbox and outbox. Click any row for the full
          message history and routing context.
        </p>
      </section>

      {error && (
        <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4">
          <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
            {error}
          </p>
        </div>
      )}

      <SubTabSwitcher
        active={subTab}
        onChange={setSubTab}
        inLCount={threads?.length ?? null}
        inboxCount={consultInbox?.length ?? null}
        outboxCount={outboxRows?.length ?? null}
      />

      {subTab === "threads" && (
        <InLThreadsView
          threads={threads}
          liveSet={liveSet}
          onSelect={setSelectedThreadId}
          search={search}
          setSearch={setSearch}
          statusFilter={statusFilter}
          setStatusFilter={setStatusFilter}
        />
      )}

      {subTab === "inbox" && (
        <ConsultInboxView
          threads={consultInbox}
          selfL2Id={selfL2Id}
          onSelect={setSelectedConsultId}
        />
      )}

      {subTab === "outbox" && <ConsultOutboxView rows={outboxRows} />}

      {selectedThreadId && (
        <CrosstalkThreadDetailDrawer
          mode="in-l2"
          threadId={selectedThreadId}
          onClose={() => setSelectedThreadId(null)}
          onClosed={() => {
            setSelectedThreadId(null)
            load()
          }}
        />
      )}

      {selectedConsultId && (
        <CrosstalkThreadDetailDrawer
          mode="consult"
          threadId={selectedConsultId}
          consultMeta={
            consultInbox?.find((t) => t.thread_id === selectedConsultId) ?? null
          }
          onClose={() => setSelectedConsultId(null)}
        />
      )}
    </div>
  )
}
