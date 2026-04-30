import { useEffect, useRef, useState } from "react";
import { getToken } from "../api";
import type { TopologyResponse } from "./types";
import { topologyFixture } from "./fixtures/topology.fixture";

export interface TopologyPollState {
  data: TopologyResponse | null;
  error: string | null;
  lastUpdated: number | null; // epoch ms of last successful response
}

export interface UseTopologyPollOptions {
  intervalMs?: number;
  // Test seam — supplies an alternate fetcher (default: real fetch with auth).
  fetcher?: () => Promise<TopologyResponse>;
  // When true, skip network and use the bundled fixture. Useful for storybook/
  // demo environments where the proxy isn't reachable. Off by default.
  useFixture?: boolean;
}

const DEFAULT_INTERVAL_MS = 5000;

async function defaultFetcher(): Promise<TopologyResponse> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const resp = await fetch("/api/v1/network/topology", { headers });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}`);
  }
  return resp.json();
}

export function useTopologyPoll(
  options: UseTopologyPollOptions = {},
): TopologyPollState {
  const {
    intervalMs = DEFAULT_INTERVAL_MS,
    fetcher,
    useFixture = false,
  } = options;
  const [state, setState] = useState<TopologyPollState>(() =>
    useFixture
      ? {
          data: topologyFixture,
          error: null,
          lastUpdated: Date.now(),
        }
      : { data: null, error: null, lastUpdated: null },
  );
  // Keep latest fetcher in a ref so changing it doesn't restart the timer.
  const fetcherRef = useRef<() => Promise<TopologyResponse>>(
    fetcher ?? defaultFetcher,
  );
  useEffect(() => {
    fetcherRef.current = fetcher ?? defaultFetcher;
  }, [fetcher]);

  useEffect(() => {
    if (useFixture) return;

    let cancelled = false;

    async function tick() {
      try {
        const data = await fetcherRef.current();
        if (cancelled) return;
        setState({ data, error: null, lastUpdated: Date.now() });
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : "fetch failed";
        // Preserve last-good data + lastUpdated so the canvas doesn't blank.
        setState((prev) => ({ ...prev, error: message }));
      }
    }

    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs, useFixture]);

  return state;
}
