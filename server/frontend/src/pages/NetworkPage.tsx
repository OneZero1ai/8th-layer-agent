import { useMemo, useState } from "react";
import { useTopologyPoll } from "../network/useTopologyPoll";
import { buildElements } from "../network/graph";
import { TopologyCanvas } from "../network/components/TopologyCanvas";
import { L2DetailPanel } from "../network/components/L2DetailPanel";
import { DemoControls } from "../network/components/DemoControls";
import type { TopologyL2, TopologyResponse } from "../network/types";

interface NetworkPageProps {
  // Test seam — production code never passes this.
  initialData?: TopologyResponse;
}

function findL2(
  topology: TopologyResponse | null,
  l2_id: string | null,
): TopologyL2 | null {
  if (!topology || !l2_id) return null;
  for (const ent of topology.enterprises) {
    for (const l2 of ent.l2s) {
      if (l2.l2_id === l2_id) return l2;
    }
  }
  return null;
}

function lastUpdatedLabel(lastUpdated: number | null): string {
  if (!lastUpdated) return "never";
  const seconds = Math.max(0, Math.floor((Date.now() - lastUpdated) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ago`;
}

export function NetworkPage({ initialData }: NetworkPageProps = {}) {
  const poll = useTopologyPoll({ useFixture: !!initialData });
  // Tests can preempt the polling result so they don't have to mock fetch.
  const data = initialData ?? poll.data;
  const [selectedL2Id, setSelectedL2Id] = useState<string | null>(null);

  const elements = useMemo(
    () => (data ? buildElements(data) : []),
    [data],
  );
  const selectedL2 = findL2(data, selectedL2Id);

  return (
    // 100vh minus the 49px nav bar (py-3 + line height ~25px).
    <div
      className="relative flex flex-col"
      style={{ height: "calc(100vh - 49px)" }}
    >
      {/* Top bar with title + last-updated */}
      <div className="flex items-center justify-between border-b border-gray-200 bg-white px-5 py-2">
        <div>
          <h1 className="text-base font-semibold text-gray-900">
            Live network topology
          </h1>
          <p className="text-xs text-gray-500">
            Multi-Enterprise AIGRP peer-mesh — polled every 5s
          </p>
        </div>
        <div
          data-testid="last-updated"
          className="flex items-center gap-2 text-xs text-gray-500"
        >
          {poll.error && (
            <span
              className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-700"
              title={poll.error}
            >
              fetch error
            </span>
          )}
          <span>updated {lastUpdatedLabel(poll.lastUpdated)}</span>
        </div>
      </div>

      {/* Canvas + slide-out detail */}
      <div className="relative flex-1 overflow-hidden">
        {!data ? (
          <div
            data-testid="topology-empty"
            className="flex h-full items-center justify-center text-sm text-gray-400"
          >
            {poll.error ? "topology unavailable" : "loading topology…"}
          </div>
        ) : (
          <TopologyCanvas
            elements={elements}
            selectedL2Id={selectedL2Id}
            onSelectL2={setSelectedL2Id}
          />
        )}
        <L2DetailPanel
          l2={selectedL2}
          onClose={() => setSelectedL2Id(null)}
        />
      </div>

      <DemoControls />
    </div>
  );
}
