import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ConsentCeremony } from "../network/components/ConsentCeremony"
import { DemoControls } from "../network/components/DemoControls"
import { DesktopOnlyGate } from "../network/components/DesktopOnlyGate"
import {
  type DsnPhase,
  DsnSearchPanel,
} from "../network/components/DsnSearchPanel"
import { EventTicker, type NocEvent } from "../network/components/EventTicker"
import { L2DetailPanel } from "../network/components/L2DetailPanel"
import { type LayerKey, LeftRail } from "../network/components/LeftRail"
import { NocCanvas } from "../network/components/NocCanvas"
import { OnboardingTour } from "../network/components/OnboardingTour"
import { PacketTraceOverlay } from "../network/components/PacketTraceOverlay"
import { TopBar } from "../network/components/TopBar"
import { TopologyMirror } from "../network/components/TopologyMirror"
import {
  type DemoScenario,
  type DemoTraceResponse,
  demoTraceFixtures,
  type TraceEvent,
} from "../network/fixtures/demoTrace.fixture"
import { findNode, layoutTopology } from "../network/layout"
import { TOUR_STEPS } from "../network/tour-steps"
import type {
  CrossEnterpriseConsent,
  TopologyL2,
  TopologyResponse,
} from "../network/types"
import { useTopologyPoll } from "../network/useTopologyPoll"

interface NetworkPageProps {
  initialData?: TopologyResponse
}

function findL2(
  topology: TopologyResponse | null,
  l2_id: string | null,
): TopologyL2 | null {
  if (!topology || !l2_id) return null
  for (const ent of topology.enterprises) {
    for (const l2 of ent.l2s) {
      if (l2.l2_id === l2_id) return l2
    }
  }
  return null
}

async function runDemo(scenario: DemoScenario): Promise<DemoTraceResponse> {
  try {
    const resp = await fetch(`/api/v1/network/demo/${scenario}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    })
    if (resp.ok) return await resp.json()
  } catch {
    // ignore
  }
  return demoTraceFixtures[scenario]
}

async function signConsentRpc(): Promise<void> {
  try {
    await fetch("/api/v1/consents/sign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requester_enterprise: "orion",
        responder_enterprise: "acme",
        requester_group: "engineering",
        responder_group: "engineering",
        policy: "summary_only",
      }),
    })
  } catch {
    // ignore — fixture-only consent state is fine
  }
}

const SYNTHETIC_CONSENT: CrossEnterpriseConsent = {
  requester_enterprise: "orion",
  responder_enterprise: "acme",
  requester_group: "engineering",
  responder_group: "engineering",
  policy: "summary_only",
  expires_at: null,
}

export function NetworkPage({ initialData }: NetworkPageProps = {}) {
  const poll = useTopologyPoll({ useFixture: !!initialData })
  const baseData = initialData ?? poll.data

  const [layer, setLayer] = useState<LayerKey>("L2")
  const [selectedL2Id, setSelectedL2Id] = useState<string | null>(null)
  const [hoveredL2Id, setHoveredL2Id] = useState<string | null>(null)

  // Locally-applied consent (so the consent ceremony immediately reflects in
  // the topology even if the live endpoint isn't writing through).
  const [extraConsents, setExtraConsents] = useState<CrossEnterpriseConsent[]>(
    [],
  )

  // Merge poll data with extra consents.
  const data: TopologyResponse | null = useMemo(() => {
    if (!baseData) return null
    if (extraConsents.length === 0) return baseData
    return {
      ...baseData,
      cross_enterprise_consents: [
        ...baseData.cross_enterprise_consents,
        ...extraConsents,
      ],
    }
  }, [baseData, extraConsents])

  // ── Scene state ─────────────────────────────────────────────────────────
  const [scene, setScene] = useState<"topology" | "trace" | "dsn">("topology")
  const [trace, setTrace] = useState<DemoTraceResponse | null>(null)
  const [activeTraceEdgeIdx, setActiveTraceEdgeIdx] = useState(0)
  const [packetTrails, setPacketTrails] = useState<
    Array<{
      from: string
      to: string
      tone: "info" | "blocked" | "success"
      label?: string
    }>
  >([])
  const [zoomTo, setZoomTo] = useState<{
    cx: number
    cy: number
    scale: number
  } | null>(null)
  const [flashCenter, setFlashCenter] = useState<{
    x: number
    y: number
  } | null>(null)

  // DSN scene
  const [dsnHighlight, setDsnHighlight] = useState<string[]>([])

  // Consent ceremony
  const [consentOpen, setConsentOpen] = useState(false)

  // Onboarding
  const [tourOpen, setTourOpen] = useState(false)
  const [tourStep, setTourStep] = useState(0)
  const TOUR_KEY = "noc-tour-seen-v1"

  useEffect(() => {
    if (typeof window === "undefined") return
    const seen = window.localStorage?.getItem(TOUR_KEY)
    if (!seen) {
      const t = setTimeout(() => setTourOpen(true), 800)
      return () => clearTimeout(t)
    }
  }, [])

  // SPACE replays / opens tour from anywhere on page when not in input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      const isInput =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      if (e.key === " " && !isInput && !tourOpen) {
        e.preventDefault()
        setTourStep(0)
        setTourOpen(true)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [tourOpen])

  // Apply current tour-step camera + highlights.
  const tourState = useMemo(() => {
    if (!tourOpen) return null
    const s = TOUR_STEPS[tourStep]
    return {
      highlightNodes: s.highlightNodes ?? [],
      highlightEdges: s.highlightEdges ?? [],
      zoomTo: s.zoomTo ?? null,
    }
  }, [tourOpen, tourStep])

  // Demo runner
  const traceTimers = useRef<number[]>([])
  const clearTraceTimers = () => {
    for (const t of traceTimers.current) window.clearTimeout(t)
    traceTimers.current = []
  }

  const runScenario = useCallback(
    async (scenario: DemoScenario) => {
      clearTraceTimers()
      const resp = await runDemo(scenario)
      setTrace(resp)
      setScene("trace")
      setActiveTraceEdgeIdx(0)

      // Camera: zoom toward the cluster involved.
      const firstL2 = resp.events[0]?.l2_id
      if (firstL2 && data) {
        const layout = layoutTopology(data)
        const node = findNode(layout, firstL2)
        if (node) {
          if (
            scenario === "cross-enterprise-consented" ||
            scenario === "cross-enterprise-blocked"
          ) {
            setZoomTo({ cx: 800, cy: 450, scale: 1 })
          } else {
            setZoomTo({
              cx: node.enterprise === "orion" ? 460 : 1140,
              cy: 450,
              scale: 1.2,
            })
          }
        }
      }

      // Schedule packet trails along consecutive event L2s.
      const baseDelay = 700
      const tone: "info" | "success" | "blocked" =
        scenario === "cross-enterprise-blocked"
          ? "blocked"
          : scenario === "cross-enterprise-consented"
            ? "success"
            : "info"

      // Build a list of (from, to) hops from the events.
      const hops: Array<{
        from: string
        to: string
        latency: number
        label: string
      }> = []
      for (let i = 0; i < resp.events.length - 1; i++) {
        const a = resp.events[i]
        const b = resp.events[i + 1]
        if (a.l2_id !== b.l2_id) {
          hops.push({
            from: a.l2_id,
            to: b.l2_id,
            latency: b.latency_ms,
            label: `${b.action} · ${b.latency_ms}ms`,
          })
        }
      }
      hops.forEach((h, i) => {
        const tid = window.setTimeout(() => {
          setPacketTrails([{ from: h.from, to: h.to, tone, label: h.label }])
          setActiveTraceEdgeIdx(i + 1)
        }, i * baseDelay)
        traceTimers.current.push(tid)
      })
      // Auto-clear trails at the end + return to topology after results pause.
      const clearTid = window.setTimeout(
        () => setPacketTrails([]),
        hops.length * baseDelay + 1200,
      )
      traceTimers.current.push(clearTid)
    },
    [data, clearTraceTimers],
  )

  const closeTrace = () => {
    clearTraceTimers()
    setTrace(null)
    setPacketTrails([])
    setScene("topology")
    setZoomTo(null)
    setActiveTraceEdgeIdx(0)
  }

  // Compose events injected into the right rail when scenes fire.
  const [injectedEvents, setInjectedEvents] = useState<NocEvent[]>([])
  useEffect(() => {
    if (!trace) return
    const t = window.setTimeout(() => {
      const evts: NocEvent[] = trace.events.map((e: TraceEvent, i) => ({
        id: `${trace.scenario}-${e.step}-${Date.now()}-${i}`,
        ts: Date.now() + i,
        l2_id: e.l2_id,
        enterprise: e.l2_id.split("/")[0],
        action: actionOf(e),
        domain: domainOf(),
        detail: `${e.action} · ${e.latency_ms}ms`,
      }))
      setInjectedEvents(evts)
    }, 0)
    return () => window.clearTimeout(t)
  }, [trace])

  // Consent ceremony flow
  const onSignConsent = async () => {
    await signConsentRpc()
    // Show flash at midpoint between the two L2s.
    if (data) {
      const layout = layoutTopology(data)
      const a = findNode(layout, "orion/engineering")
      const b = findNode(layout, "acme/engineering")
      if (a && b) {
        setFlashCenter({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 })
        setTimeout(() => setFlashCenter(null), 1500)
      }
    }
    setExtraConsents([SYNTHETIC_CONSENT])
    setInjectedEvents((prev) => [
      {
        id: `consent-${Date.now()}`,
        ts: Date.now(),
        l2_id: "orion/engineering",
        enterprise: "orion",
        action: "consent.signed",
        domain: "policy",
        detail: "orion/eng ↔ acme/eng · summary_only",
      },
      ...prev,
    ])
  }

  // Cleanup timers on unmount.
  useEffect(() => () => clearTraceTimers(), [clearTraceTimers])

  // DSN phase handler: highlights L2s while DSN resolves.
  const onDsnPhase = useCallback(
    (
      phase: DsnPhase,
      payload: { topL2Ids: string[]; intent: string } | null,
    ) => {
      if (phase === "idle") {
        setDsnHighlight([])
        return
      }
      if (payload) {
        setDsnHighlight(payload.topL2Ids)
      }
    },
    [],
  )

  // Compose highlight state across scenes (tour > trace > dsn).
  const highlightedNodeIds = tourState?.highlightNodes ?? []
  const highlightedEdgeIds = tourState?.highlightEdges ?? []
  const dsnNodeHighlights = scene === "topology" ? dsnHighlight : []
  const finalHighlightNodes =
    highlightedNodeIds.length > 0 ? highlightedNodeIds : dsnNodeHighlights

  const tourZoom = tourState?.zoomTo ?? null
  const finalZoom = tourZoom ?? zoomTo

  const selectedL2 = findL2(data, selectedL2Id)
  const hasCrossEntConsent = (data?.cross_enterprise_consents.length ?? 0) > 0

  // Stop hovering when a tour step is showing — distracting.
  const effectiveHoveredId = tourOpen ? null : hoveredL2Id

  return (
    <DesktopOnlyGate>
      <div
        className="relative flex flex-col bg-[var(--bg-to)]"
        style={{ height: "calc(100vh - 49px)" }}
      >
        <TopBar
          topology={data}
          lastUpdated={poll.lastUpdated}
          pollError={poll.error}
        />

        <div className="flex flex-1 overflow-hidden">
          <LeftRail active={layer} onChange={setLayer} />

          <div className="relative flex-1">
            {!data ? (
              <div
                data-testid="topology-empty"
                className="flex h-full items-center justify-center text-sm text-white/40"
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                }}
              >
                {poll.error ? "topology unavailable" : "loading topology…"}
              </div>
            ) : (
              <>
                <NocCanvas
                  topology={data}
                  selectedL2Id={selectedL2Id}
                  onSelectL2={setSelectedL2Id}
                  hoveredL2Id={effectiveHoveredId}
                  onHoverL2={setHoveredL2Id}
                  highlightedNodeIds={finalHighlightNodes}
                  highlightedEdgeIds={highlightedEdgeIds}
                  packetTrail={packetTrails}
                  zoomTo={finalZoom}
                  layerFilter={layer}
                  flashCenter={flashCenter}
                />
                <TopologyMirror topology={data} />
                <L2DetailPanel
                  l2={selectedL2}
                  onClose={() => setSelectedL2Id(null)}
                />
                <PacketTraceOverlay
                  trace={scene === "trace" ? trace : null}
                  onReplay={() => trace && runScenario(trace.scenario)}
                  onClose={closeTrace}
                />
                <ConsentCeremony
                  open={consentOpen}
                  onSign={onSignConsent}
                  onClose={() => setConsentOpen(false)}
                />
                <OnboardingTour
                  open={tourOpen}
                  step={tourStep}
                  onNext={() => {
                    if (tourStep + 1 >= TOUR_STEPS.length) {
                      setTourOpen(false)
                      setTourStep(0)
                      try {
                        window.localStorage?.setItem(TOUR_KEY, "1")
                      } catch {
                        /* ignore */
                      }
                    } else {
                      setTourStep((s) => s + 1)
                    }
                  }}
                  onSkip={() => {
                    setTourOpen(false)
                    setTourStep(0)
                    try {
                      window.localStorage?.setItem(TOUR_KEY, "1")
                    } catch {
                      /* ignore */
                    }
                  }}
                />
              </>
            )}
          </div>

          <EventTicker topology={data} injectedEvents={injectedEvents} />
        </div>

        {/* Bottom strip — DSN search + demo buttons */}
        <div className="relative z-10 flex items-stretch gap-4 border-t border-white/5 bg-[#06061a]/85 px-6 py-4 backdrop-blur">
          <div className="flex-1">
            <DsnSearchPanel onPhaseChange={onDsnPhase} />
          </div>
          <div className="flex items-center gap-2">
            <DemoControls
              onRun={runScenario}
              onSignConsent={() => setConsentOpen(true)}
              hasConsent={hasCrossEntConsent}
              busy={scene === "trace"}
            />
          </div>
        </div>

        {/* Subtle press-SPACE hint when tour is dormant */}
        {!tourOpen && scene === "topology" && (
          <div
            className="pointer-events-none absolute right-[320px] top-[78px] rounded-md border border-white/10 bg-black/55 px-3 py-1.5 text-[10px] uppercase tracking-[0.28em] text-white/55"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            ⎵ tour
          </div>
        )}

        <ActiveTraceEdgeNoOp idx={activeTraceEdgeIdx} />
      </div>
    </DesktopOnlyGate>
  )
}

// Helper to map demo trace events into ticker actions.
function actionOf(e: TraceEvent): NocEvent["action"] {
  if (e.action === "aigrp_lookup") return "aigrp.signature"
  if (e.action === "forward_query") return "forward.query"
  if (e.action === "consent_check") return "consent.signed"
  return "ku.proposed"
}

function domainOf(): string {
  return "demo"
}

// Tiny helper component to silence the "unused state" warning when activeTraceEdgeIdx
// is updated mainly to drive timing — kept for future visual indicators.
function ActiveTraceEdgeNoOp({ idx }: { idx: number }) {
  return <span className="hidden" data-testid="trace-step" data-idx={idx} />
}
