import type { CreateL2Request, CreateL2Response } from "./l2wizard/types"
import type {
  ActivityListResponse,
  ApiKeysList,
  ConsultInboxResponse,
  ConsultMessagesResponse,
  CreatedApiKey,
  CreateInviteRequest,
  CreatePersonaRequest,
  CreatePersonaResponse,
  CrosstalkThreadListResponse,
  CrosstalkThreadWithMessages,
  InvitePublic,
  InviteStatus,
  InvitesPublic,
  MessageResponse,
  PatchPersonaRequest,
  PatchPersonaResponse,
  PersonaListResponse,
  ReviewDecisionResponse,
  ReviewItem,
  ReviewQueueResponse,
  ReviewStatsResponse,
} from "./types"

const API_BASE = "/api/v1"
const LEGACY_TOKEN_KEY = "cq_auth_token"

/**
 * One-shot cleanup of pre-FO-1d localStorage bearer (#199, 8l-reviewer HIGH).
 * Run once at module load so any stale token left by an older session is
 * cleared. From FO-1d forward the `cq_session` HttpOnly cookie is the only
 * auth substrate for human users; agent api-keys (`cqa.v1.*`) are sent via
 * the dedicated CLI and never touch the browser.
 */
if (typeof localStorage !== "undefined") {
  localStorage.removeItem(LEGACY_TOKEN_KEY)
}

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

let onUnauthorized: (() => void) | null = null

export function setOnUnauthorized(callback: () => void) {
  onUnauthorized = callback
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  }
  // FO-1d (#199): cookie-only auth. The `cq_session` HttpOnly cookie set by
  // FO-1c travels automatically via `credentials: "include"`. Bearer tokens
  // in localStorage were the XSS-leak vector FO-1c was meant to close, so
  // we no longer attach an Authorization header from JS-reachable storage.
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    credentials: "include",
  })
  if (!resp.ok) {
    if (resp.status === 401 && onUnauthorized) {
      onUnauthorized()
    }
    const body = await resp.json().catch(() => ({}))
    throw new ApiError(resp.status, body.detail || `HTTP ${resp.status}`)
  }
  if (resp.status === 204) {
    return undefined as T
  }
  return resp.json()
}

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; username: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  me: () => request<{ username: string; created_at: string }>("/auth/me"),

  reviewQueue: (limit = 20, offset = 0) =>
    request<ReviewQueueResponse>(
      `/review/queue?limit=${limit}&offset=${offset}`,
    ),

  approve: (unitId: string) =>
    request<ReviewDecisionResponse>(`/review/${unitId}/approve`, {
      method: "POST",
    }),

  reject: (unitId: string) =>
    request<ReviewDecisionResponse>(`/review/${unitId}/reject`, {
      method: "POST",
    }),

  reviewStats: () => request<ReviewStatsResponse>("/review/stats"),

  getUnit: (unitId: string) => request<ReviewItem>(`/review/${unitId}`),

  listUnits: (params: {
    domain?: string
    confidence_min?: number
    confidence_max?: number
    status?: string
  }) => {
    const qs = new URLSearchParams()
    if (params.domain) qs.set("domain", params.domain)
    if (params.confidence_min != null)
      qs.set("confidence_min", String(params.confidence_min))
    if (params.confidence_max != null)
      qs.set("confidence_max", String(params.confidence_max))
    if (params.status) qs.set("status", params.status)
    const query = qs.toString()
    return request<ReviewItem[]>(`/review/units${query ? `?${query}` : ""}`)
  },

  listApiKeys: () => request<ApiKeysList>("/auth/api-keys"),

  createApiKey: (name: string, ttl: string, labels: string[] = []) =>
    request<CreatedApiKey>("/auth/api-keys", {
      method: "POST",
      body: JSON.stringify({ name, ttl, labels }),
    }),

  revokeApiKey: (id: string) =>
    request<MessageResponse>(`/auth/api-keys/${id}/revoke`, { method: "POST" }),

  listPersonas: (limit = 50, offset = 0) =>
    request<PersonaListResponse>(
      `/admin/personas?limit=${limit}&offset=${offset}`,
    ),

  createPersona: (body: CreatePersonaRequest) =>
    request<CreatePersonaResponse>("/admin/personas", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  patchPersona: (username: string, body: PatchPersonaRequest) =>
    request<PatchPersonaResponse>(`/admin/personas/${username}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  disablePersona: (username: string) =>
    request<MessageResponse>(`/admin/personas/${username}/disable`, {
      method: "POST",
    }),

  listInvites: (status?: InviteStatus) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : ""
    return request<InvitesPublic>(`/admin/invites${query}`)
  },

  createInvite: (body: CreateInviteRequest) =>
    request<InvitePublic>("/admin/invites", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  revokeInvite: (id: number) =>
    request<InvitePublic>(`/admin/invites/${id}`, {
      method: "DELETE",
    }),

  // Activity-log read. The crosstalk tab uses it to derive the
  // cross-Enterprise consult outbox from consult_open events.
  listActivity: (
    params: {
      persona?: string
      since?: string
      until?: string
      event_type?: string
      limit?: number
      cursor?: string
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (params.persona) qs.set("persona", params.persona)
    if (params.since) qs.set("since", params.since)
    if (params.until) qs.set("until", params.until)
    if (params.event_type) qs.set("event_type", params.event_type)
    if (params.limit != null) qs.set("limit", String(params.limit))
    if (params.cursor) qs.set("cursor", params.cursor)
    const query = qs.toString()
    return request<ActivityListResponse>(`/activity${query ? `?${query}` : ""}`)
  },

  // Crosstalk threads (#171). The list endpoint returns ThreadSummary
  // (compact shape — no message-count or last-message timestamp). The
  // per-thread fetch returns the full CrosstalkThread + messages, which
  // the drawer uses to render the timeline.
  listCrosstalkThreads: (limit = 100) =>
    request<CrosstalkThreadListResponse>(`/crosstalk/threads?limit=${limit}`),

  getCrosstalkThread: (threadId: string, limit = 200) =>
    request<CrosstalkThreadWithMessages>(
      `/crosstalk/threads/${encodeURIComponent(threadId)}?limit=${limit}`,
    ),

  closeCrosstalkThread: (threadId: string, reason?: string) =>
    request<{ thread_id: string; status: string }>(
      `/crosstalk/threads/${encodeURIComponent(threadId)}/close`,
      {
        method: "POST",
        body: JSON.stringify({ reason: reason ?? null }),
      },
    ),

  // Cross-Enterprise consults (#171). Inbox = consults addressed to this
  // L2; outbox endpoint does not exist on the backend yet (see issue
  // surfaced from #171 — derive from /activity event_type=consult_open
  // for now, or wait for the dedicated endpoint).
  consultInbox: (includeClosed = true, limit = 100) =>
    request<ConsultInboxResponse>(
      `/consults/inbox?include_closed=${includeClosed}&limit=${limit}`,
    ),

  consultMessages: (threadId: string) =>
    request<ConsultMessagesResponse>(
      `/consults/${encodeURIComponent(threadId)}/messages`,
    ),

  // FO-3 (agent#193 / Decision 32) — Create-L2 wizard.
  //
  // createL2 posts the wizard's {l2_slug, description, aws_region} to the
  // cq-server L2-provision proxy (PR #292). The proxy resolves the caller's
  // Enterprise + AWS binding server-side; the browser never sends those.
  // The 202 response carries the SSE `stream_url` the progress step opens.
  createL2: (body: CreateL2Request) =>
    request<CreateL2Response>("/admin/l2s", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // checkL2SlugAvailable does a debounced live availability probe for the
  // wizard's name step. The cq-server proxy's slug-availability route is not
  // part of PR #292 (the create call is the authoritative uniqueness check —
  // it 409s L2_SLUG_TAKEN). Until a dedicated GET lands, a 404 (route absent)
  // resolves to `unknown` so the UI falls back to client-side regex
  // validation rather than hard-blocking the wizard. A 409 means taken; a
  // 2xx body's `available` flag is honoured when the route does exist.
  checkL2SlugAvailable: async (
    slug: string,
  ): Promise<"available" | "taken" | "unknown"> => {
    try {
      const resp = await request<{ available?: boolean }>(
        `/admin/l2s/slug-available?slug=${encodeURIComponent(slug)}`,
      )
      if (resp && resp.available === false) return "taken"
      return "available"
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) return "taken"
      // 404 → route not deployed; anything else → treat as unknown so the
      // wizard never hard-blocks on a probe that may not exist yet.
      return "unknown"
    }
  },
}

export { ApiError }
