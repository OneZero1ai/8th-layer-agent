import { render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { topologyFixture } from "../network/fixtures/topology.fixture"
import { NetworkPage } from "./NetworkPage"

// NocCanvas uses requestAnimationFrame + SVG sub-pixel things happy-dom doesn't
// fully implement. Mock to a passthrough so we can assert via TopologyMirror.
vi.mock("../network/components/NocCanvas", () => ({
  NocCanvas: () => <div data-testid="topology-canvas" />,
}))

// Width gate would otherwise block the page in happy-dom (default 1024 width).
// Force the gate open by mocking innerWidth.
beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { writable: true, value: 1440 })
})

describe("NetworkPage", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(topologyFixture),
    }) as unknown as typeof fetch
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("renders 2 Enterprise clusters and 6 L2 nodes from the fixture", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    const clusterNodes = screen.getAllByTestId("mirror-node-enterprise-cluster")
    expect(clusterNodes).toHaveLength(2)
    const enterprises = clusterNodes.map((n) => n.dataset.enterprise)
    expect(enterprises).toEqual(expect.arrayContaining(["orion", "acme"]))

    const l2Nodes = screen.getAllByTestId("mirror-node-l2")
    expect(l2Nodes).toHaveLength(6)
  })

  it("renders cross-Enterprise edges as unconsented dashed lines (no consents in fixture)", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    const crossEdges = screen.getAllByTestId("mirror-edge-cross")
    // 3 orion L2s × 3 acme L2s = 9 cross edges
    expect(crossEdges).toHaveLength(9)
    for (const e of crossEdges) {
      expect(e.dataset.consented).toBe("false")
      expect(e.dataset.classes).toMatch(/unconsented/)
    }
  })

  it("renders same-Enterprise peer-mesh edges (3 per enterprise = 6 total)", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    const peerEdges = screen.getAllByTestId("mirror-edge-peer")
    // each Enterprise: 3 nodes -> 3 unique pairs -> 3 edges; 2 enterprises = 6
    expect(peerEdges).toHaveLength(6)
  })

  it("renders the demo controls strip with 3 wired buttons", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    const controls = screen.getByTestId("demo-controls")
    const buttons = controls.querySelectorAll("button")
    expect(buttons).toHaveLength(3)
    // Tier 3: buttons are wired (no longer disabled). Verify the three IDs.
    const ids = Array.from(buttons).map((b) =>
      b.getAttribute("data-demo-button"),
    )
    expect(ids).toEqual(
      expect.arrayContaining([
        "run-cross-group",
        "try-cross-enterprise",
        "sign-consent",
      ]),
    )
  })

  it("shows the NOC top bar and last-updated indicator", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    expect(screen.getByTestId("noc-topbar")).toBeInTheDocument()
    expect(screen.getByTestId("last-updated")).toBeInTheDocument()
  })

  it("includes the L1/L2/L3 layer rail with L2 active by default", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    expect(screen.getByTestId("layer-rail")).toBeInTheDocument()
    expect(screen.getByTestId("layer-L1")).toBeInTheDocument()
    expect(screen.getByTestId("layer-L2")).toBeInTheDocument()
    expect(screen.getByTestId("layer-L3")).toBeDisabled()
  })

  it("includes the DSN search bar and event ticker", () => {
    render(<NetworkPage initialData={topologyFixture} />)
    expect(screen.getByTestId("dsn-search")).toBeInTheDocument()
    expect(screen.getByTestId("event-ticker")).toBeInTheDocument()
  })
})
