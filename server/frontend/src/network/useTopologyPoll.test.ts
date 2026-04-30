import { describe, expect, it, vi, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useTopologyPoll } from "./useTopologyPoll";
import { topologyFixture } from "./fixtures/topology.fixture";
import type { TopologyResponse } from "./types";

describe("useTopologyPoll", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads from fixture immediately when useFixture=true", () => {
    const { result } = renderHook(() => useTopologyPoll({ useFixture: true }));
    expect(result.current.data).toEqual(topologyFixture);
    expect(result.current.error).toBeNull();
    expect(result.current.lastUpdated).not.toBeNull();
  });

  it("calls the fetcher on mount and re-polls on the interval", async () => {
    const fetcher = vi.fn<() => Promise<TopologyResponse>>().mockResolvedValue(
      topologyFixture,
    );
    // Use a short real interval so the test stays fast.
    renderHook(() => useTopologyPoll({ fetcher, intervalMs: 30 }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 500,
    });
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(3), {
      timeout: 500,
    });
  });

  it("records error and preserves last-good data on fetch failure", async () => {
    const fetcher = vi
      .fn<() => Promise<TopologyResponse>>()
      .mockResolvedValueOnce(topologyFixture)
      .mockRejectedValue(new Error("HTTP 503"));
    const { result } = renderHook(() =>
      useTopologyPoll({ fetcher, intervalMs: 30 }),
    );
    await waitFor(() => expect(result.current.data).toEqual(topologyFixture));
    const firstUpdate = result.current.lastUpdated;
    expect(firstUpdate).not.toBeNull();

    await waitFor(() => expect(result.current.error).toBe("HTTP 503"), {
      timeout: 500,
    });
    // Last-good data is preserved through the error.
    expect(result.current.data).toEqual(topologyFixture);
    expect(result.current.lastUpdated).toBe(firstUpdate);
  });

  it("recovers and clears error on subsequent successful fetch", async () => {
    const fetcher = vi
      .fn<() => Promise<TopologyResponse>>()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(topologyFixture);
    // Longer interval ensures we observe the error state before recovery.
    const { result } = renderHook(() =>
      useTopologyPoll({ fetcher, intervalMs: 200 }),
    );
    await waitFor(() => expect(result.current.error).toBe("boom"));
    await waitFor(() => expect(result.current.data).toEqual(topologyFixture), {
      timeout: 1000,
    });
    expect(result.current.error).toBeNull();
  });

  it("stops polling after unmount", async () => {
    const fetcher = vi.fn<() => Promise<TopologyResponse>>().mockResolvedValue(
      topologyFixture,
    );
    const { unmount } = renderHook(() =>
      useTopologyPoll({ fetcher, intervalMs: 30 }),
    );
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    unmount();
    const callsAtUnmount = fetcher.mock.calls.length;
    await new Promise((r) => setTimeout(r, 150));
    expect(fetcher.mock.calls.length).toBe(callsAtUnmount);
  });
});
