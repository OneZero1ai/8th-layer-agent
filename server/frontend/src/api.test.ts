import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api } from "./api"

const LEGACY_TOKEN_KEY = "cq_auth_token"

describe("api auth substrate (FO-1d, post-#199)", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("does NOT export setToken / getToken — those are FO-1c-superseded", async () => {
    // The bearer-in-localStorage path was the XSS leak FO-1c was meant to
    // close. FO-1d completes the migration: the JS auth surface is the
    // cq_session HttpOnly cookie. There is no legitimate reason to expose
    // a getToken/setToken in this module.
    const mod = await import("./api")
    expect("setToken" in mod).toBe(false)
    expect("getToken" in mod).toBe(false)
  })

  it("clears legacy localStorage bearer on module load", async () => {
    // Simulate a stale token left by a pre-FO-1d session.
    localStorage.setItem(LEGACY_TOKEN_KEY, "stale-token-from-old-release")
    // Re-import to retrigger the one-shot cleanup.
    vi.resetModules()
    await import("./api")
    expect(localStorage.getItem(LEGACY_TOKEN_KEY)).toBeNull()
  })

  it("does NOT attach Authorization header from localStorage", async () => {
    // Even if something writes to localStorage (a third-party lib, a
    // future-bug, an XSS injection), no Authorization header should be
    // attached by the request() wrapper.
    localStorage.setItem(LEGACY_TOKEN_KEY, "tampered-token")
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ username: "alice", created_at: "x" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    await api.me()
    const callArgs = fetchMock.mock.calls[0]
    const headers = (callArgs[1]?.headers as Record<string, string>) ?? {}
    expect(headers.Authorization).toBeUndefined()
  })

  it("attaches credentials: 'include' so the cq_session cookie travels", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ username: "alice", created_at: "x" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    await api.me()
    const callArgs = fetchMock.mock.calls[0]
    expect(callArgs[1]?.credentials).toBe("include")
  })
})
