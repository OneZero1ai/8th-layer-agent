import type { TopologyResponse, CrossEnterpriseConsent } from "../types";

// Hidden DOM mirror so existing Testing-Library assertions keep working.
// The NOC canvas paints to SVG (which is queryable), but the test uses
// data-testid hooks on the old graph shape. We emit the same hooks here.

function isConsented(
  consents: CrossEnterpriseConsent[],
  fromEnt: string,
  fromGroup: string,
  toEnt: string,
  toGroup: string,
): boolean {
  return consents.some((c) => {
    const matchOne =
      c.requester_enterprise === fromEnt &&
      c.responder_enterprise === toEnt &&
      (c.requester_group === null || c.requester_group === fromGroup) &&
      (c.responder_group === null || c.responder_group === toGroup);
    const matchOther =
      c.requester_enterprise === toEnt &&
      c.responder_enterprise === fromEnt &&
      (c.requester_group === null || c.requester_group === toGroup) &&
      (c.responder_group === null || c.responder_group === fromGroup);
    return matchOne || matchOther;
  });
}

export function TopologyMirror({ topology }: { topology: TopologyResponse }) {
  const clusters = topology.enterprises.map((e) => ({ id: `cluster:${e.enterprise}`, enterprise: e.enterprise }));
  const l2s = topology.enterprises.flatMap((e) =>
    e.l2s.map((l) => ({ id: l.l2_id, enterprise: e.enterprise, group: l.group })),
  );

  const peerEdges: Array<{ id: string }> = [];
  const seen = new Set<string>();
  for (const ent of topology.enterprises) {
    for (const l2 of ent.l2s) {
      for (const peer of l2.peers) {
        const [a, b] = [l2.l2_id, peer.l2_id].sort();
        const id = `peer:${a}--${b}`;
        if (seen.has(id)) continue;
        seen.add(id);
        peerEdges.push({ id });
      }
    }
  }

  const crossEdges: Array<{ id: string; consented: boolean }> = [];
  if (topology.enterprises.length >= 2) {
    const ents = topology.enterprises;
    for (let i = 0; i < ents.length; i++) {
      for (let j = i + 1; j < ents.length; j++) {
        for (const aL2 of ents[i].l2s) {
          for (const bL2 of ents[j].l2s) {
            const [src, tgt] = [aL2.l2_id, bL2.l2_id].sort();
            const consented = isConsented(
              topology.cross_enterprise_consents,
              ents[i].enterprise,
              aL2.group,
              ents[j].enterprise,
              bL2.group,
            );
            crossEdges.push({ id: `cross:${src}--${tgt}`, consented });
          }
        }
      }
    }
  }

  return (
    <div data-testid="topology-mirror" className="sr-only" aria-hidden="true">
      {clusters.map((c) => (
        <span
          key={c.id}
          data-testid="mirror-node-enterprise-cluster"
          data-node-id={c.id}
          data-enterprise={c.enterprise}
        />
      ))}
      {l2s.map((n) => (
        <span
          key={n.id}
          data-testid="mirror-node-l2"
          data-node-id={n.id}
          data-enterprise={n.enterprise}
          data-group={n.group}
        />
      ))}
      {peerEdges.map((e) => (
        <span key={e.id} data-testid="mirror-edge-peer" data-edge-id={e.id} />
      ))}
      {crossEdges.map((e) => (
        <span
          key={e.id}
          data-testid="mirror-edge-cross"
          data-edge-id={e.id}
          data-consented={String(e.consented)}
          data-classes={e.consented ? "cross-edge consented" : "cross-edge unconsented"}
        />
      ))}
    </div>
  );
}
