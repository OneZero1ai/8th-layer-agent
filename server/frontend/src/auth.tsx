/**
 * Authentication state for the L2 admin shell (FO-1c + FO-1d).
 *
 * Login state is determined by calling `/auth/me` with `credentials:
 * "include"` so the cq_session cookie (HttpOnly, set by the server on
 * login) is the source of truth. The legacy bearer-token-in-localStorage
 * path is retired here — JS no longer holds the JWT, which closes the
 * XSS-leak vector that motivated FO-1c.
 *
 * Two login methods:
 *   - login(username, password) — legacy fallback; calls POST /auth/login.
 *     The server responds 200 + sets the cookie; we discard the token in
 *     the body and treat the cookie as authoritative.
 *   - loginWithPasskey(username) — FO-1a passkey ceremony via
 *     ./webauthn.passkeyLogin; same cookie disposition on success.
 *
 * useAuth exposes both plus { username, role, isAuthenticated, loading,
 * logout }.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react"
import { api, setToken } from "./api"
import { passkeyLogin } from "./webauthn"

interface MeResponse {
  username: string
  role?: string
}

interface AuthState {
  username: string | null
  role: string | null
  isAuthenticated: boolean
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  loginWithPasskey: (username: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthState | null>(null)

async function fetchMe(): Promise<MeResponse | null> {
  // /auth/me uses cookie credentials per FO-1c. 401 → not logged in;
  // anything else (network error, 5xx) → null with the caller deciding.
  try {
    const resp = await fetch("/api/v1/auth/me", {
      credentials: "include",
    })
    if (!resp.ok) return null
    return await resp.json()
  } catch {
    return null
  }
}

async function callLogout(): Promise<void> {
  // Best-effort POST to clear the cookie server-side. Server may not
  // expose a logout route in V1 — we still clear local state below.
  try {
    await fetch("/api/v1/auth/logout", {
      method: "POST",
      credentials: "include",
    })
  } catch {
    // ignore
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [username, setUsername] = useState<string | null>(null)
  const [role, setRole] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const me = await fetchMe()
    if (me) {
      setUsername(me.username)
      setRole(me.role ?? null)
    } else {
      setUsername(null)
      setRole(null)
    }
  }, [])

  const login = useCallback(
    async (user: string, pass: string) => {
      // Legacy password login. The server sets the cookie on success;
      // we read /auth/me to learn role + canonicalize username.
      await api.login(user, pass)
      await refresh()
    },
    [refresh],
  )

  const loginWithPasskey = useCallback(
    async (user: string) => {
      await passkeyLogin(user)
      await refresh()
    },
    [refresh],
  )

  const logout = useCallback(() => {
    setUsername(null)
    setRole(null)
    // Clear any stray bearer state from the legacy localStorage path.
    setToken(null)
    void callLogout()
  }, [])

  // On mount, attempt to read the cookie-bound session.
  useEffect(() => {
    let cancelled = false
    fetchMe()
      .then((me) => {
        if (cancelled) return
        if (me) {
          setUsername(me.username)
          setRole(me.role ?? null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <AuthContext.Provider
      value={{
        username,
        role,
        isAuthenticated: !!username,
        loading,
        login,
        loginWithPasskey,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- standard React context pattern.
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within AuthProvider")
  return ctx
}
