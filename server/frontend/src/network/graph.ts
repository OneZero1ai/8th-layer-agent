import type {
  CrossEnterpriseConsent,
  TopologyResponse,
} from "./types";

export interface CytoNodeData {
  id: string;
  label: string;
  enterprise: string;
  group: string;
  ku_count: number;
  parent?: string;
  // 'enterprise-cluster' nodes are compound parents; 'l2' nodes are L2 endpoints.
  kind: "enterprise-cluster" | "l2";
}

export interface CytoEdgeData {
  id: string;
  source: string;
  target: string;
  // 'peer' = same-Enterprise AIGRP mesh edge.
  // 'cross' = cross-Enterprise edge (consented or unconsented).
  kind: "peer" | "cross";
  consented: boolean;
}

export interface CytoElement {
  data: CytoNodeData | CytoEdgeData;
  classes?: string;
}

function consentMatches(
  consent: CrossEnterpriseConsent,
  reqEnt: string,
  reqGroup: string,
  resEnt: string,
  resGroup: string,
): boolean {
  if (consent.requester_enterprise !== reqEnt) return false;
  if (consent.responder_enterprise !== resEnt) return false;
  if (consent.requester_group !== null && consent.requester_group !== reqGroup)
    return false;
  if (consent.responder_group !== null && consent.responder_group !== resGroup)
    return false;
  return true;
}

function isConsented(
  consents: CrossEnterpriseConsent[],
  fromEnt: string,
  fromGroup: string,
  toEnt: string,
  toGroup: string,
): boolean {
  // Either direction (requester->responder or responder->requester) counts.
  return consents.some(
    (c) =>
      consentMatches(c, fromEnt, fromGroup, toEnt, toGroup) ||
      consentMatches(c, toEnt, toGroup, fromEnt, fromGroup),
  );
}

/**
 * Build Cytoscape elements from a TopologyResponse.
 *
 * - Each Enterprise becomes a compound parent node (cluster).
 * - Each L2 becomes a child node.
 * - Same-Enterprise peer edges drawn as solid 'peer' edges (deduped).
 * - Cross-Enterprise edges drawn between every L2 pair across enterprises;
 *   `consented` flag controls styling. Deduped so each pair appears once.
 */
export function buildElements(topology: TopologyResponse): CytoElement[] {
  const elements: CytoElement[] = [];

  for (const ent of topology.enterprises) {
    const parentId = `cluster:${ent.enterprise}`;
    elements.push({
      data: {
        id: parentId,
        label: ent.enterprise.toUpperCase(),
        enterprise: ent.enterprise,
        group: "",
        ku_count: 0,
        kind: "enterprise-cluster",
      },
      classes: `cluster cluster-${ent.enterprise}`,
    });

    for (const l2 of ent.l2s) {
      elements.push({
        data: {
          id: l2.l2_id,
          label: l2.group,
          enterprise: ent.enterprise,
          group: l2.group,
          ku_count: l2.ku_count,
          parent: parentId,
          kind: "l2",
        },
        classes: `l2 enterprise-${ent.enterprise}`,
      });
    }
  }

  // Same-Enterprise peer edges (dedupe via canonical sort).
  const seenPeerEdges = new Set<string>();
  for (const ent of topology.enterprises) {
    for (const l2 of ent.l2s) {
      for (const peer of l2.peers) {
        const [a, b] = [l2.l2_id, peer.l2_id].sort();
        const edgeId = `peer:${a}--${b}`;
        if (seenPeerEdges.has(edgeId)) continue;
        seenPeerEdges.add(edgeId);
        elements.push({
          data: {
            id: edgeId,
            source: a,
            target: b,
            kind: "peer",
            consented: true,
          },
          classes: "peer-edge",
        });
      }
    }
  }

  // Cross-Enterprise edges — every pair of L2s across distinct enterprises.
  if (topology.enterprises.length >= 2) {
    const ents = topology.enterprises;
    for (let i = 0; i < ents.length; i++) {
      for (let j = i + 1; j < ents.length; j++) {
        const a = ents[i];
        const b = ents[j];
        for (const aL2 of a.l2s) {
          for (const bL2 of b.l2s) {
            const consented = isConsented(
              topology.cross_enterprise_consents,
              a.enterprise,
              aL2.group,
              b.enterprise,
              bL2.group,
            );
            const [src, tgt] = [aL2.l2_id, bL2.l2_id].sort();
            elements.push({
              data: {
                id: `cross:${src}--${tgt}`,
                source: src,
                target: tgt,
                kind: "cross",
                consented,
              },
              classes: consented ? "cross-edge consented" : "cross-edge unconsented",
            });
          }
        }
      }
    }
  }

  return elements;
}
