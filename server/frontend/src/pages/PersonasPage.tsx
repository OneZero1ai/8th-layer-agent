// SPDX-License-Identifier: Apache-2.0
//
// Personas tab — directory + per-persona detail drawer (#170).
//
// V1 reads from existing endpoints only:
//   - /activity  → timeline + persona derivation (group by persona)
//   - /review/units → KU contributions (filtered client-side by created_by)
//
// A dedicated /admin/personas read is a follow-up backend issue
// (denormalised summary; current approach is O(activity_rows) per page
// load and accepts the cost for V1).

import { useCallback, useEffect, useMemo, useState } from "react"
import { ApiError, api } from "../api"
import { PersonaDetailDrawer } from "../components/PersonaDetailDrawer"
import type {
  ActivityRow,
  PersonaStatus,
  PersonaSummary,
  ReviewItem,
} from "../types"
import { timeAgo } from "../utils"

// Persona-derivation parameters. Pull a generous slice of recent
// activity (admin scope: every persona within the Enterprise) and
// fold into per-persona buckets. Empty result = empty-state render.
const ACTIVITY_PAGE_LIMIT = 500

// Derive status from last_seen freshness. The backend doesn't
// surface a lifecycle status today; this is a client-side
// approximation until the lifecycle_events table read lands.
const IDLE_THRESHOLD_DAYS = 7
const DEPARTED_THRESHOLD_DAYS = 60

type SortKey = "last_seen" | "name" | "group" | "ku_count" | "joined"
type SortDir = "asc" | "desc"

function classifyStatus(lastSeen: string | null): PersonaStatus {
  if (!lastSeen) return "departed"
  const ageDays = (Date.now() - new Date(lastSeen).getTime()) / 86_400_000
  if (ageDays > DEPARTED_THRESHOLD_DAYS) return "departed"
  if (ageDays > IDLE_THRESHOLD_DAYS) return "idle"
  return "active"
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

// Build the persona directory by folding /activity rows + /review/units.
// We pull KUs once and join on `created_by` rather than issuing one
// request per persona — keeps page load O(1) HTTP calls.
function derivePersonas(
  activity: ActivityRow[],
  units: ReviewItem[],
): PersonaSummary[] {
  const kuByPersona = new Map<string, number>()
  for (const item of units) {
    const author = item.knowledge_unit.created_by
    if (!author) continue
    kuByPersona.set(author, (kuByPersona.get(author) ?? 0) + 1)
  }

  type Bucket = {
    name: string
    enterprise: string
    group: string | null
    first: string
    last: string
  }
  const buckets = new Map<string, Bucket>()
  for (const row of activity) {
    if (!row.persona) continue
    const existing = buckets.get(row.persona)
    if (!existing) {
      buckets.set(row.persona, {
        name: row.persona,
        enterprise: row.tenant_enterprise,
        group: row.tenant_group,
        first: row.ts,
        last: row.ts,
      })
      continue
    }
    if (row.ts < existing.first) existing.first = row.ts
    if (row.ts > existing.last) existing.last = row.ts
    // Last-write-wins on group; persona moves don't happen mid-life
    // in practice but if they do, the most recent row is the source
    // of truth.
    if (row.tenant_group && !existing.group) existing.group = row.tenant_group
  }

  // Personas with KUs but no activity rows still merit a row in the
  // directory (rare — but covers the case where a persona proposed
  // before instrumentation landed and never came back).
  for (const author of kuByPersona.keys()) {
    if (buckets.has(author)) continue
    buckets.set(author, {
      name: author,
      enterprise: "",
      group: null,
      first: "",
      last: "",
    })
  }

  return Array.from(buckets.values()).map((b) => ({
    name: b.name,
    group: b.group,
    enterprise: b.enterprise,
    status: classifyStatus(b.last || null),
    joined: b.first || null,
    last_seen: b.last || null,
    ku_count: kuByPersona.get(b.name) ?? 0,
    // No admin endpoint to enumerate per-persona keys; surfaces as
    // "—" in the table until backend issue ships.
    api_key_count: 0,
  }))
}

function sortPersonas(
  rows: PersonaSummary[],
  key: SortKey,
  dir: SortDir,
): PersonaSummary[] {
  const sorted = [...rows].sort((a, b) => {
    let cmp = 0
    switch (key) {
      case "name":
        cmp = a.name.localeCompare(b.name)
        break
      case "group":
        cmp = (a.group ?? "").localeCompare(b.group ?? "")
        break
      case "ku_count":
        cmp = a.ku_count - b.ku_count
        break
      case "joined":
        cmp = (a.joined ?? "").localeCompare(b.joined ?? "")
        break
      case "last_seen":
        cmp = (a.last_seen ?? "").localeCompare(b.last_seen ?? "")
        break
    }
    return dir === "asc" ? cmp : -cmp
  })
  return sorted
}

function matchesSearch(
  persona: PersonaSummary,
  units: ReviewItem[],
  query: string,
): boolean {
  if (query === "") return true
  const needle = query.toLowerCase()
  if (persona.name.toLowerCase().includes(needle)) return true
  if ((persona.group ?? "").toLowerCase().includes(needle)) return true
  // Domain search: any KU authored by this persona that tags the
  // search term as a domain → match.
  const personaUnits = units.filter(
    (u) => u.knowledge_unit.created_by === persona.name,
  )
  return personaUnits.some((u) =>
    u.knowledge_unit.domains.some((d) => d.toLowerCase().includes(needle)),
  )
}

interface SortHeaderProps {
  label: string
  field: SortKey
  active: SortKey
  dir: SortDir
  onSort: (field: SortKey) => void
  align?: "left" | "right"
}

function SortHeader({
  label,
  field,
  active,
  dir,
  onSort,
  align = "left",
}: SortHeaderProps) {
  const isActive = active === field
  return (
    <button
      type="button"
      onClick={() => onSort(field)}
      className={`group inline-flex items-center gap-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] transition-colors ${
        isActive ? "text-[var(--cyan)]" : "text-[var(--ink-mute)]"
      } ${align === "right" ? "justify-end" : ""}`}
    >
      {label}
      <span
        aria-hidden="true"
        className={`text-[8px] ${isActive ? "opacity-100" : "opacity-30 group-hover:opacity-60"}`}
      >
        {isActive && dir === "asc" ? "▲" : "▼"}
      </span>
    </button>
  )
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleString()
}

export function PersonasPage() {
  const [activity, setActivity] = useState<ActivityRow[] | null>(null)
  const [units, setUnits] = useState<ReviewItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>("last_seen")
  const [sortDir, setSortDir] = useState<SortDir>("desc")
  const [search, setSearch] = useState("")
  const [statusFilter, setStatusFilter] = useState<PersonaStatus | "all">("all")
  const [selectedPersona, setSelectedPersona] = useState<string | null>(null)

  const loadDirectory = useCallback(async () => {
    setError(null)
    try {
      // Pull activity (admin scope returns every persona). KUs in
      // parallel — independent endpoints, no ordering dependency.
      const [activityResp, unitsResp] = await Promise.all([
        api.listActivity({ limit: ACTIVITY_PAGE_LIMIT }),
        api.listUnits({}).catch((err) => {
          // /review/units is admin-scoped; non-admins get 403. Degrade
          // gracefully with an empty KU list rather than crashing the
          // whole page — directory still renders from /activity alone.
          if (err instanceof ApiError && err.status === 403) return []
          throw err
        }),
      ])
      setUnits(unitsResp)
      setActivity(activityResp.items)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load personas")
    }
  }, [])

  useEffect(() => {
    loadDirectory()
  }, [loadDirectory])

  const personas = useMemo(() => {
    if (!activity || !units) return null
    return derivePersonas(activity, units)
  }, [activity, units])

  const filteredPersonas = useMemo(() => {
    if (!personas) return null
    let rows = personas
    if (statusFilter !== "all") {
      rows = rows.filter((p) => p.status === statusFilter)
    }
    if (search) {
      rows = rows.filter((p) => matchesSearch(p, units ?? [], search))
    }
    return sortPersonas(rows, sortKey, sortDir)
  }, [personas, statusFilter, search, sortKey, sortDir, units])

  function onSort(field: SortKey) {
    if (sortKey === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"))
    } else {
      setSortKey(field)
      setSortDir("desc")
    }
  }

  const personaCount = personas?.length ?? 0
  const activeCount = personas?.filter((p) => p.status === "active").length ?? 0
  const idleCount = personas?.filter((p) => p.status === "idle").length ?? 0
  const departedCount =
    personas?.filter((p) => p.status === "departed").length ?? 0

  const personaUnits = useMemo(() => {
    if (!units || !selectedPersona) return []
    return units.filter((u) => u.knowledge_unit.created_by === selectedPersona)
  }, [units, selectedPersona])

  return (
    <div className="space-y-8">
      <section>
        <p className="eyebrow">Identity</p>
        <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
          Personas
        </h1>
        <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
          Every persona that has registered with this L2. Status, last activity,
          and knowledge contributions surface at a glance; click a row for the
          full activity timeline and KU breakdown.
        </p>
      </section>

      {error && (
        <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4">
          <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
            {error}
          </p>
        </div>
      )}

      <section>
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="shrink-0 font-display text-xl text-[var(--ink)]">
            Directory
            <span className="ml-3 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {personaCount} {personaCount === 1 ? "persona" : "personas"}
            </span>
          </h2>
          <fieldset
            aria-label="Filter personas"
            className="inline-flex shrink-0 overflow-hidden rounded-lg border border-[var(--rule-strong)] bg-[var(--surface)] text-sm"
          >
            {(
              [
                ["all", `All (${personaCount})`],
                ["active", `Active (${activeCount})`],
                ["idle", `Idle (${idleCount})`],
                ["departed", `Departed (${departedCount})`],
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
            placeholder="Search persona, group, or KU domain…"
            aria-label="Search personas"
            className="brand-input min-w-40 flex-1 text-sm"
          />
        </div>

        {filteredPersonas === null ? (
          <p className="mt-4 text-sm text-[var(--ink-mute)]">Loading…</p>
        ) : personas?.length === 0 ? (
          <div className="mt-6 brand-surface flex flex-col items-center justify-center py-12 gap-3">
            <span
              aria-hidden="true"
              className="font-display text-3xl text-[var(--ink-faint)]"
            >
              ∅
            </span>
            <span className="eyebrow text-[var(--cyan)]">No personas yet</span>
            <span className="text-sm text-[var(--ink-mute)]">
              Personas will appear here once they register and emit their first
              activity event.
            </span>
          </div>
        ) : filteredPersonas.length === 0 ? (
          <p className="mt-4 text-sm text-[var(--ink-mute)]">
            No personas match the current filter.
          </p>
        ) : (
          <div className="mt-4 overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
            <table className="w-full text-sm">
              <thead className="border-b border-[var(--rule)] bg-[color-mix(in_srgb,var(--bg-from)_40%,transparent)]">
                <tr>
                  <th scope="col" className="px-4 py-3 text-left">
                    <SortHeader
                      label="Persona"
                      field="name"
                      active={sortKey}
                      dir={sortDir}
                      onSort={onSort}
                    />
                  </th>
                  <th scope="col" className="px-4 py-3 text-left">
                    <SortHeader
                      label="Group"
                      field="group"
                      active={sortKey}
                      dir={sortDir}
                      onSort={onSort}
                    />
                  </th>
                  <th
                    scope="col"
                    className="px-4 py-3 text-left font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                  >
                    Status
                  </th>
                  <th scope="col" className="px-4 py-3 text-left">
                    <SortHeader
                      label="Last seen"
                      field="last_seen"
                      active={sortKey}
                      dir={sortDir}
                      onSort={onSort}
                    />
                  </th>
                  <th scope="col" className="px-4 py-3 text-right">
                    <SortHeader
                      label="KUs"
                      field="ku_count"
                      active={sortKey}
                      dir={sortDir}
                      onSort={onSort}
                      align="right"
                    />
                  </th>
                  <th scope="col" className="px-4 py-3 text-left">
                    <SortHeader
                      label="Joined"
                      field="joined"
                      active={sortKey}
                      dir={sortDir}
                      onSort={onSort}
                    />
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredPersonas.map((p) => (
                  <tr
                    key={p.name}
                    className="border-t border-[var(--rule)] hover:bg-[var(--surface-hover)] cursor-pointer transition-colors"
                    onClick={() => setSelectedPersona(p.name)}
                  >
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="font-display text-base text-[var(--ink)] hover:text-[var(--cyan)] transition-colors text-left"
                      >
                        {p.name}
                      </button>
                    </td>
                    <td className="px-4 py-3 font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--ink-dim)]">
                      {p.group ?? "—"}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                          p.status,
                        )}`}
                      >
                        {p.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className="text-[var(--ink-dim)]"
                        title={formatTimestamp(p.last_seen)}
                      >
                        {p.last_seen ? timeAgo(p.last_seen) : "never"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono-brand tabular-nums text-[var(--ink)]">
                      {p.ku_count}
                    </td>
                    <td
                      className="px-4 py-3 text-[var(--ink-mute)]"
                      title={formatTimestamp(p.joined)}
                    >
                      {p.joined ? timeAgo(p.joined) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {selectedPersona && personas?.find((p) => p.name === selectedPersona) && (
        <PersonaDetailDrawer
          persona={
            personas.find((p) => p.name === selectedPersona) as PersonaSummary
          }
          units={personaUnits}
          onClose={() => setSelectedPersona(null)}
        />
      )}
    </div>
  )
}
