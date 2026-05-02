// Topology API contract — server-side proxy at /api/v1/network/topology
// (proxy itself is Lane F; this page consumes the contract)

export interface TopologyPeerEdge {
  l2_id: string
  last_signature_at: string | null
}

export interface TopologyActivePersona {
  persona: string
  last_seen_at: string
  working_dir_hint: string | null
  expertise_domains: string[]
}

export interface TopologyL2 {
  l2_id: string
  group: string
  endpoint_url: string
  ku_count: number
  domain_count: number
  peer_count: number
  generated_at: string | null
  peers: TopologyPeerEdge[]
  active_personas: TopologyActivePersona[]
}

export interface TopologyEnterprise {
  enterprise: string
  l2s: TopologyL2[]
}

export interface CrossEnterpriseConsent {
  requester_enterprise: string
  responder_enterprise: string
  requester_group: string | null
  responder_group: string | null
  policy: "summary_only" | "full_body"
  expires_at: string | null
}

export interface TopologyResponse {
  generated_at: string
  enterprises: TopologyEnterprise[]
  cross_enterprise_consents: CrossEnterpriseConsent[]
}
