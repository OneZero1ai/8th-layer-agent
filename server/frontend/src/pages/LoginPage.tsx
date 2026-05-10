/**
 * LoginPage — passkey-first login UI for the L2 admin shell (FO-1d, #199).
 *
 * Two paths in the same screen (Decision 30 §"Auth UI surfaces"):
 *
 *   1. Primary CTA — "Sign in with passkey" — calls
 *      /api/v1/auth/passkey/login/begin + WebAuthn ceremony +
 *      /api/v1/auth/passkey/login/finish via the auth context's
 *      `loginWithPasskey`.
 *   2. Secondary fallback — username + password legacy form.
 *
 * Cookie-bound session (FO-1c): both paths set the cq_session cookie
 * server-side; the browser keeps it; this page never stores a JWT in
 * localStorage. After success we read user.role from /auth/me (via
 * useAuth's restore) and redirect: enterprise_admin / l2_admin → /admin,
 * everyone else → /.
 *
 * The screen consumes the theme context so the Enterprise + L2 brand
 * land before auth — Decision 30's "first-impression contract".
 */

import { type FormEvent, useState } from "react"
import { useNavigate } from "react-router"
import { useAuth } from "../auth"
import { PoweredBy8thLayer } from "../components/PoweredBy8thLayer"
import { Wordmark } from "../components/Wordmark"
import { useTheme } from "../theme"

export function LoginPage() {
  const { login, loginWithPasskey } = useAuth()
  const { theme } = useTheme()
  const navigate = useNavigate()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [passkeyLoading, setPasskeyLoading] = useState(false)
  const [passwordLoading, setPasswordLoading] = useState(false)

  function landingForRole(role: string | null): string {
    if (role === "enterprise_admin" || role === "l2_admin") return "/admin"
    return "/"
  }

  async function handlePasskey() {
    if (!username) {
      setError("Enter your username to use a passkey.")
      return
    }
    setError(null)
    setPasskeyLoading(true)
    try {
      await loginWithPasskey(username)
      // useAuth.refresh() has already run; consult /auth/me result via
      // a fresh GET to read role for routing. Using fetch directly
      // avoids racing the AuthProvider's useEffect.
      const meResp = await fetch("/api/v1/auth/me", { credentials: "include" })
      const me = meResp.ok ? await meResp.json() : null
      navigate(landingForRole(me?.role ?? null))
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Passkey sign-in failed. Try password instead.",
      )
    } finally {
      setPasskeyLoading(false)
    }
  }

  async function handlePassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setPasswordLoading(true)
    try {
      await login(username, password)
      const meResp = await fetch("/api/v1/auth/me", { credentials: "include" })
      const me = meResp.ok ? await meResp.json() : null
      navigate(landingForRole(me?.role ?? null))
    } catch {
      setError("Invalid credentials")
    } finally {
      setPasswordLoading(false)
    }
  }

  const enterpriseName = theme?.enterprise.display_name
  const l2Label = theme?.l2.label

  return (
    <div className="min-h-screen flex flex-col items-center justify-between px-4 py-10">
      <div className="w-full max-w-sm flex-1 flex flex-col justify-center">
        <div className="flex flex-col items-center gap-4 mb-8">
          <Wordmark size="lg" />
          {(enterpriseName || l2Label) && (
            <p className="eyebrow text-center">
              {enterpriseName ?? "—"}
              {l2Label ? ` · ${l2Label}` : ""}
            </p>
          )}
        </div>

        <div className="brand-surface-raised p-7 backdrop-blur-sm shadow-[0_24px_60px_-24px_rgba(0,0,0,0.6)]">
          {error && (
            <p className="mb-4 rounded-md border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_8%,transparent)] px-3 py-2 text-center text-sm text-[var(--rose)]">
              {error}
            </p>
          )}

          {/* Username — shared between passkey and password paths. */}
          <label className="block mb-4">
            <span className="eyebrow block mb-1.5">Username</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username webauthn"
              className="brand-input w-full"
              required
            />
          </label>

          {/* Primary CTA — passkey. Always rendered first per Decision 30. */}
          <button
            type="button"
            onClick={handlePasskey}
            disabled={passkeyLoading || passwordLoading || !username}
            className="w-full rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_22%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_55%,transparent)] py-2.5 font-mono-brand text-[11px] uppercase tracking-[0.22em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_32%,transparent)] disabled:opacity-50 transition-all"
          >
            {passkeyLoading ? "Awaiting passkey…" : "Sign in with passkey"}
          </button>

          {/* OR separator */}
          <div className="my-5 flex items-center gap-3">
            <span className="flex-1 h-px bg-[var(--rule)]" />
            <span className="font-mono-brand text-[10px] uppercase tracking-[0.22em] text-[var(--ink-faint)]">
              or
            </span>
            <span className="flex-1 h-px bg-[var(--rule)]" />
          </div>

          {/* Legacy password fallback. */}
          <form onSubmit={handlePassword}>
            <label className="block mb-4">
              <span className="eyebrow block mb-1.5">Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                className="brand-input w-full"
              />
            </label>
            <button
              type="submit"
              disabled={
                passwordLoading || passkeyLoading || !username || !password
              }
              className="w-full rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] py-2 font-mono-brand text-[11px] uppercase tracking-[0.22em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"
            >
              {passwordLoading ? "Signing in…" : "Sign in with password"}
            </button>
          </form>

          <p className="mt-6 text-center text-xs text-[var(--ink-mute)] leading-relaxed">
            Don't have an account?
            <br />
            Ask your admin for an invite.
          </p>
        </div>
      </div>

      <PoweredBy8thLayer />
    </div>
  )
}
