import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";
import type { Core, ElementDefinition } from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import type { CytoElement, CytoNodeData } from "../graph";

let extensionRegistered = false;
function ensureExtension() {
  if (extensionRegistered) return;
  // cytoscape extension registration mutates global cytoscape state — do once.
  cytoscape.use(coseBilkent);
  extensionRegistered = true;
}

interface Props {
  elements: CytoElement[];
  selectedL2Id: string | null;
  onSelectL2: (id: string | null) => void;
}

export function TopologyCanvas({ elements, selectedL2Id, onSelectL2 }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);

  // Mount: build cy instance once.
  useEffect(() => {
    if (!containerRef.current) return;
    ensureExtension();

    const cy = cytoscape({
      container: containerRef.current,
      elements: elements as ElementDefinition[],
      // cose-bilkent handles compound + force layout cleanly.
      // Type isn't in cytoscape's built-in LayoutOptions union; cast.
      layout: {
        name: "cose-bilkent",
        animate: false,
        randomize: true,
        nodeRepulsion: 8000,
        idealEdgeLength: 100,
        edgeElasticity: 0.45,
        gravity: 0.4,
      } as cytoscape.LayoutOptions,
      style: [
        {
          selector: "node.l2",
          style: {
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "font-size": 11,
            "font-weight": 600,
            color: "#1f2937",
            "text-outline-color": "#fff",
            "text-outline-width": 2,
            // Size proportional to ku_count, clamped.
            width: "mapData(ku_count, 0, 300, 30, 90)",
            height: "mapData(ku_count, 0, 300, 30, 90)",
            "border-width": 2,
          },
        },
        {
          selector: "node.enterprise-orion",
          style: {
            "background-color": "#6366f1", // indigo-500
            "border-color": "#4338ca", // indigo-700
          },
        },
        {
          selector: "node.enterprise-acme",
          style: {
            "background-color": "#14b8a6", // teal-500
            "border-color": "#0f766e", // teal-700
          },
        },
        {
          selector: "node.cluster",
          style: {
            label: "data(label)",
            "text-valign": "top",
            "text-halign": "center",
            "font-size": 14,
            "font-weight": 700,
            color: "#374151",
            "background-opacity": 0.05,
            "border-width": 1,
            "border-style": "dashed",
            "border-color": "#9ca3af",
            padding: "24px",
            "background-color": "#f3f4f6",
          },
        },
        {
          selector: "node.cluster.cluster-orion",
          style: {
            "background-color": "#eef2ff", // indigo-50
            "border-color": "#a5b4fc", // indigo-300
          },
        },
        {
          selector: "node.cluster.cluster-acme",
          style: {
            "background-color": "#f0fdfa", // teal-50
            "border-color": "#5eead4", // teal-300
          },
        },
        {
          selector: "node.l2:selected",
          style: {
            "border-color": "#f59e0b", // amber-500
            "border-width": 4,
          },
        },
        {
          selector: "edge.peer-edge",
          style: {
            width: 2,
            "line-color": "#6b7280",
            "curve-style": "bezier",
            opacity: 0.7,
          },
        },
        {
          selector: "edge.cross-edge.unconsented",
          style: {
            width: 1.5,
            "line-color": "#9ca3af",
            "line-style": "dashed",
            "curve-style": "bezier",
            opacity: 0.5,
          },
        },
        {
          selector: "edge.cross-edge.consented",
          style: {
            width: 2.5,
            "line-color": "#10b981", // emerald-500
            "curve-style": "bezier",
            opacity: 0.85,
          },
        },
      ],
      wheelSensitivity: 0.2,
    });

    cy.on("tap", "node.l2", (evt) => {
      const id = evt.target.id();
      onSelectL2(id);
    });
    cy.on("tap", (evt) => {
      // Click on background clears selection.
      if (evt.target === cy) onSelectL2(null);
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync elements when topology changes.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().remove();
      cy.add(elements as ElementDefinition[]);
    });
    cy.layout({
      name: "cose-bilkent",
      animate: false,
      randomize: false,
      fit: true,
    } as cytoscape.LayoutOptions).run();
  }, [elements]);

  // Sync external selection -> cy.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().unselect();
    if (selectedL2Id) {
      const node = cy.getElementById(selectedL2Id);
      if (node && node.length > 0) node.select();
    }
  }, [selectedL2Id]);

  // Hidden test-only DOM mirror — Cytoscape draws to <canvas>, which is
  // opaque to Testing Library. Mirror the data we feed it for assertions.
  const nodeData = elements
    .map((e) => e.data)
    .filter((d): d is CytoNodeData => "kind" in d);
  const edgeData = elements.filter(
    (e) => "kind" in e.data && (e.data.kind === "peer" || e.data.kind === "cross"),
  );

  return (
    <div className="relative h-full w-full">
      <div
        ref={containerRef}
        data-testid="topology-canvas"
        className="h-full w-full bg-gray-50"
      />
      {/* Test mirror — invisible but queryable. */}
      <div data-testid="topology-mirror" className="sr-only" aria-hidden="true">
        {nodeData.map((n) => (
          <span
            key={n.id}
            data-testid={`mirror-node-${n.kind}`}
            data-node-id={n.id}
            data-enterprise={n.enterprise}
            data-group={n.group}
          >
            {n.label}
          </span>
        ))}
        {edgeData.map((e) => {
          const d = e.data as { id: string; kind: string; consented?: boolean };
          return (
            <span
              key={d.id}
              data-testid={`mirror-edge-${d.kind}`}
              data-edge-id={d.id}
              data-consented={String(!!d.consented)}
              data-classes={e.classes ?? ""}
            />
          );
        })}
      </div>
    </div>
  );
}
