export interface Insight {
  summary: string
  detail: string
  action: string
}

export interface Context {
  languages: string[]
  frameworks: string[]
  pattern: string
}

export interface Evidence {
  confidence: number
  confirmations: number
  first_observed: string | null
  last_confirmed: string | null
}

export interface Flag {
  reason: "stale" | "incorrect" | "duplicate"
  timestamp: string
  detail: string | null
  duplicate_of: string | null
}

export interface KnowledgeUnit {
  id: string
  version: number
  domains: string[]
  insight: Insight
  context: Context
  evidence: Evidence
  tier: string
  created_by: string
  superseded_by: string | null
  flags: Flag[]
}

export interface ReviewItem {
  knowledge_unit: KnowledgeUnit
  status: "pending" | "approved" | "rejected"
  reviewed_by: string | null
  reviewed_at: string | null
}

export interface ReviewQueueResponse {
  items: ReviewItem[]
  total: number
  offset: number
  limit: number
}

export type Selection = "approve" | "reject" | "skip" | null

export interface ReviewDecisionResponse {
  unit_id: string
  status: "approved" | "rejected"
  reviewed_by: string
  reviewed_at: string
}

export interface ActivityEvent {
  type: "proposed" | "approved" | "rejected"
  unit_id: string
  summary: string
  reviewed_by?: string
  timestamp: string
}

export interface DailyCount {
  date: string
  proposed: number
  approved: number
  rejected: number
}

export interface ReviewStatsResponse {
  counts: { pending: number; approved: number; rejected: number }
  domains: Record<string, number>
  confidence_distribution: Record<string, number>
  recent_activity: ActivityEvent[]
  trends: { daily: DailyCount[] }
}

export interface ApiKeyPublic {
  id: string
  name: string
  labels: string[]
  prefix: string
  ttl: string
  expires_at: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
  is_expired: boolean
  is_active: boolean
}

export interface CreatedApiKey extends ApiKeyPublic {
  token: string
}

export interface ApiKeysList {
  data: ApiKeyPublic[]
  count: number
}

export interface MessageResponse {
  message: string
}

// Activity log row — wire shape mirrors backend ActivityRow in
// activity_routes.py. Used both directly (timeline view) and as the
// source-of-truth for deriving the persona directory in the absence of
// a dedicated /admin/personas endpoint.
export interface ActivityRow {
  id: string
  ts: string
  tenant_enterprise: string
  tenant_group: string | null
  persona: string | null
  human: string | null
  event_type: string
  payload: Record<string, unknown>
  result_summary: Record<string, unknown> | null
  thread_or_chain_id: string | null
}

export interface ActivityListResponse {
  items: ActivityRow[]
  count: number
  next_cursor: string | null
}

// Derived persona summary — assembled client-side from /activity rows
// and (optionally) /review/units. Once a backend /admin/personas
// endpoint lands, swap this for the wire shape and delete the
// derivation in PersonasPage.
export type PersonaStatus = "active" | "idle" | "departed" | "suspended"

export interface PersonaSummary {
  name: string
  group: string | null
  enterprise: string
  status: PersonaStatus
  joined: string | null
  last_seen: string | null
  ku_count: number
  api_key_count: number
}
