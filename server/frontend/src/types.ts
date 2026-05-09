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

export type PersonaName = "admin" | "viewer" | "agent" | "external-collaborator"

export interface PersonaAssignment {
  username: string
  email: string | null
  persona: PersonaName
  assigned_at: string
  assigned_by: string
  disabled_at: string | null
}

export interface PersonaListResponse {
  items: PersonaAssignment[]
  total: number
  offset: number
  limit: number
}

export interface CreatePersonaRequest {
  username: string
  email: string
  persona: PersonaName
}

export interface CreatePersonaResponse {
  assignment: PersonaAssignment
  invite_sent: boolean
}

export interface PatchPersonaRequest {
  persona: PersonaName
}

export interface PatchPersonaResponse {
  assignment: PersonaAssignment
}

// ---------------------------------------------------------------------------
// Invites (FO-1b backend, P2 frontend)
// ---------------------------------------------------------------------------

export type InviteRole = "enterprise_admin" | "l2_admin" | "user"

export type InviteStatus = "pending" | "claimed" | "expired" | "revoked"

export interface InvitePublic {
  id: number
  email: string
  role: InviteRole
  target_l2_id: string | null
  issued_by: number
  issued_at: string
  expires_at: string
  claimed_at: string | null
  claimed_by: number | null
  revoked_at: string | null
  status: InviteStatus
}

export interface InvitesPublic {
  data: InvitePublic[]
  count: number
}

export interface CreateInviteRequest {
  email: string
  role: InviteRole
  target_l2_id?: string | null
  enterprise_name?: string
}

// Activity log row — wire shape mirrors backend ActivityRow in
// activity_routes.py. The crosstalk tab uses it to derive the
// cross-Enterprise consult outbox from consult_open events.
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

// ---------------------------------------------------------------------------
// Crosstalk threads (#171). Wire shapes mirror crosstalk_routes.py:
//   - ThreadSummary           (GET /crosstalk/threads)
//   - CrosstalkThread + msgs  (GET /crosstalk/threads/{id})
// ---------------------------------------------------------------------------

export interface CrosstalkThreadSummary {
  id: string
  subject: string
  status: string
  created_at: string
  created_by_username: string
  participants: string[]
}

export interface CrosstalkThreadListResponse {
  items: CrosstalkThreadSummary[]
  count: number
}

export interface CrosstalkThread {
  id: string
  subject: string
  status: string
  closed_at: string | null
  closed_by_username: string | null
  closed_reason: string | null
  enterprise_id: string
  group_id: string
  created_at: string
  created_by_username: string
  participants: string[]
}

export interface CrosstalkMessage {
  id: string
  thread_id: string
  from_username: string
  from_persona: string | null
  to_username: string | null
  content: string
  sent_at: string
  read_at: string | null
}

export interface CrosstalkThreadWithMessages {
  thread: CrosstalkThread
  messages: CrosstalkMessage[]
}

// ---------------------------------------------------------------------------
// Cross-Enterprise consults (#171). Wire shapes mirror consults.py:
//   - ConsultThreadOut        (GET /consults/inbox.threads)
//   - ConsultMessageOut       (GET /consults/{id}/messages.messages)
// ---------------------------------------------------------------------------

export interface ConsultThread {
  thread_id: string
  from_l2_id: string
  from_persona: string
  to_l2_id: string
  to_persona: string
  subject: string | null
  status: string
  claimed_by: string | null
  created_at: string
  closed_at: string | null
  resolution_summary: string | null
}

export interface ConsultInboxResponse {
  self_l2_id: string
  self_persona: string
  threads: ConsultThread[]
}

export interface ConsultMessage {
  message_id: string
  thread_id: string
  from_l2_id: string
  from_persona: string
  content: string
  created_at: string
}

export interface ConsultMessagesResponse {
  thread_id: string
  messages: ConsultMessage[]
}
