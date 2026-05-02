import type { TopologyResponse } from "../types"

// Static fixture mirroring the live test-fleet shape (6 L2s across 2 Enterprises).
// Used in dev/tests until Lane F's server-side proxy ships at /api/v1/network/topology.

const NOW = "2026-04-30T12:00:00Z"
const RECENT = "2026-04-30T11:59:30Z"

const ORION_L2_IDS = ["orion/engineering", "orion/solutions", "orion/gtm"]
const ACME_L2_IDS = ["acme/engineering", "acme/solutions", "acme/finance"]

function peersExcluding(allIds: string[], selfId: string) {
  return allIds
    .filter((id) => id !== selfId)
    .map((id) => ({ l2_id: id, last_signature_at: RECENT }))
}

export const topologyFixture: TopologyResponse = {
  generated_at: NOW,
  enterprises: [
    {
      enterprise: "orion",
      l2s: [
        {
          l2_id: "orion/engineering",
          group: "engineering",
          endpoint_url:
            "http://test-ori-Alb-uYtCiM8iwUDE-1537178551.us-east-1.elb.amazonaws.com",
          ku_count: 287,
          domain_count: 42,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ORION_L2_IDS, "orion/engineering"),
          active_personas: [
            {
              persona: "claude-mux-dev",
              last_seen_at: RECENT,
              working_dir_hint: "~/projects/clawrig",
              expertise_domains: ["aws", "terraform", "ecs"],
            },
          ],
        },
        {
          l2_id: "orion/solutions",
          group: "solutions",
          endpoint_url:
            "http://test-ori-Alb-iWhYcfoCeuHA-164324034.us-east-1.elb.amazonaws.com",
          ku_count: 154,
          domain_count: 28,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ORION_L2_IDS, "orion/solutions"),
          active_personas: [
            {
              persona: "solutions-architect",
              last_seen_at: RECENT,
              working_dir_hint: "~/projects/case-study",
              expertise_domains: ["customer-success", "demo"],
            },
          ],
        },
        {
          l2_id: "orion/gtm",
          group: "gtm",
          endpoint_url:
            "http://test-ori-Alb-D7CVfG04aGRc-778844735.us-east-1.elb.amazonaws.com",
          ku_count: 96,
          domain_count: 19,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ORION_L2_IDS, "orion/gtm"),
          active_personas: [
            {
              persona: "gtm-pitch-builder",
              last_seen_at: RECENT,
              working_dir_hint: "~/projects/nebula-gtm-pitch",
              expertise_domains: ["pitch-deck", "narrative"],
            },
          ],
        },
      ],
    },
    {
      enterprise: "acme",
      l2s: [
        {
          l2_id: "acme/engineering",
          group: "engineering",
          endpoint_url:
            "http://test-acm-Alb-w0Eq2rO5MeVM-1954810296.us-east-1.elb.amazonaws.com",
          ku_count: 211,
          domain_count: 35,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ACME_L2_IDS, "acme/engineering"),
          active_personas: [
            {
              persona: "acme-platform-eng",
              last_seen_at: RECENT,
              working_dir_hint: "~/work/acme-infra",
              expertise_domains: ["kubernetes", "go"],
            },
          ],
        },
        {
          l2_id: "acme/solutions",
          group: "solutions",
          endpoint_url:
            "http://test-acm-Alb-jIOoMinF94dR-73889023.us-east-1.elb.amazonaws.com",
          ku_count: 132,
          domain_count: 24,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ACME_L2_IDS, "acme/solutions"),
          active_personas: [
            {
              persona: "acme-se-east",
              last_seen_at: RECENT,
              working_dir_hint: "~/work/customer-X-poc",
              expertise_domains: ["integration", "salesforce"],
            },
          ],
        },
        {
          l2_id: "acme/finance",
          group: "finance",
          endpoint_url:
            "http://test-acm-Alb-3z1VuBmK1VDX-1994393375.us-east-1.elb.amazonaws.com",
          ku_count: 64,
          domain_count: 12,
          peer_count: 2,
          generated_at: NOW,
          peers: peersExcluding(ACME_L2_IDS, "acme/finance"),
          active_personas: [
            {
              persona: "acme-fpa",
              last_seen_at: RECENT,
              working_dir_hint: "~/work/quarterly-close",
              expertise_domains: ["quickbooks", "reporting"],
            },
          ],
        },
      ],
    },
  ],
  // No active cross-Enterprise consents in the default fixture — Lane F demo
  // toggles "Sign cross-Enterprise consent" to populate this list at runtime.
  cross_enterprise_consents: [],
}
