// Federation tab — read-only fetcher with fixture fallback.
//
// Canonical endpoint when the backend lands:
//   GET  /api/v1/directory/peerings/{enterprise_id}
//   POST /api/v1/directory/peerings/{enterprise_id}/offers/{offer_id}/{accept|decline|withdraw}
//
// Until the directory exposes per-peering consult-history reads (see backend
// gap surfaced in agent#172 issue body), the page composes drawer fixtures
// locally. Same graceful-fallback shape as NetworkPage's runDemo.

import { FEDERATION_FIXTURE } from "./fixtures"
import type { FederationView } from "./types"

const FED_PATH = "/api/v1/directory/peerings/self"

export async function fetchFederationView(): Promise<{
  data: FederationView
  fromFixture: boolean
}> {
  try {
    const resp = await fetch(FED_PATH, {
      headers: { "Content-Type": "application/json" },
    })
    if (resp.ok) {
      const data = (await resp.json()) as FederationView
      return { data, fromFixture: false }
    }
  } catch {
    // Network error or backend not yet wired — fall through to fixture.
  }
  return { data: FEDERATION_FIXTURE, fromFixture: true }
}

export async function actOnOffer(
  offer_id: string,
  action: "accept" | "decline" | "withdraw",
): Promise<{ ok: boolean }> {
  try {
    const resp = await fetch(
      `/api/v1/directory/peerings/self/offers/${offer_id}/${action}`,
      { method: "POST", headers: { "Content-Type": "application/json" } },
    )
    return { ok: resp.ok }
  } catch {
    return { ok: false }
  }
}
