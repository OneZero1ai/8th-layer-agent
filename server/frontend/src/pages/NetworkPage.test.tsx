import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { NetworkPage } from "./NetworkPage";
import { topologyFixture } from "../network/fixtures/topology.fixture";

// Cytoscape touches canvas APIs that happy-dom doesn't fully implement,
// so we mock the canvas component and rely on the props it receives.
vi.mock("../network/components/TopologyCanvas", () => ({
  TopologyCanvas: ({
    elements,
  }: {
    elements: Array<{
      data: Record<string, unknown>;
      classes?: string;
    }>;
  }) => (
    <div data-testid="topology-canvas">
      {elements.map((e) => {
        const data = e.data as {
          id: string;
          kind?: string;
          enterprise?: string;
          source?: string;
          target?: string;
          consented?: boolean;
        };
        if (data.kind === "l2" || data.kind === "enterprise-cluster") {
          return (
            <span
              key={data.id}
              data-testid={`mirror-node-${data.kind}`}
              data-node-id={data.id}
              data-enterprise={data.enterprise}
            />
          );
        }
        if (data.kind === "peer" || data.kind === "cross") {
          return (
            <span
              key={data.id}
              data-testid={`mirror-edge-${data.kind}`}
              data-edge-id={data.id}
              data-consented={String(!!data.consented)}
              data-classes={e.classes ?? ""}
            />
          );
        }
        return null;
      })}
    </div>
  ),
}));

describe("NetworkPage", () => {
  beforeEach(() => {
    // Stub fetch so the polling hook never makes real requests if
    // initialData isn't supplied for some reason.
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(topologyFixture),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders 2 Enterprise clusters and 6 L2 nodes from the fixture", () => {
    render(<NetworkPage initialData={topologyFixture} />);
    const clusterNodes = screen.getAllByTestId("mirror-node-enterprise-cluster");
    expect(clusterNodes).toHaveLength(2);
    const enterprises = clusterNodes.map((n) => n.dataset.enterprise);
    expect(enterprises).toEqual(expect.arrayContaining(["orion", "acme"]));

    const l2Nodes = screen.getAllByTestId("mirror-node-l2");
    expect(l2Nodes).toHaveLength(6);
  });

  it("renders cross-Enterprise edges as unconsented dashed lines (no consents in fixture)", () => {
    render(<NetworkPage initialData={topologyFixture} />);
    const crossEdges = screen.getAllByTestId("mirror-edge-cross");
    // 3 orion L2s × 3 acme L2s = 9 cross edges
    expect(crossEdges).toHaveLength(9);
    for (const e of crossEdges) {
      expect(e.dataset.consented).toBe("false");
      expect(e.dataset.classes).toMatch(/unconsented/);
    }
  });

  it("renders same-Enterprise peer-mesh edges (3 per enterprise = 6 total)", () => {
    render(<NetworkPage initialData={topologyFixture} />);
    const peerEdges = screen.getAllByTestId("mirror-edge-peer");
    // each Enterprise: 3 nodes -> 3 unique pairs -> 3 edges; 2 enterprises = 6
    expect(peerEdges).toHaveLength(6);
  });

  it("renders the demo controls strip with 3 disabled buttons", () => {
    render(<NetworkPage initialData={topologyFixture} />);
    const controls = screen.getByTestId("demo-controls");
    const buttons = controls.querySelectorAll("button");
    expect(buttons).toHaveLength(3);
    for (const b of buttons) {
      expect(b).toBeDisabled();
      expect(b.title).toBe("Wired in Lane F");
    }
  });

  it("shows the page header and last-updated indicator", () => {
    render(<NetworkPage initialData={topologyFixture} />);
    expect(screen.getByText(/live network topology/i)).toBeInTheDocument();
    expect(screen.getByTestId("last-updated")).toBeInTheDocument();
  });
});
