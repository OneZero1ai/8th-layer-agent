import { useEffect, useState } from "react"
import { Link, Outlet, useLocation } from "react-router"
import { api } from "../api"
import { useAuth } from "../auth"
import { TourLauncher } from "../tour/TourLauncher"
import { PoweredBy8thLayer } from "./PoweredBy8thLayer"
import { Wordmark } from "./Wordmark"

export function Layout() {
  const { username, logout } = useAuth()
  const location = useLocation()
  const [pendingCount, setPendingCount] = useState(0)
  const onDashboard = location.pathname === "/dashboard"
  // Network page needs a full-width main; everything else stays narrow.
  const wide = location.pathname === "/network"

  useEffect(() => {
    if (onDashboard) return
    function fetchCount() {
      api
        .reviewQueue(0, 0)
        .then((r) => setPendingCount(r.total))
        .catch(() => {})
    }
    fetchCount()
    const interval = setInterval(fetchCount, 15_000)
    return () => clearInterval(interval)
  }, [onDashboard])

  function navLink(path: string, label: string, tourId?: string) {
    const active = location.pathname === path
    return (
      <Link
        to={path}
        data-tour-target={tourId}
        className={`relative font-mono-brand text-[11px] uppercase tracking-[0.22em] py-1 whitespace-nowrap transition-colors ${
          active
            ? "text-[var(--ink)]"
            : "text-[var(--ink-mute)] hover:text-[var(--ink-dim)]"
        }`}
      >
        {label}
        {path === "/review" && pendingCount > 0 && (
          <span className="ml-2 inline-flex items-center justify-center rounded-full px-1.5 py-0.5 text-[10px] font-mono-brand text-[var(--gold)] bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] border border-[color-mix(in_srgb,var(--gold)_28%,transparent)]">
            {pendingCount}
          </span>
        )}
        {active && (
          <span className="absolute -bottom-px left-0 right-0 h-px bg-gradient-to-r from-transparent via-[var(--brand-primary)] to-transparent" />
        )}
      </Link>
    )
  }

  return (
    <div className="min-h-screen overflow-x-hidden">
      <nav className="border-b border-[var(--rule)] bg-[color-mix(in_srgb,var(--bg-via)_85%,transparent)] backdrop-blur sticky top-0 z-20">
        <div
          className={`${wide ? "w-full px-6" : "max-w-3xl mx-auto px-4"} py-3 flex items-center justify-between`}
        >
          <div className="flex items-center gap-3 md:gap-7">
            <Link to="/dashboard" className="mr-2" data-tour-target="welcome">
              <Wordmark size="md" />
            </Link>
            <span data-tour-target="group" className="contents" />
            {navLink("/review", "Review", "review")}
            {navLink("/dashboard", "Dashboard", "dashboard")}
            {navLink("/network", "Network", "network")}
            {navLink("/crosstalk", "Crosstalk", "crosstalk")}
            {navLink("/federation", "Federation", "federation")}
            {navLink("/settings/api-keys", "API Keys", "api-keys")}
            {navLink("/admin/personas", "Personas", "personas")}
          </div>
          <div className="flex items-center gap-4">
            <TourLauncher />
            <span className="hidden md:inline font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {username}
            </span>
            <button
              type="button"
              onClick={logout}
              data-tour-target="done"
              className="font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-faint)] hover:text-[var(--rose)] transition-colors"
            >
              Logout
            </button>
          </div>
        </div>
      </nav>
      <main
        className={wide ? "w-full px-0 py-0" : "max-w-3xl mx-auto py-10 px-4"}
      >
        <Outlet context={{ setPendingCount }} />
      </main>
      <PoweredBy8thLayer />
    </div>
  )
}
