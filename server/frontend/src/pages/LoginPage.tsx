import { type FormEvent, useState } from "react"
import { useAuth } from "../auth"
import { Wordmark } from "../components/Wordmark"

export function LoginPage() {
  const { login } = useAuth()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await login(username, password)
    } catch {
      setError("Invalid credentials")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center gap-6 mb-8">
          <Wordmark size="lg" />
          <p className="eyebrow">Layer 8 · Admin Console</p>
        </div>
        <form
          onSubmit={handleSubmit}
          className="brand-surface-raised p-7 backdrop-blur-sm shadow-[0_24px_60px_-24px_rgba(0,0,0,0.6)]"
        >
          {error && (
            <p className="mb-4 rounded-md border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_8%,transparent)] px-3 py-2 text-center text-sm text-[var(--rose)]">
              {error}
            </p>
          )}
          <label className="block mb-4">
            <span className="eyebrow block mb-1.5">Username</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              className="brand-input w-full"
              required
            />
          </label>
          <label className="block mb-7">
            <span className="eyebrow block mb-1.5">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="brand-input w-full"
              required
            />
          </label>
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] py-2.5 font-mono-brand text-[11px] uppercase tracking-[0.22em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="mt-6 text-center font-mono-brand text-[10px] uppercase tracking-[0.2em] text-[var(--ink-faint)]">
          8th-layer.ai · semantic knowledge layer
        </p>
      </div>
    </div>
  )
}
