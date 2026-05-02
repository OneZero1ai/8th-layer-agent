// Demo trace fixtures for the three packet-trace scenarios.
// Match the contract of POST /api/v1/network/demo/{scenario} — array of trace
// events plus a final_results payload.

export interface TraceEvent {
  step: number
  ts_offset_ms: number
  l2_id: string
  action: string
  payload_preview: string
  result_summary: string
  latency_ms: number
}

export interface RedactedKuResult {
  ku_id: string
  l2_id: string
  domain_tags: string[]
  // For summary-only policy: title + a short summary, with redacted body.
  title: string
  summary: string
  body: string | null // null = redacted
  policy: "direct" | "summary_only" | "blocked"
  reason?: string
}

export interface DemoTraceResponse {
  scenario:
    | "cross-group-query"
    | "cross-enterprise-blocked"
    | "cross-enterprise-consented"
  total_latency_ms: number
  events: TraceEvent[]
  final_results: RedactedKuResult[]
}

export const crossGroupQueryFixture: DemoTraceResponse = {
  scenario: "cross-group-query",
  total_latency_ms: 287,
  events: [
    {
      step: 1,
      ts_offset_ms: 0,
      l2_id: "acme/engineering",
      action: "intake",
      payload_preview: "intent: cloudfront origin failover",
      result_summary: "registered query · 14 local KUs preview",
      latency_ms: 12,
    },
    {
      step: 2,
      ts_offset_ms: 14,
      l2_id: "acme/engineering",
      action: "aigrp_lookup",
      payload_preview: "signature: a14f7c2 (cosine top-k=3)",
      result_summary: "best ext = acme/solutions @ 0.78",
      latency_ms: 38,
    },
    {
      step: 3,
      ts_offset_ms: 54,
      l2_id: "acme/solutions",
      action: "forward_query",
      payload_preview: "GET /xgroup/query?from=acme/engineering",
      result_summary: "policy=cross_group_summary · 3 candidate KUs",
      latency_ms: 91,
    },
    {
      step: 4,
      ts_offset_ms: 148,
      l2_id: "acme/solutions",
      action: "redact",
      payload_preview: "summary_only — strip body, keep title + tags",
      result_summary: "3 KUs redacted",
      latency_ms: 22,
    },
    {
      step: 5,
      ts_offset_ms: 172,
      l2_id: "acme/engineering",
      action: "respond",
      payload_preview: "200 OK — 3 results",
      result_summary: "delivered to caller",
      latency_ms: 18,
    },
  ],
  final_results: [
    {
      ku_id: "ku_8c41a",
      l2_id: "acme/solutions",
      domain_tags: ["cloudfront", "edge", "failover"],
      title: "CloudFront origin-shield + secondary origin failover ladder",
      summary:
        "Pattern for two-tier failover using origin-shield as health-anchor.",
      body: null,
      policy: "summary_only",
    },
    {
      ku_id: "ku_119bd",
      l2_id: "acme/solutions",
      domain_tags: ["cloudfront", "ttl", "stale-while-revalidate"],
      title: "Stale-if-error TTL tuning for B2B dashboard tiles",
      summary:
        "Long stale-if-error window stabilises dashboards through origin blips.",
      body: null,
      policy: "summary_only",
    },
    {
      ku_id: "ku_3edd2",
      l2_id: "acme/solutions",
      domain_tags: ["cloudfront", "lambda-edge"],
      title: "Lambda@Edge auth-token rewrite — viewer-request gotcha",
      summary:
        "Trailing slash in path triggers double rewrite if not normalised.",
      body: null,
      policy: "summary_only",
    },
  ],
}

export const crossEnterpriseBlockedFixture: DemoTraceResponse = {
  scenario: "cross-enterprise-blocked",
  total_latency_ms: 64,
  events: [
    {
      step: 1,
      ts_offset_ms: 0,
      l2_id: "orion/engineering",
      action: "intake",
      payload_preview: "intent: cloudfront origin failover",
      result_summary: "registered query · 6 local KUs preview",
      latency_ms: 11,
    },
    {
      step: 2,
      ts_offset_ms: 13,
      l2_id: "orion/engineering",
      action: "aigrp_lookup",
      payload_preview: "signature: a14f7c2 — cross-enterprise candidate",
      result_summary: "best ext = acme/engineering @ 0.91 (other enterprise)",
      latency_ms: 28,
    },
    {
      step: 3,
      ts_offset_ms: 43,
      l2_id: "orion/engineering",
      action: "consent_check",
      payload_preview:
        "consent_record(orion ↔ acme, engineering ↔ engineering)",
      result_summary: "no active consent — BLOCKED",
      latency_ms: 9,
    },
    {
      step: 4,
      ts_offset_ms: 54,
      l2_id: "orion/engineering",
      action: "respond",
      payload_preview: "403 cross_enterprise_blocked",
      result_summary: "no results — boundary held",
      latency_ms: 8,
    },
  ],
  final_results: [
    {
      ku_id: "ku_blocked_1",
      l2_id: "acme/engineering",
      domain_tags: ["cloudfront"],
      title: "[hidden — cross-Enterprise policy: blocked]",
      summary: "[hidden — cross-Enterprise policy: blocked]",
      body: null,
      policy: "blocked",
      reason: "no active consent record (orion ↔ acme)",
    },
  ],
}

export const crossEnterpriseConsentedFixture: DemoTraceResponse = {
  scenario: "cross-enterprise-consented",
  total_latency_ms: 318,
  events: [
    {
      step: 1,
      ts_offset_ms: 0,
      l2_id: "orion/engineering",
      action: "intake",
      payload_preview: "intent: cloudfront origin failover",
      result_summary: "registered query · 6 local KUs preview",
      latency_ms: 12,
    },
    {
      step: 2,
      ts_offset_ms: 14,
      l2_id: "orion/engineering",
      action: "aigrp_lookup",
      payload_preview: "signature: a14f7c2 — cross-enterprise candidate",
      result_summary: "best ext = acme/engineering @ 0.91",
      latency_ms: 32,
    },
    {
      step: 3,
      ts_offset_ms: 48,
      l2_id: "orion/engineering",
      action: "consent_check",
      payload_preview:
        "consent_record(orion ↔ acme, engineering ↔ engineering)",
      result_summary: "active · policy=summary_only",
      latency_ms: 11,
    },
    {
      step: 4,
      ts_offset_ms: 61,
      l2_id: "acme/engineering",
      action: "forward_query",
      payload_preview: "GET /xenterprise/query (signed JWT)",
      result_summary: "3 candidate KUs · summary_only",
      latency_ms: 124,
    },
    {
      step: 5,
      ts_offset_ms: 187,
      l2_id: "acme/engineering",
      action: "redact",
      payload_preview: "summary_only — strip body, keep title + tags",
      result_summary: "3 KUs redacted",
      latency_ms: 28,
    },
    {
      step: 6,
      ts_offset_ms: 217,
      l2_id: "orion/engineering",
      action: "respond",
      payload_preview: "200 OK — 3 results · cross-enterprise consented",
      result_summary: "delivered to caller",
      latency_ms: 21,
    },
  ],
  final_results: [
    {
      ku_id: "ku_8c41a",
      l2_id: "acme/engineering",
      domain_tags: ["cloudfront", "edge", "failover"],
      title: "CloudFront origin-shield + secondary origin failover ladder",
      summary:
        "Pattern for two-tier failover using origin-shield as health-anchor.",
      body: null,
      policy: "summary_only",
    },
    {
      ku_id: "ku_2ef41",
      l2_id: "acme/engineering",
      domain_tags: ["cloudfront", "route53", "health-check"],
      title: "Route53 health-check thresholds for edge-failover triggers",
      summary:
        "3-of-5 thresholds avoid flapping for typical B2B traffic shapes.",
      body: null,
      policy: "summary_only",
    },
    {
      ku_id: "ku_77a9b",
      l2_id: "acme/engineering",
      domain_tags: ["cloudfront", "wcu", "rate-limit"],
      title: "WAF WCU budget interaction with rate-based rules at edge",
      summary: "Rate-based rules consume disproportionate WCU at high TPS.",
      body: null,
      policy: "summary_only",
    },
  ],
}

export const demoTraceFixtures = {
  "cross-group-query": crossGroupQueryFixture,
  "cross-enterprise-blocked": crossEnterpriseBlockedFixture,
  "cross-enterprise-consented": crossEnterpriseConsentedFixture,
} as const

export type DemoScenario = keyof typeof demoTraceFixtures
