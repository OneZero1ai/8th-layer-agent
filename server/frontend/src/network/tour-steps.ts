// Onboarding tour steps. Kept in a separate module so the tour component
// stays compatible with React Fast Refresh (which requires a file to export
// components only).

export interface TourStep {
  title: string;
  body: string;
  highlightNodes?: string[];
  highlightEdges?: string[];
  zoomTo?: { cx: number; cy: number; scale: number } | null;
}

export const TOUR_STEPS: TourStep[] = [
  {
    title: "Welcome — this is the topology.",
    body: "6 L2s in 2 Enterprises. Each L2 is a cq Remote running on AWS Fargate. Same image, marketplace deploy.",
    highlightNodes: [],
  },
  {
    title: "L2 — the semantic commons.",
    body: "Every L2 holds local KUs, embeddings, and AIGRP signatures. Sized by KU count, glow grows with activity.",
    highlightNodes: ["orion/engineering"],
    zoomTo: { cx: 660, cy: 450, scale: 1.25 },
  },
  {
    title: "AIGRP = BGP for agent fleets.",
    body: "Solid edges are AIGRP peer-mesh signatures — gossip-propagated cosine summaries. Particles flow at the rate of fresh activity.",
    highlightEdges: [
      "peer:orion/engineering--orion/solutions",
      "peer:orion/engineering--orion/gtm",
      "peer:orion/gtm--orion/solutions",
    ],
    zoomTo: { cx: 460, cy: 450, scale: 1.1 },
  },
  {
    title: "DSN = DNS for intent.",
    body: "Type a need. The resolver embeds it, fans out across 6 L2s, returns a ranked candidate list with policy already overlaid.",
    highlightNodes: [],
    zoomTo: null,
  },
  {
    title: "Cross-Enterprise = hard isolation by default.",
    body: "Dashed grey edges between Enterprises carry nothing. No KU body ever leaves a tenant boundary unless consent says so.",
    highlightEdges: ["cross:acme/engineering--orion/engineering"],
    zoomTo: { cx: 800, cy: 450, scale: 0.95 },
  },
  {
    title: "Sign a consent — open a redacted bridge.",
    body: "A signed consent record opens a summary-only path. Edge solidifies in amber, particles begin flowing across it.",
    highlightEdges: ["cross:acme/engineering--orion/engineering"],
    zoomTo: { cx: 800, cy: 450, scale: 0.95 },
  },
  {
    title: "Now you. Click around.",
    body: "Click an L2 for detail. Run a demo from the bottom strip. Press SPACE to replay this tour, ESC to skip.",
    highlightNodes: [],
    zoomTo: null,
  },
];
