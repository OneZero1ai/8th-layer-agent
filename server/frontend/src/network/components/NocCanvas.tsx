import { AnimatePresence, motion } from "framer-motion"
import { useEffect, useMemo, useRef, useState } from "react"
import {
  CANVAS_HEIGHT,
  CANVAS_WIDTH,
  findNode,
  layoutTopology,
  type NodePosition,
  nodeRadius,
} from "../layout"
import type { CrossEnterpriseConsent, TopologyResponse } from "../types"

interface Props {
  topology: TopologyResponse
  selectedL2Id: string | null
  onSelectL2: (id: string | null) => void
  hoveredL2Id: string | null
  onHoverL2: (id: string | null) => void
  highlightedNodeIds?: string[]
  highlightedEdgeIds?: string[]
  packetTrail?: Array<{
    from: string
    to: string
    tone: "info" | "blocked" | "success"
    label?: string
  }>
  zoomTo?: { cx: number; cy: number; scale: number } | null
  layerFilter: "L1" | "L2" | "L3"
  flashCenter?: { x: number; y: number } | null
}

const ENTERPRISE_TINT: Record<
  string,
  { halo: string; rim: string; chip: string }
> = {
  orion: { halo: "#7C5CFF", rim: "#A38BFF", chip: "#3D2D8F" },
  acme: { halo: "#5BD0FF", rim: "#7FE4FF", chip: "#1E5C7E" },
}

function isConsented(
  consents: CrossEnterpriseConsent[],
  fromEnt: string,
  fromGroup: string,
  toEnt: string,
  toGroup: string,
): boolean {
  return consents.some((c) => {
    const matchOne =
      c.requester_enterprise === fromEnt &&
      c.responder_enterprise === toEnt &&
      (c.requester_group === null || c.requester_group === fromGroup) &&
      (c.responder_group === null || c.responder_group === toGroup)
    const matchOther =
      c.requester_enterprise === toEnt &&
      c.responder_enterprise === fromEnt &&
      (c.requester_group === null || c.requester_group === toGroup) &&
      (c.responder_group === null || c.responder_group === fromGroup)
    return matchOne || matchOther
  })
}

// Hash a string into a stable [0, 1) — used to vary particle phases per edge.
function hash01(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0
  return ((h >>> 0) % 1000) / 1000
}

export function NocCanvas({
  topology,
  selectedL2Id,
  onSelectL2,
  hoveredL2Id,
  onHoverL2,
  highlightedNodeIds = [],
  highlightedEdgeIds = [],
  packetTrail = [],
  zoomTo = null,
  layerFilter,
  flashCenter = null,
}: Props) {
  const layout = useMemo(() => layoutTopology(topology), [topology])
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [tNow, setTNow] = useState(0)
  const tickerRef = useRef<number | null>(null)

  // Single shared rAF loop for orbit + particle phase.
  useEffect(() => {
    let frame = 0
    const start = performance.now()
    const loop = () => {
      const t = (performance.now() - start) / 1000
      setTNow(t)
      frame = requestAnimationFrame(loop)
      tickerRef.current = frame
    }
    frame = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(frame)
  }, [])

  // Edge list (peer + cross), built once per topology change.
  const edges = useMemo(() => {
    const list: Array<{
      id: string
      kind: "peer" | "cross"
      consented: boolean
      from: NodePosition
      to: NodePosition
    }> = []
    const seen = new Set<string>()
    // peer edges
    for (const ent of topology.enterprises) {
      for (const l2 of ent.l2s) {
        for (const peer of l2.peers) {
          const [a, b] = [l2.l2_id, peer.l2_id].sort()
          const id = `peer:${a}--${b}`
          if (seen.has(id)) continue
          seen.add(id)
          const from = findNode(layout, a)
          const to = findNode(layout, b)
          if (from && to)
            list.push({ id, kind: "peer", consented: true, from, to })
        }
      }
    }
    // cross edges
    if (topology.enterprises.length >= 2) {
      const ents = topology.enterprises
      for (let i = 0; i < ents.length; i++) {
        for (let j = i + 1; j < ents.length; j++) {
          for (const aL2 of ents[i].l2s) {
            for (const bL2 of ents[j].l2s) {
              const [src, tgt] = [aL2.l2_id, bL2.l2_id].sort()
              const id = `cross:${src}--${tgt}`
              const consented = isConsented(
                topology.cross_enterprise_consents,
                ents[i].enterprise,
                aL2.group,
                ents[j].enterprise,
                bL2.group,
              )
              const from = findNode(layout, src)
              const to = findNode(layout, tgt)
              if (from && to)
                list.push({ id, kind: "cross", consented, from, to })
            }
          }
        }
      }
    }
    return list
  }, [topology, layout])

  // Active packet trails (lookup by from->to edge).
  const trailLookup = useMemo(() => {
    const m = new Map<
      string,
      {
        tone: "info" | "blocked" | "success"
        label?: string
        from: string
        to: string
      }
    >()
    for (const t of packetTrail) {
      const [a, b] = [t.from, t.to].sort()
      const peerKey = `peer:${a}--${b}`
      const crossKey = `cross:${a}--${b}`
      m.set(peerKey, t)
      m.set(crossKey, t)
    }
    return m
  }, [packetTrail])

  const dimNonL2 = layerFilter !== "L2"
  // L3 is "coming Q3" — dims everything (acts as preview / placeholder).
  const dimAll = layerFilter === "L3"

  // Camera transform for zoom-to-cluster scenes.
  const cameraStyle = useMemo(() => {
    if (!zoomTo) return { transform: "translate3d(0,0,0) scale(1)" }
    // We translate the canvas so (cx, cy) → center, then scale.
    const tx = (CANVAS_WIDTH / 2 - zoomTo.cx) * zoomTo.scale
    const ty = (CANVAS_HEIGHT / 2 - zoomTo.cy) * zoomTo.scale
    return {
      transform: `translate3d(${tx}px, ${ty}px, 0) scale(${zoomTo.scale})`,
      transformOrigin: "0 0",
    }
  }, [zoomTo])

  return (
    <div
      ref={containerRef}
      data-testid="topology-canvas"
      className="relative h-full w-full overflow-hidden"
      style={{
        background:
          "radial-gradient(ellipse at 30% 20%, rgba(124,92,255,0.18), transparent 60%)," +
          "radial-gradient(ellipse at 75% 80%, rgba(91,208,255,0.12), transparent 55%)," +
          "linear-gradient(180deg, #06061a 0%, #08081f 50%, #04040f 100%)",
      }}
    >
      {/* star field */}
      <StarField />
      {/* faint scanline overlay */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.06]"
        style={{
          backgroundImage:
            "repeating-linear-gradient(0deg, rgba(255,255,255,0.6) 0px, rgba(255,255,255,0.6) 1px, transparent 1px, transparent 4px)",
        }}
      />
      {/* grid */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.07]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(124,92,255,0.4) 1px, transparent 1px)," +
            "linear-gradient(90deg, rgba(124,92,255,0.4) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
        }}
      />

      <motion.svg
        viewBox={`0 0 ${CANVAS_WIDTH} ${CANVAS_HEIGHT}`}
        preserveAspectRatio="xMidYMid meet"
        className="absolute inset-0 h-full w-full"
        animate={cameraStyle}
        transition={{ type: "tween", duration: 1.2, ease: [0.22, 1, 0.36, 1] }}
        onClick={(e) => {
          if (e.target === e.currentTarget) onSelectL2(null)
        }}
      >
        <defs>
          {/* Edge gradients */}
          <linearGradient id="grad-peer-orion" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#7C5CFF" stopOpacity="0.85" />
            <stop offset="50%" stopColor="#B6A0FF" stopOpacity="1" />
            <stop offset="100%" stopColor="#7C5CFF" stopOpacity="0.85" />
          </linearGradient>
          <linearGradient id="grad-peer-acme" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#5BD0FF" stopOpacity="0.85" />
            <stop offset="50%" stopColor="#A4E8FF" stopOpacity="1" />
            <stop offset="100%" stopColor="#5BD0FF" stopOpacity="0.85" />
          </linearGradient>
          <linearGradient id="grad-cross-consented" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#FFB347" stopOpacity="0.95" />
            <stop offset="50%" stopColor="#FFD89B" stopOpacity="1" />
            <stop offset="100%" stopColor="#FFB347" stopOpacity="0.95" />
          </linearGradient>
          <linearGradient id="grad-trail-info" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#5BD0FF" stopOpacity="0" />
            <stop offset="50%" stopColor="#A4E8FF" stopOpacity="1" />
            <stop offset="100%" stopColor="#5BD0FF" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="grad-trail-success" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#FFB347" stopOpacity="0" />
            <stop offset="50%" stopColor="#FFD89B" stopOpacity="1" />
            <stop offset="100%" stopColor="#FFB347" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="grad-trail-blocked" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#FF5C7C" stopOpacity="0" />
            <stop offset="50%" stopColor="#FF8FA8" stopOpacity="1" />
            <stop offset="100%" stopColor="#FF5C7C" stopOpacity="0" />
          </linearGradient>

          {/* Node radial fills */}
          <radialGradient id="node-fill-orion" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#1A1240" />
            <stop offset="60%" stopColor="#0E0828" />
            <stop offset="100%" stopColor="#06041A" />
          </radialGradient>
          <radialGradient id="node-fill-acme" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#0F2438" />
            <stop offset="60%" stopColor="#0A1828" />
            <stop offset="100%" stopColor="#04101A" />
          </radialGradient>

          {/* Glow filter */}
          <filter id="glow-soft" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter
            id="glow-strong"
            x="-100%"
            y="-100%"
            width="300%"
            height="300%"
          >
            <feGaussianBlur stdDeviation="10" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* Cluster halos */}
        {layout.clusters.map((c) => {
          const tint = ENTERPRISE_TINT[c.enterprise] ?? ENTERPRISE_TINT.orion
          return (
            <g key={`cluster-${c.enterprise}`} opacity={dimAll ? 0.25 : 1}>
              <ellipse
                cx={c.cx}
                cy={c.cy}
                rx={c.rx}
                ry={c.ry}
                fill={tint.halo}
                fillOpacity="0.04"
                stroke={tint.halo}
                strokeOpacity="0.32"
                strokeWidth="1.5"
                strokeDasharray="2 8"
              />
              <ellipse
                cx={c.cx}
                cy={c.cy}
                rx={c.rx + 14}
                ry={c.ry + 14}
                fill="none"
                stroke={tint.halo}
                strokeOpacity="0.12"
                strokeWidth="1"
              />
              {/* Cluster label */}
              <text
                x={c.cx}
                y={c.cy - c.ry - 28}
                textAnchor="middle"
                fontFamily="'JetBrains Mono', ui-monospace, monospace"
                fontSize="13"
                letterSpacing="0.32em"
                fontWeight="600"
                fill={tint.rim}
                opacity="0.9"
              >
                ◆ ENTERPRISE / {c.enterprise.toUpperCase()}
              </text>
              <text
                x={c.cx}
                y={c.cy - c.ry - 11}
                textAnchor="middle"
                fontFamily="'JetBrains Mono', ui-monospace, monospace"
                fontSize="10"
                fill="#5A5A7E"
                letterSpacing="0.18em"
              >
                aigrp.peer-mesh ·{" "}
                {topology.enterprises.find((e) => e.enterprise === c.enterprise)
                  ?.l2s.length ?? 0}{" "}
                L2
              </text>
            </g>
          )
        })}

        {/* Edges layer */}
        <g opacity={dimAll ? 0.2 : 1}>
          {edges.map((e) => {
            const isCross = e.kind === "cross"
            const isHighlighted = highlightedEdgeIds.includes(e.id)
            const dim =
              dimNonL2 ||
              (highlightedEdgeIds.length > 0 && !isHighlighted) ||
              (selectedL2Id !== null &&
                !(e.from.l2_id === selectedL2Id || e.to.l2_id === selectedL2Id))
            const opacityBase = isCross ? (e.consented ? 0.85 : 0.32) : 0.7
            const stroke =
              isCross && e.consented
                ? "url(#grad-cross-consented)"
                : isCross
                  ? "#384067"
                  : e.from.enterprise === "orion"
                    ? "url(#grad-peer-orion)"
                    : "url(#grad-peer-acme)"
            const strokeWidth = isHighlighted ? 3.5 : isCross ? 1.5 : 2
            const dash = isCross && !e.consented ? "5 9" : undefined
            const dx = e.to.x - e.from.x
            const dy = e.to.y - e.from.y
            const len = Math.hypot(dx, dy)

            const trail = trailLookup.get(e.id)

            return (
              <g key={e.id} opacity={dim ? 0.18 : 1}>
                <line
                  x1={e.from.x}
                  y1={e.from.y}
                  x2={e.to.x}
                  y2={e.to.y}
                  stroke={stroke}
                  strokeWidth={strokeWidth}
                  strokeOpacity={opacityBase}
                  strokeDasharray={dash}
                  strokeLinecap="round"
                  filter={isHighlighted ? "url(#glow-strong)" : undefined}
                />
                {/* particles */}
                {(!isCross || e.consented) && !dim && (
                  <ParticleStream
                    edgeId={e.id}
                    from={e.from}
                    to={e.to}
                    tNow={tNow}
                    color={
                      isCross
                        ? "#FFD89B"
                        : e.from.enterprise === "orion"
                          ? "#B6A0FF"
                          : "#A4E8FF"
                    }
                    speed={isCross ? 0.32 : 0.45}
                    count={isCross ? 4 : 5}
                  />
                )}
                {/* Active packet (comet trail) */}
                {trail && (
                  <PacketComet
                    from={trail.from === e.from.l2_id ? e.from : e.to}
                    to={trail.from === e.from.l2_id ? e.to : e.from}
                    tone={trail.tone}
                    label={trail.label}
                    duration={Math.max(0.8, len / 800)}
                  />
                )}
              </g>
            )
          })}
        </g>

        {/* Nodes layer */}
        <g>
          {layout.nodes.map((n) => {
            const isSelected = n.l2_id === selectedL2Id
            const isHovered = n.l2_id === hoveredL2Id
            const isHighlighted = highlightedNodeIds.includes(n.l2_id)
            const r = nodeRadius(n.ku_count)
            const tint = ENTERPRISE_TINT[n.enterprise] ?? ENTERPRISE_TINT.orion
            const dim =
              (highlightedNodeIds.length > 0 && !isHighlighted) || dimAll
            const personas =
              topology.enterprises
                .find((e) => e.enterprise === n.enterprise)
                ?.l2s.find((l) => l.l2_id === n.l2_id)?.active_personas ?? []
            const domains = Array.from(
              new Set(personas.flatMap((p) => p.expertise_domains)),
            ).slice(0, 5)

            return (
              <g
                key={n.l2_id}
                style={{ cursor: "pointer" }}
                onClick={(e) => {
                  e.stopPropagation()
                  onSelectL2(n.l2_id === selectedL2Id ? null : n.l2_id)
                }}
                onMouseEnter={() => onHoverL2(n.l2_id)}
                onMouseLeave={() => onHoverL2(null)}
                opacity={dim ? 0.25 : 1}
              >
                {/* Halo ring (animated breathing) */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={r + 18 + Math.sin(tNow * 1.5 + hash01(n.l2_id) * 6) * 3}
                  fill="none"
                  stroke={tint.halo}
                  strokeOpacity={isSelected || isHovered ? 0.5 : 0.18}
                  strokeWidth={isSelected ? 2 : 1}
                />
                {/* Outer glow when selected/highlighted */}
                {(isSelected || isHovered || isHighlighted) && (
                  <circle
                    cx={n.x}
                    cy={n.y}
                    r={r + 30}
                    fill={tint.halo}
                    fillOpacity="0.12"
                    filter="url(#glow-strong)"
                  />
                )}

                {/* KU-count gauge ring */}
                <KuGauge
                  cx={n.x}
                  cy={n.y}
                  r={r + 6}
                  ku_count={n.ku_count}
                  color={tint.halo}
                />

                {/* Node body */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={r}
                  fill={
                    n.enterprise === "orion"
                      ? "url(#node-fill-orion)"
                      : "url(#node-fill-acme)"
                  }
                  stroke={tint.rim}
                  strokeOpacity={isSelected ? 0.95 : 0.6}
                  strokeWidth={isSelected ? 2.5 : 1.5}
                  filter={
                    isSelected || isHovered ? "url(#glow-soft)" : undefined
                  }
                />
                {/* Inner ring */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={r - 8}
                  fill="none"
                  stroke={tint.halo}
                  strokeOpacity="0.22"
                  strokeWidth="1"
                />

                {/* Group name */}
                <text
                  x={n.x}
                  y={n.y - 2}
                  textAnchor="middle"
                  fontFamily="'JetBrains Mono', ui-monospace, monospace"
                  fontSize="13"
                  fontWeight="600"
                  fill="#E6E6F8"
                  letterSpacing="0.05em"
                >
                  {n.group}
                </text>
                {/* KU count */}
                <text
                  x={n.x}
                  y={n.y + 16}
                  textAnchor="middle"
                  fontFamily="'JetBrains Mono', ui-monospace, monospace"
                  fontSize="11"
                  fill={tint.rim}
                  opacity="0.85"
                >
                  {n.ku_count} kus
                </text>

                {/* Orbiting domain pills (frozen on hover) */}
                <DomainOrbit
                  cx={n.x}
                  cy={n.y}
                  r={r + 38}
                  domains={domains}
                  tNow={tNow}
                  frozen={isHovered || isSelected}
                  color={tint.rim}
                  seed={hash01(n.l2_id)}
                />
              </g>
            )
          })}
        </g>

        {/* Consent flash */}
        {flashCenter && <ConsentFlash x={flashCenter.x} y={flashCenter.y} />}
      </motion.svg>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// Sub-components

function ParticleStream({
  edgeId,
  from,
  to,
  tNow,
  color,
  speed,
  count,
}: {
  edgeId: string
  from: NodePosition
  to: NodePosition
  tNow: number
  color: string
  speed: number
  count: number
}) {
  // Particles travel in both directions; stagger phase per particle.
  const seed = hash01(edgeId)
  const dots: Array<{ x: number; y: number; opacity: number; r: number }> = []
  for (let i = 0; i < count; i++) {
    const phase = (tNow * speed + seed + i / count) % 1
    const a = phase
    const b = 1 - phase
    ;[a, b].forEach((t, k) => {
      const x = from.x + (to.x - from.x) * t
      const y = from.y + (to.y - from.y) * t
      // Bright at midpoint, fade at endpoints
      const fade = Math.sin(t * Math.PI)
      dots.push({
        x,
        y,
        opacity: 0.4 + 0.6 * fade,
        r: 1.6 + 1.2 * fade + (k === 0 ? 0.2 : 0),
      })
    })
  }
  return (
    <g>
      {dots.map((d, i) => (
        <circle
          key={i}
          cx={d.x}
          cy={d.y}
          r={d.r}
          fill={color}
          opacity={d.opacity}
        />
      ))}
    </g>
  )
}

function PacketComet({
  from,
  to,
  tone,
  label,
  duration,
}: {
  from: NodePosition
  to: NodePosition
  tone: "info" | "blocked" | "success"
  label?: string
  duration: number
}) {
  const colorMap = {
    info: "#5BD0FF",
    success: "#FFD89B",
    blocked: "#FF5C7C",
  }
  const color = colorMap[tone]
  return (
    <motion.g
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
    >
      <motion.g
        initial={{ x: from.x, y: from.y }}
        animate={{ x: to.x, y: to.y }}
        transition={{ duration, ease: "easeInOut" }}
      >
        <circle r={11} fill={color} opacity={0.25} filter="url(#glow-strong)" />
        <circle r={5} fill={color} />
        <circle r={2} fill="#FFFFFF" />
        {label && (
          <text
            y={-16}
            textAnchor="middle"
            fontFamily="'JetBrains Mono', ui-monospace, monospace"
            fontSize="11"
            fill="#FFFFFF"
            opacity={0.85}
          >
            {label}
          </text>
        )}
      </motion.g>
    </motion.g>
  )
}

function DomainOrbit({
  cx,
  cy,
  r,
  domains,
  tNow,
  frozen,
  color,
  seed,
}: {
  cx: number
  cy: number
  r: number
  domains: string[]
  tNow: number
  frozen: boolean
  color: string
  seed: number
}) {
  if (domains.length === 0) return null
  // Slow rotation, ~3s per cycle.
  const rotation = frozen ? seed * 360 : (tNow * 60 + seed * 360) % 360
  return (
    <g
      transform={`rotate(${rotation} ${cx} ${cy})`}
      opacity={frozen ? 1 : 0.85}
    >
      {domains.map((d, i) => {
        const a = (i / domains.length) * Math.PI * 2
        const x = cx + Math.cos(a) * r
        const y = cy + Math.sin(a) * r
        const w = d.length * 6.5 + 14
        return (
          <g key={d} transform={`rotate(${-rotation} ${x} ${y})`}>
            <rect
              x={x - w / 2}
              y={y - 9}
              width={w}
              height={16}
              rx={8}
              fill="#0a0a1f"
              stroke={color}
              strokeOpacity={0.5}
              strokeWidth={1}
            />
            <text
              x={x}
              y={y + 3}
              textAnchor="middle"
              fontFamily="'JetBrains Mono', ui-monospace, monospace"
              fontSize="9"
              fill={color}
              letterSpacing="0.05em"
            >
              {d}
            </text>
          </g>
        )
      })}
    </g>
  )
}

function KuGauge({
  cx,
  cy,
  r,
  ku_count,
  color,
}: {
  cx: number
  cy: number
  r: number
  ku_count: number
  color: string
}) {
  // Gauge runs from 7 o'clock to 5 o'clock (270° arc).
  const total = 300 // assume max 300 KU for arc fill
  const t = Math.min(1, ku_count / total)
  const arcDeg = 270 * t
  const startAngle = 135
  const endAngle = startAngle + arcDeg
  const toRad = (d: number) => (d * Math.PI) / 180
  const sx = cx + Math.cos(toRad(startAngle)) * r
  const sy = cy + Math.sin(toRad(startAngle)) * r
  const ex = cx + Math.cos(toRad(endAngle)) * r
  const ey = cy + Math.sin(toRad(endAngle)) * r
  const largeArc = arcDeg > 180 ? 1 : 0
  const arc =
    arcDeg < 1 ? "" : `M ${sx} ${sy} A ${r} ${r} 0 ${largeArc} 1 ${ex} ${ey}`
  const trackArc = `M ${cx + Math.cos(toRad(135)) * r} ${cy + Math.sin(toRad(135)) * r} A ${r} ${r} 0 1 1 ${cx + Math.cos(toRad(135 + 270)) * r} ${cy + Math.sin(toRad(135 + 270)) * r}`
  return (
    <g>
      <path
        d={trackArc}
        fill="none"
        stroke={color}
        strokeOpacity={0.15}
        strokeWidth={2}
      />
      {arc && (
        <path
          d={arc}
          fill="none"
          stroke={color}
          strokeOpacity={0.85}
          strokeWidth={2.5}
          strokeLinecap="round"
        />
      )}
    </g>
  )
}

function ConsentFlash({ x, y }: { x: number; y: number }) {
  return (
    <AnimatePresence>
      <motion.g
        initial={{ opacity: 0, scale: 0.2 }}
        animate={{ opacity: [0, 1, 0], scale: [0.2, 6, 8] }}
        exit={{ opacity: 0 }}
        transition={{ duration: 1.4, ease: "easeOut" }}
      >
        <circle
          cx={x}
          cy={y}
          r={40}
          fill="#FFD89B"
          filter="url(#glow-strong)"
        />
        <circle cx={x} cy={y} r={20} fill="#FFFFFF" />
      </motion.g>
    </AnimatePresence>
  )
}

function StarField() {
  // Static SVG of ~80 small stars — generated once.
  const stars = useMemo(() => {
    const arr: Array<{ x: number; y: number; r: number; o: number }> = []
    let s = 1234567
    const rnd = () => {
      s = (s * 9301 + 49297) % 233280
      return s / 233280
    }
    for (let i = 0; i < 90; i++) {
      arr.push({
        x: rnd() * 100,
        y: rnd() * 100,
        r: 0.4 + rnd() * 1.2,
        o: 0.2 + rnd() * 0.6,
      })
    }
    return arr
  }, [])
  return (
    <svg
      aria-hidden
      className="pointer-events-none absolute inset-0 h-full w-full"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
    >
      {stars.map((s, i) => (
        <circle
          key={i}
          cx={s.x}
          cy={s.y}
          r={s.r * 0.1}
          fill="#FFFFFF"
          opacity={s.o}
        />
      ))}
    </svg>
  )
}
