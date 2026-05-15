// Federation tab — L2 admin view of this Enterprise's peerings.
//
// Sub-tabs (Active / Pending / Outgoing / Health) live as URL-less inline
// state; cheap to navigate, cheap to test. Drawer click-through opens the
// PeeringDetailDrawer with reachability/signing-key/timeline/topology
// information. Backend reads default to the directory's
// `/api/v1/directory/peerings/self` endpoint and gracefully degrade to a
// fixture when the route 404s — same shape as NetworkPage's demo fallback.
//
// Bundle discipline: no new top-level deps; sparkline + heatmap are
// hand-rolled SVG/CSS-grid. Recharts is used for the stacked area, but only
// because it's already in the bundle (DashboardPage). Hard ceiling for this
// PR is +5KB gzipped.

import { useCallback, useEffect, useMemo, useState } from "react"
import { actOnOffer, fetchFederationView } from "../federation/api"
import { HealthHeatmap } from "../federation/components/HealthHeatmap"
import { PeeringDetailDrawer } from "../federation/components/PeeringDetailDrawer"
import { Sparkline } from "../federation/components/Sparkline"
import { StackedAreaChart } from "../federation/components/StackedAreaChart"
import type {
  ActivePeering,
  FederationView,
  PeeringStatus,
} from "../federation/types"
import { timeAgo, timeUntil } from "../utils"

type SubTab = "active" | "pending" | "outgoing" | "health"

const SUB_TABS: Array<[SubTab, string]> = [
  ["active", "Active"],
  ["pending", "Pending"],
  ["outgoing", "Outgoing"],
  ["health", "Health"],
]

function statusBadge(status: PeeringStatus): string {
  switch (status) {
    case "active":
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
    case "pending-acceptance":
      return "bg-[color-mix(in_srgb,var(--cyan)_14%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_30%,transparent)]"
    case "expiring-soon":
      return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_30%,transparent)]"
    case "expired":
      return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
  }
}

function rateBadge(rate: number): string {
  if (rate >= 0.9)
    return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)]"
  if (rate >= 0.6)
    return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)]"
  return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)]"
}

interface FederationPageProps {
  initialData?: FederationView
}

export function FederationPage({ initialData }: FederationPageProps = {}) {
  const [view, setView] = useState<FederationView | null>(initialData ?? null)
  const [loading, setLoading] = useState(!initialData)
  const [fromFixture, setFromFixture] = useState<boolean>(false)
  const [subTab, setSubTab] = useState<SubTab>("active")
  const [drawer, setDrawer] = useState<ActivePeering | null>(null)
  const [actionPending, setActionPending] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (initialData) return
    setLoading(true)
    const { data, fromFixture } = await fetchFederationView()
    setView(data)
    setFromFixture(fromFixture)
    setLoading(false)
  }, [initialData])

  useEffect(() => {
    refresh()
  }, [refresh])

  const activeCount = view?.active.length ?? 0
  const pendingCount = view?.pending.length ?? 0
  const outgoingCount = view?.outgoing.length ?? 0
  const alarmCount = view?.mesh_health.alarms.length ?? 0

  const silentlyBrokenCount = useMemo(
    () => view?.active.filter((p) => p.silently_broken).length ?? 0,
    [view],
  )

  async function handleOfferAction(
    offer_id: string,
    action: "accept" | "decline" | "withdraw",
  ) {
    setActionPending(offer_id)
    await actOnOffer(offer_id, action)
    // Optimistic remove from local state. Real reconcile happens on next poll.
    setView((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        pending: prev.pending.filter((o) => o.offer_id !== offer_id),
        outgoing: prev.outgoing.filter((o) => o.offer_id !== offer_id),
      }
    })
    setActionPending(null)
  }

  if (loading || !view) {
    return (
      <div className="space-y-8">
        <header>
          <p className="eyebrow">Federation</p>
          <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
            Peering health
          </h1>
        </header>
        <p className="text-sm text-[var(--ink-mute)]">Loading peerings…</p>
      </div>
    )
  }

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Federation</p>
        <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
          Peering health
        </h1>
        <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
          Cross-Enterprise consult routing health from this L2's point of view.
          Active peerings, pending offers, and aggregate mesh health — all
          driven by the directory's <code>peering_offers</code>,{" "}
          <code>peering_acceptances</code>, and <code>consults</code> tables.
        </p>
        {fromFixture && (
          <p
            className="mt-2 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--gold)]"
            data-testid="federation-fixture-banner"
          >
            Showing fixture data — directory endpoint not yet reachable.
          </p>
        )}
        {silentlyBrokenCount > 0 && (
          <p
            className="mt-2 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--rose)]"
            data-testid="federation-silent-break-summary"
          >
            {silentlyBrokenCount} peering
            {silentlyBrokenCount === 1 ? "" : "s"} silently broken — open the
            drawer for detail.
          </p>
        )}
      </header>

      <nav
        aria-label="Federation sub-tabs"
        className="flex flex-wrap gap-1 border-b border-[var(--rule)]"
      >
        {SUB_TABS.map(([key, label]) => {
          const count =
            key === "active"
              ? activeCount
              : key === "pending"
                ? pendingCount
                : key === "outgoing"
                  ? outgoingCount
                  : alarmCount
          const active = subTab === key
          return (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setSubTab(key)}
              className={`relative px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.18em] transition-colors ${
                active
                  ? "text-[var(--ink)]"
                  : "text-[var(--ink-mute)] hover:text-[var(--ink-dim)]"
              }`}
            >
              {label}
              {count > 0 && (
                <span className="ml-2 inline-flex items-center justify-center rounded-full px-1.5 py-0.5 text-[10px] font-mono-brand text-[var(--cyan)] bg-[color-mix(in_srgb,var(--cyan)_14%,transparent)] border border-[color-mix(in_srgb,var(--cyan)_28%,transparent)]">
                  {count}
                </span>
              )}
              {active && (
                <span className="absolute -bottom-px left-2 right-2 h-px bg-gradient-to-r from-transparent via-[var(--cyan)] to-transparent" />
              )}
            </button>
          )
        })}
      </nav>

      {subTab === "active" && (
        <ActiveTab
          view={view}
          onOpenDrawer={setDrawer}
          silentlyBrokenCount={silentlyBrokenCount}
        />
      )}
      {subTab === "pending" && (
        <PendingTab
          view={view}
          actionPending={actionPending}
          onAction={handleOfferAction}
        />
      )}
      {subTab === "outgoing" && (
        <OutgoingTab
          view={view}
          actionPending={actionPending}
          onWithdraw={(id) => handleOfferAction(id, "withdraw")}
        />
      )}
      {subTab === "health" && <HealthTab view={view} />}

      <PeeringDetailDrawer peering={drawer} onClose={() => setDrawer(null)} />
    </div>
  )
}

// ── Active sub-tab ──────────────────────────────────────────────────────────

function ActiveTab({
  view,
  onOpenDrawer,
  silentlyBrokenCount,
}: {
  view: FederationView
  onOpenDrawer: (p: ActivePeering) => void
  silentlyBrokenCount: number
}) {
  if (view.active.length === 0) {
    return (
      <EmptyState
        eyebrow="No active peerings"
        message="This L2 has no accepted peering offers yet. Open one from the network tab or accept a pending offer."
      />
    )
  }
  return (
    <section>
      <div className="brand-surface-raised overflow-hidden">
        <table className="w-full text-sm" data-testid="active-peerings-table">
          <thead className="bg-[var(--surface)]">
            <tr className="text-left">
              {[
                "Peer",
                "Direction",
                "Status",
                "Topics",
                "7d success",
                "7d inbound",
                "Last RT",
                "30d",
              ].map((h) => (
                <th
                  key={h}
                  scope="col"
                  className="px-3 py-2 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.active.map((p) => (
              <tr
                key={p.peering_id}
                className="cursor-pointer border-t border-[var(--rule)] hover:bg-[var(--surface-hover)] transition-colors"
                onClick={() => onOpenDrawer(p)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onOpenDrawer(p)
                }}
                tabIndex={0}
              >
                <td className="px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    {p.silently_broken && (
                      <span
                        role="img"
                        title={`Silently broken: ${p.silently_broken}`}
                        aria-label="silently broken"
                        className="inline-block h-2 w-2 rounded-full bg-[var(--rose)] shadow-[0_0_8px_var(--rose)]"
                      />
                    )}
                    <div className="min-w-0">
                      <p className="font-display text-[var(--ink)] truncate">
                        {p.peer.display_name}
                      </p>
                      <code className="font-mono-brand text-[10px] text-[var(--ink-mute)] truncate">
                        {p.peer.enterprise_id}
                      </code>
                    </div>
                  </div>
                </td>
                <td className="px-3 py-2.5">
                  <span className="font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-dim)]">
                    {p.direction === "offered-by-us"
                      ? "→ outbound"
                      : "← inbound"}
                  </span>
                </td>
                <td className="px-3 py-2.5">
                  <span
                    className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadge(p.status)}`}
                  >
                    {p.status}
                  </span>
                  {p.status !== "expired" && (
                    <p className="mt-1 text-[10px] text-[var(--ink-mute)] font-mono-brand">
                      expires {timeUntil(p.expires_at)}
                    </p>
                  )}
                </td>
                <td className="px-3 py-2.5">
                  <div className="flex flex-wrap gap-1 max-w-[180px]">
                    {p.topic_filters.slice(0, 2).map((t) => (
                      <span
                        key={t}
                        className="rounded-full bg-[color-mix(in_srgb,var(--violet)_12%,transparent)] border border-[color-mix(in_srgb,var(--violet)_22%,transparent)] px-2 py-0.5 font-mono-brand text-[10px] text-[var(--violet)]"
                      >
                        {t}
                      </span>
                    ))}
                    {p.topic_filters.length > 2 && (
                      <span className="font-mono-brand text-[10px] text-[var(--ink-mute)]">
                        +{p.topic_filters.length - 2}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2.5">
                  <span
                    className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[11px] ${rateBadge(p.outbound_success_rate_7d)}`}
                  >
                    {(p.outbound_success_rate_7d * 100).toFixed(0)}%
                  </span>
                </td>
                <td className="px-3 py-2.5 font-mono-brand text-[var(--ink-dim)]">
                  {p.inbound_consults_7d}
                </td>
                <td className="px-3 py-2.5 font-mono-brand text-[11px] text-[var(--ink-mute)]">
                  {p.last_round_trip_at ? timeAgo(p.last_round_trip_at) : "—"}
                </td>
                <td className="px-3 py-2.5">
                  <Sparkline
                    values={p.health_timeline_30d.map((d) => d.success_rate)}
                    ariaLabel={`30-day success rate for ${p.peer.display_name}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {silentlyBrokenCount > 0 && (
        <p className="mt-3 text-[11px] text-[var(--ink-mute)] font-mono-brand">
          Tip: rows with a rose pulse-dot are nominally active but failing
          health checks. Open the drawer to see the break reason.
        </p>
      )}
    </section>
  )
}

// ── Pending sub-tab (inbox) ─────────────────────────────────────────────────

function PendingTab({
  view,
  actionPending,
  onAction,
}: {
  view: FederationView
  actionPending: string | null
  onAction: (
    offer_id: string,
    action: "accept" | "decline" | "withdraw",
  ) => void
}) {
  if (view.pending.length === 0) {
    return (
      <EmptyState
        eyebrow="Inbox empty"
        message="No incoming peering offers awaiting your acceptance."
      />
    )
  }
  return (
    <section className="space-y-3">
      {view.pending.map((o) => {
        const isPending = actionPending === o.offer_id
        return (
          <div
            key={o.offer_id}
            className="brand-surface-raised p-4 flex flex-col md:flex-row md:items-center gap-4"
          >
            <div className="flex-1 min-w-0">
              <p className="eyebrow">Offered {timeAgo(o.offered_at)}</p>
              <h3 className="font-display text-lg text-[var(--ink)] mt-0.5 truncate">
                {o.peer.display_name}
              </h3>
              <code className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
                {o.peer.enterprise_id}
              </code>
              <div className="mt-2 flex flex-wrap gap-1">
                {o.topic_filters.map((t) => (
                  <span
                    key={t}
                    className="rounded-full bg-[color-mix(in_srgb,var(--violet)_12%,transparent)] border border-[color-mix(in_srgb,var(--violet)_22%,transparent)] px-2 py-0.5 font-mono-brand text-[10px] text-[var(--violet)]"
                  >
                    {t}
                  </span>
                ))}
              </div>
              <p className="mt-2 font-mono-brand text-[11px] text-[var(--ink-dim)]">
                policy:{" "}
                <span className="text-[var(--ink)]">{o.content_policy}</span> ·
                sig{" "}
                <code className="text-[var(--cyan)]">
                  {o.signature_fingerprint}
                </code>
              </p>
            </div>
            <div className="flex flex-wrap gap-2 shrink-0">
              <button
                type="button"
                disabled={isPending}
                onClick={() => onAction(o.offer_id, "accept")}
                className="rounded-md bg-[color-mix(in_srgb,var(--emerald)_18%,transparent)] border border-[color-mix(in_srgb,var(--emerald)_45%,transparent)] px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--emerald)] hover:bg-[color-mix(in_srgb,var(--emerald)_28%,transparent)] disabled:opacity-50"
              >
                Accept
              </button>
              <button
                type="button"
                disabled={isPending}
                onClick={() => onAction(o.offer_id, "decline")}
                className="rounded-md bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] border border-[color-mix(in_srgb,var(--rose)_45%,transparent)] px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_28%,transparent)] disabled:opacity-50"
              >
                Decline
              </button>
              <button
                type="button"
                disabled={true}
                title="Counter-offer flow ships in #172 follow-up"
                className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)] disabled:opacity-50"
              >
                Counter-offer
              </button>
            </div>
          </div>
        )
      })}
    </section>
  )
}

// ── Outgoing sub-tab ────────────────────────────────────────────────────────

function OutgoingTab({
  view,
  actionPending,
  onWithdraw,
}: {
  view: FederationView
  actionPending: string | null
  onWithdraw: (offer_id: string) => void
}) {
  if (view.outgoing.length === 0) {
    return (
      <EmptyState
        eyebrow="No outgoing offers"
        message="Use the Network tab to extend a peering offer to another Enterprise."
      />
    )
  }
  return (
    <section className="space-y-3">
      {view.outgoing.map((o) => {
        const isPending = actionPending === o.offer_id
        const expired = o.status === "expired"
        return (
          <div
            key={o.offer_id}
            className="brand-surface-raised p-4 flex flex-col md:flex-row md:items-center gap-4"
          >
            <div className="flex-1 min-w-0">
              <p className="eyebrow">Offered {timeAgo(o.offered_at)}</p>
              <h3 className="font-display text-lg text-[var(--ink)] mt-0.5 truncate">
                {o.peer.display_name}
              </h3>
              <code className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
                {o.peer.enterprise_id}
              </code>
              <p className="mt-2 font-mono-brand text-[11px] text-[var(--ink-dim)]">
                topics:{" "}
                <span className="text-[var(--ink)]">
                  {o.topic_filters.join(", ")}
                </span>
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span
                className={`rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${
                  o.status === "pending"
                    ? "bg-[color-mix(in_srgb,var(--cyan)_14%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_30%,transparent)]"
                    : o.status === "accepted"
                      ? "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
                      : "bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]"
                }`}
              >
                {o.status}
              </span>
              <button
                type="button"
                disabled={isPending || expired}
                onClick={() => onWithdraw(o.offer_id)}
                className="rounded-md bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] border border-[color-mix(in_srgb,var(--rose)_45%,transparent)] px-3 py-1.5 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_28%,transparent)] disabled:opacity-50"
              >
                Withdraw
              </button>
            </div>
          </div>
        )
      })}
    </section>
  )
}

// ── Health sub-tab ──────────────────────────────────────────────────────────

function HealthTab({ view }: { view: FederationView }) {
  const { mesh_health } = view
  if (mesh_health.daily.length === 0) {
    return (
      <EmptyState
        eyebrow="No mesh data"
        message="Health timelines surface once peerings carry consult traffic."
      />
    )
  }
  return (
    <section className="space-y-6">
      <div>
        <p className="eyebrow">Consult outcomes (30d)</p>
        <div className="brand-surface-raised mt-2 p-3">
          <StackedAreaChart
            data={mesh_health.daily}
            series={[
              { key: "success", label: "success", color: "#10b981" },
              { key: "blocked", label: "blocked", color: "#fcd34d" },
              { key: "timeout", label: "timeout", color: "#a685ff" },
              { key: "error", label: "error", color: "#ff5c7c" },
            ]}
          />
        </div>
      </div>

      <div>
        <p className="eyebrow">Per-peer success rate (heatmap)</p>
        <div className="brand-surface-raised mt-2 p-3">
          <HealthHeatmap data={mesh_health.heatmap} />
        </div>
      </div>

      <div>
        <p className="eyebrow">Alarms</p>
        {mesh_health.alarms.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--ink-mute)]">
            No peerings below the 90% success threshold.
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {mesh_health.alarms.map((a) => (
              <li
                key={a.peering_id}
                className="brand-surface flex items-center justify-between p-3 border-l-2 border-l-[var(--rose)]"
              >
                <div>
                  <p className="font-display text-[var(--ink)]">
                    {a.peer.display_name}
                  </p>
                  <p className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
                    success rate fell below {(a.threshold * 100).toFixed(0)}% —
                    current {(a.current_rate * 100).toFixed(0)}% since{" "}
                    {timeAgo(a.since)}
                  </p>
                </div>
                <span className="rounded-full bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)] px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em]">
                  alarm
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

// ── Empty state ────────────────────────────────────────────────────────────

function EmptyState({
  eyebrow,
  message,
}: {
  eyebrow: string
  message: string
}) {
  return (
    <div
      className="brand-surface flex flex-col items-center justify-center py-12 gap-3"
      data-testid="federation-empty"
    >
      <span
        aria-hidden="true"
        className="font-display text-3xl text-[var(--ink-faint)]"
      >
        ∅
      </span>
      <span className="eyebrow text-[var(--cyan)]">{eyebrow}</span>
      <span className="text-sm text-[var(--ink-mute)] text-center max-w-sm">
        {message}
      </span>
    </div>
  )
}
