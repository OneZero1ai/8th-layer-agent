// Deterministic NOC layout for the 6-L2 / 2-Enterprise topology.
//
// Force-directed layout produces unstable, ugly positioning at this scale.
// A hand-tuned hexagonal twin-cluster reads as "designed" and gives us
// stable anchor points for animations, tour highlights, and packet traces.
//
// Coordinate space: a 1600×900 logical canvas, scaled into a viewBox.
// Two clusters orbit the centerline; consent edges pass through the gap.

import type { TopologyL2, TopologyResponse } from "./types"

export interface NodePosition {
  l2_id: string
  enterprise: string
  group: string
  x: number
  y: number
  ku_count: number
  domain_count: number
  peer_count: number
}

export interface ClusterBounds {
  enterprise: string
  cx: number
  cy: number
  rx: number
  ry: number
}

export interface LaidOutTopology {
  nodes: NodePosition[]
  clusters: ClusterBounds[]
  bounds: { width: number; height: number }
}

export const CANVAS_WIDTH = 1600
export const CANVAS_HEIGHT = 900

const ORION_CENTER_X = 460
const ACME_CENTER_X = 1140
const CLUSTER_CENTER_Y = 450
const TRIANGLE_RADIUS = 200

// Each cluster: 3 L2s arranged on a triangle, with the apex pointing toward
// the centerline so cross-Enterprise edges read as bridges across the gap.
const ORION_GROUP_OFFSETS: Record<string, { dx: number; dy: number }> = {
  engineering: { dx: TRIANGLE_RADIUS, dy: 0 }, // apex toward acme
  solutions: { dx: -TRIANGLE_RADIUS / 2, dy: -TRIANGLE_RADIUS * 0.866 },
  gtm: { dx: -TRIANGLE_RADIUS / 2, dy: TRIANGLE_RADIUS * 0.866 },
}

const ACME_GROUP_OFFSETS: Record<string, { dx: number; dy: number }> = {
  engineering: { dx: -TRIANGLE_RADIUS, dy: 0 }, // apex toward orion
  solutions: { dx: TRIANGLE_RADIUS / 2, dy: -TRIANGLE_RADIUS * 0.866 },
  finance: { dx: TRIANGLE_RADIUS / 2, dy: TRIANGLE_RADIUS * 0.866 },
}

function fallbackOffset(index: number): { dx: number; dy: number } {
  const angle = (index * 2 * Math.PI) / 3 - Math.PI / 2
  return {
    dx: TRIANGLE_RADIUS * Math.cos(angle),
    dy: TRIANGLE_RADIUS * Math.sin(angle),
  }
}

function offsetFor(
  enterprise: string,
  group: string,
  index: number,
): { dx: number; dy: number } {
  const table =
    enterprise === "orion" ? ORION_GROUP_OFFSETS : ACME_GROUP_OFFSETS
  return table[group] ?? fallbackOffset(index)
}

function centerXFor(enterprise: string, isFirst: boolean): number {
  if (enterprise === "orion") return ORION_CENTER_X
  if (enterprise === "acme") return ACME_CENTER_X
  return isFirst ? ORION_CENTER_X : ACME_CENTER_X
}

export function layoutTopology(topology: TopologyResponse): LaidOutTopology {
  const nodes: NodePosition[] = []
  const clusters: ClusterBounds[] = []

  topology.enterprises.forEach((ent, entIdx) => {
    const cx = centerXFor(ent.enterprise, entIdx === 0)
    const cy = CLUSTER_CENTER_Y

    ent.l2s.forEach((l2: TopologyL2, idx) => {
      const offset = offsetFor(ent.enterprise, l2.group, idx)
      nodes.push({
        l2_id: l2.l2_id,
        enterprise: ent.enterprise,
        group: l2.group,
        x: cx + offset.dx,
        y: cy + offset.dy,
        ku_count: l2.ku_count,
        domain_count: l2.domain_count,
        peer_count: l2.peer_count,
      })
    })

    clusters.push({
      enterprise: ent.enterprise,
      cx,
      cy,
      rx: TRIANGLE_RADIUS + 130,
      ry: TRIANGLE_RADIUS + 130,
    })
  })

  return {
    nodes,
    clusters,
    bounds: { width: CANVAS_WIDTH, height: CANVAS_HEIGHT },
  }
}

export function nodeRadius(ku_count: number): number {
  // Map ku_count [0, 300] → radius [42, 84]. Logarithmic-ish feel via sqrt.
  const t = Math.min(1, Math.sqrt(ku_count / 300))
  return 42 + t * 42
}

export function findNode(
  layout: LaidOutTopology,
  l2_id: string,
): NodePosition | null {
  return layout.nodes.find((n) => n.l2_id === l2_id) ?? null
}
