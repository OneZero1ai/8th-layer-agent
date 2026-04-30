// Static DSN resolve fixture — used when /api/v1/network/dsn/resolve is unreachable.
// Mirrors the "I need help with CloudFront" intent path through the test-fleet shape.

export interface DsnResolutionStep {
  step: string;
  ts_offset_ms: number;
  detail: string;
}

export interface DsnCandidate {
  l2_id: string;
  enterprise: string;
  group: string;
  sim_score: number;
  ku_count_in_topic: number;
  expert_personas: string[];
  policy_if_queried: "direct" | "cross_group_summary" | "cross_enterprise_blocked" | "summary_only";
}

export interface DsnResolveResponse {
  intent: string;
  embedding_preview: number[]; // 16-dim sparkline vector
  resolution_path: DsnResolutionStep[];
  candidates: DsnCandidate[];
}

const cloudFrontEmbedding = [
  0.18, -0.42, 0.61, 0.07, -0.31, 0.55, 0.22, -0.18,
  0.39, -0.05, 0.48, -0.27, 0.16, 0.33, -0.44, 0.52,
];

export function dsnFixtureFor(intent: string): DsnResolveResponse {
  return {
    intent,
    embedding_preview: cloudFrontEmbedding,
    resolution_path: [
      { step: "embed", ts_offset_ms: 18, detail: "Bedrock Titan v2 — 1024d → preview 16d" },
      { step: "fan_out", ts_offset_ms: 42, detail: "AIGRP signature lookup × 6 L2s" },
      { step: "rank", ts_offset_ms: 134, detail: "cosine-sim top-K=3" },
      { step: "policy_overlay", ts_offset_ms: 152, detail: "consent + group policy applied" },
    ],
    candidates: [
      {
        l2_id: "acme/engineering",
        enterprise: "acme",
        group: "engineering",
        sim_score: 0.91,
        ku_count_in_topic: 14,
        expert_personas: ["acme-platform-eng", "edge-strangler"],
        policy_if_queried: "cross_enterprise_blocked",
      },
      {
        l2_id: "orion/solutions",
        enterprise: "orion",
        group: "solutions",
        sim_score: 0.84,
        ku_count_in_topic: 9,
        expert_personas: ["solutions-architect"],
        policy_if_queried: "cross_group_summary",
      },
      {
        l2_id: "orion/engineering",
        enterprise: "orion",
        group: "engineering",
        sim_score: 0.71,
        ku_count_in_topic: 6,
        expert_personas: ["claude-mux-dev"],
        policy_if_queried: "direct",
      },
      {
        l2_id: "acme/solutions",
        enterprise: "acme",
        group: "solutions",
        sim_score: 0.46,
        ku_count_in_topic: 3,
        expert_personas: ["acme-se-east"],
        policy_if_queried: "cross_enterprise_blocked",
      },
      {
        l2_id: "orion/gtm",
        enterprise: "orion",
        group: "gtm",
        sim_score: 0.22,
        ku_count_in_topic: 1,
        expert_personas: [],
        policy_if_queried: "cross_group_summary",
      },
      {
        l2_id: "acme/finance",
        enterprise: "acme",
        group: "finance",
        sim_score: 0.08,
        ku_count_in_topic: 0,
        expert_personas: [],
        policy_if_queried: "cross_enterprise_blocked",
      },
    ],
  };
}
