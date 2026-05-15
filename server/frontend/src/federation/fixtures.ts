// Federation tab — synthetic data shaped like what the directory will
// eventually return. Used both for tests and as a graceful fallback when the
// backend endpoint isn't deployed yet (same pattern as NetworkPage's demoTrace
// fixtures).

import type {
  ActivePeering,
  DailyHealthPoint,
  FederationView,
  MeshHealth,
  OutgoingOffer,
  PendingOffer,
} from "./types"

const ISO_NOW = "2026-05-09T12:00:00Z"
const ISO = (offsetDays: number, hour = 12) => {
  const d = new Date("2026-05-09T00:00:00Z")
  d.setUTCDate(d.getUTCDate() + offsetDays)
  d.setUTCHours(hour, 0, 0, 0)
  return d.toISOString()
}

function timelineFor(seed: number, baseline: number): DailyHealthPoint[] {
  const out: DailyHealthPoint[] = []
  for (let i = -29; i <= 0; i++) {
    // Deterministic pseudo-random that stays comfortably in [0,1].
    const wobble =
      Math.sin((i + seed) * 0.7) * 0.05 + Math.cos((i + seed) * 0.3) * 0.04
    const success_rate = Math.max(0, Math.min(1, baseline + wobble))
    const outbound = 14 + Math.floor(Math.abs(Math.sin(i * 0.5 + seed)) * 12)
    const inbound = 9 + Math.floor(Math.abs(Math.cos(i * 0.4 + seed)) * 10)
    out.push({
      date: ISO(i).slice(0, 10),
      success_rate,
      outbound,
      inbound,
    })
  }
  return out
}

const ACME: ActivePeering = {
  peering_id: "peer-acme-001",
  peer: { enterprise_id: "aaisn:acme-corp", display_name: "Acme Corp" },
  direction: "offered-by-us",
  status: "active",
  effective_from: ISO(-180),
  expires_at: ISO(180),
  topic_filters: ["devops", "incident-response", "platform-engineering"],
  outbound_success_rate_7d: 0.97,
  inbound_consults_7d: 42,
  last_round_trip_at: ISO(0, 11),
  reachability: {
    ours: {
      endpoint: "l2.orion.8th-layer.ai:8443",
      status: "ok",
      last_green: ISO(0, 11),
      last_red: ISO(-12),
    },
    theirs: {
      endpoint: "l2.acme.8th-layer.ai:8443",
      status: "ok",
      last_green: ISO(0, 11),
      last_red: null,
    },
  },
  signing_keys: {
    ours: {
      fingerprint: "sha256:9c4f…2a81",
      algorithm: "ed25519",
      rotated_at: ISO(-90),
    },
    theirs: {
      fingerprint: "sha256:1f02…be37",
      algorithm: "ed25519",
      rotated_at: ISO(-30),
    },
  },
  offer_timeline: [
    {
      ts: ISO(-200),
      kind: "offered",
      by_human: "david@orion",
      detail: "scope: devops + incident-response",
    },
    {
      ts: ISO(-198),
      kind: "accepted",
      by_human: "amelia@acme",
      detail: "summary-only policy",
    },
    {
      ts: ISO(-30),
      kind: "rotated",
      by_human: "amelia@acme",
      detail: "ed25519 key rotation",
    },
  ],
  consult_log: [
    { status: "success", count: 312 },
    { status: "blocked", count: 8 },
    { status: "timeout", count: 4 },
    { status: "error", count: 1 },
  ],
  health_timeline_30d: timelineFor(1, 0.95),
  silently_broken: null,
}

const NEBULA: ActivePeering = {
  peering_id: "peer-nebula-002",
  peer: {
    enterprise_id: "aaisn:nebula-research",
    display_name: "Nebula Research",
  },
  direction: "offered-by-them",
  status: "expiring-soon",
  effective_from: ISO(-340),
  expires_at: ISO(18),
  topic_filters: ["ml-research", "data-platform"],
  outbound_success_rate_7d: 0.83,
  inbound_consults_7d: 17,
  last_round_trip_at: ISO(0, 9),
  reachability: {
    ours: {
      endpoint: "l2.orion.8th-layer.ai:8443",
      status: "ok",
      last_green: ISO(0, 9),
      last_red: ISO(-3),
    },
    theirs: {
      endpoint: "l2.nebula.8th-layer.ai:8443",
      status: "warn",
      last_green: ISO(-1, 22),
      last_red: ISO(0, 4),
      detail: "intermittent 502s last 24h",
    },
  },
  signing_keys: {
    ours: {
      fingerprint: "sha256:9c4f…2a81",
      algorithm: "ed25519",
      rotated_at: ISO(-90),
    },
    theirs: {
      fingerprint: "sha256:abf3…d12c",
      algorithm: "ed25519",
      rotated_at: ISO(-220),
    },
  },
  offer_timeline: [
    {
      ts: ISO(-340),
      kind: "offered",
      by_human: "lin@nebula",
    },
    {
      ts: ISO(-339),
      kind: "accepted",
      by_human: "david@orion",
    },
  ],
  consult_log: [
    { status: "success", count: 84 },
    { status: "blocked", count: 0 },
    { status: "timeout", count: 14 },
    { status: "error", count: 3 },
  ],
  health_timeline_30d: timelineFor(2, 0.84),
  silently_broken: null,
}

const PHANTOM: ActivePeering = {
  peering_id: "peer-phantom-003",
  peer: { enterprise_id: "aaisn:phantom-labs", display_name: "Phantom Labs" },
  direction: "offered-by-us",
  status: "active",
  effective_from: ISO(-60),
  expires_at: ISO(305),
  topic_filters: ["security-research"],
  outbound_success_rate_7d: 0.12,
  inbound_consults_7d: 0,
  last_round_trip_at: ISO(-9),
  reachability: {
    ours: {
      endpoint: "l2.orion.8th-layer.ai:8443",
      status: "ok",
      last_green: ISO(0, 11),
      last_red: null,
    },
    theirs: {
      endpoint: "l2.phantom.8th-layer.ai:8443",
      status: "fail",
      last_green: ISO(-9),
      last_red: ISO(0, 11),
      detail: "TLS handshake: SNI 'l2.phantom' ≠ cert CN 'phantom-labs.io'",
    },
  },
  signing_keys: {
    ours: {
      fingerprint: "sha256:9c4f…2a81",
      algorithm: "ed25519",
      rotated_at: ISO(-90),
    },
    theirs: {
      fingerprint: "sha256:7dd1…0a04",
      algorithm: "ed25519",
      rotated_at: ISO(-410),
    },
  },
  offer_timeline: [
    { ts: ISO(-60), kind: "offered", by_human: "david@orion" },
    { ts: ISO(-59), kind: "accepted", by_human: "kira@phantom" },
  ],
  consult_log: [
    { status: "success", count: 4 },
    { status: "blocked", count: 0 },
    { status: "timeout", count: 28 },
    { status: "error", count: 17 },
  ],
  health_timeline_30d: timelineFor(3, 0.18),
  silently_broken: "sni-mismatch",
}

const SAMPLE_PENDING: PendingOffer[] = [
  {
    offer_id: "offer-incoming-aurora",
    peer: { enterprise_id: "aaisn:aurora-bio", display_name: "Aurora Bio" },
    offered_at: ISO(-2, 14),
    topic_filters: ["genomics", "regulated-data"],
    content_policy: "summary-only",
    signature_fingerprint: "sha256:cc12…ee9d",
  },
  {
    offer_id: "offer-incoming-helix",
    peer: { enterprise_id: "aaisn:helix-co", display_name: "Helix Co" },
    offered_at: ISO(-5, 10),
    topic_filters: ["fintech-compliance"],
    content_policy: "blocked-default",
    signature_fingerprint: "sha256:ad77…1f02",
  },
]

const SAMPLE_OUTGOING: OutgoingOffer[] = [
  {
    offer_id: "offer-outgoing-zenith",
    peer: { enterprise_id: "aaisn:zenith-ai", display_name: "Zenith AI" },
    offered_at: ISO(-1, 9),
    status: "pending",
    topic_filters: ["llm-safety"],
  },
  {
    offer_id: "offer-outgoing-quasar",
    peer: { enterprise_id: "aaisn:quasar-labs", display_name: "Quasar Labs" },
    offered_at: ISO(-14, 16),
    status: "expired",
    topic_filters: ["quantum-research"],
  },
]

function meshHealthFrom(peerings: ActivePeering[]): MeshHealth {
  const daily: MeshHealth["daily"] = []
  for (let i = -29; i <= 0; i++) {
    const date = ISO(i).slice(0, 10)
    let success = 0
    let blocked = 0
    let timeout = 0
    let error = 0
    for (const p of peerings) {
      const point = p.health_timeline_30d.find((d) => d.date === date)
      if (!point) continue
      const total = point.outbound + point.inbound
      success += Math.round(total * point.success_rate)
      const failure = total - Math.round(total * point.success_rate)
      blocked += Math.round(failure * 0.4)
      timeout += Math.round(failure * 0.4)
      error += failure - Math.round(failure * 0.4) - Math.round(failure * 0.4)
    }
    daily.push({ date, success, blocked, timeout, error })
  }
  const heatmap: MeshHealth["heatmap"] = peerings.map((p) => ({
    peer: p.peer,
    days: p.health_timeline_30d.map((d) => ({
      date: d.date,
      success_rate: d.success_rate,
    })),
  }))
  const alarms = peerings
    .filter((p) => p.outbound_success_rate_7d < 0.5)
    .map((p) => ({
      peering_id: p.peering_id,
      peer: p.peer,
      threshold: 0.9,
      current_rate: p.outbound_success_rate_7d,
      since: ISO(-9),
    }))
  return { daily, heatmap, alarms }
}

const ACTIVE_PEERINGS = [ACME, NEBULA, PHANTOM]

export const FEDERATION_FIXTURE: FederationView = {
  this_enterprise: {
    enterprise_id: "aaisn:orion-onezero1",
    display_name: "Orion / OneZero1",
  },
  active: ACTIVE_PEERINGS,
  pending: SAMPLE_PENDING,
  outgoing: SAMPLE_OUTGOING,
  mesh_health: meshHealthFrom(ACTIVE_PEERINGS),
  generated_at: ISO_NOW,
}

export const EMPTY_FEDERATION_FIXTURE: FederationView = {
  this_enterprise: {
    enterprise_id: "aaisn:orion-onezero1",
    display_name: "Orion / OneZero1",
  },
  active: [],
  pending: [],
  outgoing: [],
  mesh_health: { daily: [], heatmap: [], alarms: [] },
  generated_at: ISO_NOW,
}
