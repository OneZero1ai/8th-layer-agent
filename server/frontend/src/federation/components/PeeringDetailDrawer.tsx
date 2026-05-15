// Slide-in drawer surfacing the everything-about-this-peering view. Anchored
// right, modal-blur backdrop. Scope is read-only — no actions live here. The
// `silently_broken` banner is the headline contribution: it surfaces the
// "active but actually dead" state most ops dashboards hide.

import { timeAgo } from "../../utils"
import type {
  ActivePeering,
  ReachabilityCheck,
  SilentBreakReason,
} from "../types"
import { Sparkline } from "./Sparkline"

interface PeeringDetailDrawerProps {
  peering: ActivePeering | null
  onClose: () => void
}

function reachabilityBadge(check: ReachabilityCheck): string {
  if (check.status === "ok") {
    return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
  }
  if (check.status === "warn") {
    return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_30%,transparent)]"
  }
  return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
}

function silentBreakLabel(reason: SilentBreakReason): string {
  switch (reason) {
    case "dead-origin":
      return "Origin unreachable for >24h — peering is silently broken."
    case "expired-key":
      return "Peer signing key past rotation deadline — handshakes will fail."
    case "sni-mismatch":
      return "TLS SNI does not match peer cert — silently broken."
    case "no-traffic-7d":
      return "No round-trips in 7 days despite expected traffic."
  }
}

export function PeeringDetailDrawer({
  peering,
  onClose,
}: PeeringDetailDrawerProps) {
  if (!peering) return null
  const sparkValues = peering.health_timeline_30d.map((d) => d.success_rate)
  const totalConsults = peering.consult_log.reduce((acc, b) => acc + b.count, 0)
  // Topology mini-map: this L2 + peer + up to 3 of peer's other peers (~3 hops).
  // Static sketch — real graph plumbing belongs in a follow-up backed by the
  // directory's neighbour-of-neighbour read.
  const topologyHops = [
    "this enterprise",
    peering.peer.display_name,
    "→ neighbours (~3 hops)",
  ]

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="peering-drawer-heading"
      className="fixed inset-0 z-30 flex justify-end bg-black/55 backdrop-blur-sm"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose()
      }}
    >
      <div
        className="h-full w-full max-w-xl overflow-y-auto bg-[var(--bg-via)] border-l border-[var(--rule-strong)] shadow-[0_0_60px_-10px_rgba(0,0,0,0.65)]"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        role="document"
      >
        <div className="flex items-center justify-between border-b border-[var(--rule)] px-6 py-4">
          <div className="min-w-0">
            <p className="eyebrow text-[var(--cyan)]">Peering detail</p>
            <h2
              id="peering-drawer-heading"
              className="font-display text-xl text-[var(--ink)] mt-0.5 truncate"
            >
              {peering.peer.display_name}
            </h2>
            <code className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
              {peering.peer.enterprise_id}
            </code>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-2 py-1 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)]"
          >
            Close
          </button>
        </div>

        <div className="px-6 py-5 space-y-6">
          {peering.silently_broken && (
            <div
              role="alert"
              data-testid="silent-break-banner"
              className="rounded-lg border border-[color-mix(in_srgb,var(--rose)_45%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] px-4 py-3"
            >
              <p className="eyebrow text-[var(--rose)]">
                Silent break detected
              </p>
              <p className="mt-1 text-sm text-[var(--ink)]">
                {silentBreakLabel(peering.silently_broken)}
              </p>
            </div>
          )}

          <section>
            <p className="eyebrow">Endpoints &amp; reachability</p>
            <div className="mt-2 space-y-2">
              {(
                [
                  ["Ours", peering.reachability.ours],
                  ["Theirs", peering.reachability.theirs],
                ] as const
              ).map(([label, check]) => (
                <div
                  key={label}
                  className="brand-surface px-3 py-2 flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <p className="font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
                      {label}
                    </p>
                    <code className="font-mono-brand text-xs text-[var(--ink)] truncate block">
                      {check.endpoint}
                    </code>
                    {check.detail && (
                      <p className="mt-1 text-[11px] text-[var(--rose)] font-mono-brand">
                        {check.detail}
                      </p>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <span
                      className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${reachabilityBadge(check)}`}
                    >
                      {check.status}
                    </span>
                    <p className="mt-1 text-[11px] text-[var(--ink-mute)] font-mono-brand">
                      green {check.last_green ? timeAgo(check.last_green) : "—"}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section>
            <p className="eyebrow">Signing keys</p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              {(
                [
                  ["Ours", peering.signing_keys.ours],
                  ["Theirs", peering.signing_keys.theirs],
                ] as const
              ).map(([label, key]) => (
                <div key={label} className="brand-surface p-3">
                  <p className="font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
                    {label}
                  </p>
                  <code className="mt-1 block break-all font-mono-brand text-[11px] text-[var(--cyan)]">
                    {key.fingerprint}
                  </code>
                  <p className="mt-1 text-[11px] text-[var(--ink-mute)]">
                    {key.algorithm} · rotated{" "}
                    {key.rotated_at ? timeAgo(key.rotated_at) : "—"}
                  </p>
                </div>
              ))}
            </div>
          </section>

          <section>
            <p className="eyebrow">Offer signature timeline</p>
            <ol className="mt-2 space-y-2 border-l border-[var(--rule)] pl-4">
              {peering.offer_timeline.map((evt) => (
                <li key={`${evt.ts}-${evt.kind}`} className="relative">
                  <span className="absolute -left-[19px] top-1 h-2 w-2 rounded-full bg-[var(--cyan)] shadow-[0_0_8px_var(--cyan)]" />
                  <p className="font-mono-brand text-[11px] uppercase tracking-[0.16em] text-[var(--ink-dim)]">
                    {evt.kind} · {timeAgo(evt.ts)}
                  </p>
                  <p className="text-sm text-[var(--ink)]">
                    {evt.by_human}
                    {evt.detail ? (
                      <span className="text-[var(--ink-mute)]">
                        {" "}
                        — {evt.detail}
                      </span>
                    ) : null}
                  </p>
                </li>
              ))}
            </ol>
          </section>

          <section>
            <p className="eyebrow">Consult log (lifetime)</p>
            <div className="mt-2 grid grid-cols-4 gap-2">
              {peering.consult_log.map((b) => (
                <div key={b.status} className="brand-surface p-3 text-center">
                  <p className="font-display text-2xl text-[var(--ink)]">
                    {b.count}
                  </p>
                  <p className="font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
                    {b.status}
                  </p>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-[var(--ink-mute)] font-mono-brand">
              {totalConsults} total · {peering.inbound_consults_7d} inbound
              7-day
            </p>
          </section>

          <section>
            <p className="eyebrow">Health timeline (30d)</p>
            <div className="mt-2 brand-surface p-3 flex items-end gap-3">
              <Sparkline
                values={sparkValues}
                width={320}
                height={48}
                ariaLabel={`30-day success rate for ${peering.peer.display_name}`}
              />
              <p className="font-mono-brand text-[11px] text-[var(--ink-mute)]">
                latest{" "}
                <span className="text-[var(--ink)]">
                  {((sparkValues[sparkValues.length - 1] ?? 0) * 100).toFixed(
                    0,
                  )}
                  %
                </span>
              </p>
            </div>
          </section>

          <section>
            <p className="eyebrow">Topology (~3 hops)</p>
            <div className="mt-2 brand-surface p-3 font-mono-brand text-[11px] text-[var(--ink-dim)]">
              {topologyHops.join("  →  ")}
              <p className="mt-2 text-[var(--ink-mute)]">
                Full graph view follows once the directory exposes
                neighbour-of-neighbour reads (backend gap, see #172).
              </p>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
