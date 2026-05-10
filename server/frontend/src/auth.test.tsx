import { render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { AuthProvider, useAuth } from "./auth"

const originalFetch = globalThis.fetch

interface MockResponse {
  ok: boolean
  status: number
  json: () => Promise<object>
}

function mockFetch(response: object, status = 200): MockResponse {
  const ok = status >= 200 && status < 300
  const r: MockResponse = {
    ok,
    status,
    json: () => Promise.resolve(response),
  }
  globalThis.fetch = vi.fn().mockResolvedValue(r)
  return r
}

function AuthStatus() {
  const { isAuthenticated, username, loading, role } = useAuth()
  return (
    <div>
      <span data-testid="status">
        {isAuthenticated ? "authenticated" : "unauthenticated"}
      </span>
      <span data-testid="username">{username ?? ""}</span>
      <span data-testid="role">{role ?? ""}</span>
      <span data-testid="loading">{String(loading)}</span>
    </div>
  )
}

describe("AuthProvider — cookie-bound session (FO-1c + FO-1d)", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it("restores session from /auth/me cookie on mount", async () => {
    mockFetch({ username: "alice", role: "user", created_at: "2024-01-01" })

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("authenticated")
    })
    expect(screen.getByTestId("username")).toHaveTextContent("alice")
    expect(screen.getByTestId("role")).toHaveTextContent("user")
    // /auth/me must be called with credentials so the HttpOnly cookie travels.
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({ credentials: "include" }),
    )
  })

  it("treats 401 from /auth/me as unauthenticated (no cookie or expired)", async () => {
    mockFetch({ detail: "Missing or invalid session cookie" }, 401)

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId("loading")).toHaveTextContent("false")
    })
    expect(screen.getByTestId("status")).toHaveTextContent("unauthenticated")
  })

  it("reports loading while /auth/me is in-flight", async () => {
    let resolveFetch!: (value: MockResponse) => void
    globalThis.fetch = vi.fn().mockReturnValue(
      new Promise<MockResponse>((resolve) => {
        resolveFetch = resolve
      }),
    )

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    )

    expect(screen.getByTestId("loading")).toHaveTextContent("true")
    expect(screen.getByTestId("status")).toHaveTextContent("unauthenticated")

    resolveFetch({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          username: "alice",
          role: "user",
          created_at: "2024-01-01",
        }),
    })

    await waitFor(() => {
      expect(screen.getByTestId("loading")).toHaveTextContent("false")
    })
    expect(screen.getByTestId("status")).toHaveTextContent("authenticated")
  })
})
