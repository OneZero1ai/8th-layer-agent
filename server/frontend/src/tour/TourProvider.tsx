/**
 * Tour state machine + persistence.
 *
 * One global tour at a time. The provider:
 *   - Pulls tour-state from `/api/v1/users/me/tour-state` on mount.
 *   - Decides whether to auto-fire (no `completed_at` AND no `dismissed_at`).
 *   - Owns `currentIndex` and the public actions: start, next, dismiss, replay.
 *   - PUTs the current state to the server on every transition so refresh
 *     resumes mid-tour rather than restarting (and a teammate logging in
 *     on a second device sees "you've already done this").
 *
 * Why a context (not a hook with internal state): two consumers — the
 * overlay (renders the spotlight) and the launcher (the `?` button in
 * the header). One state, two readers — exactly what context is for.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react"
import { useNavigate } from "react-router"
import { useAuth } from "../auth"
import { TOUR_STEPS, type TourStep } from "./steps"

interface TourState {
  completed_at: string | null
  dismissed_at: string | null
  current_step: number
}

interface TourContextValue {
  active: boolean
  currentStep: TourStep | null
  currentIndex: number
  total: number
  start: () => void
  next: () => void
  dismiss: () => void
}

const TourContext = createContext<TourContextValue | null>(null)

const API = "/api/v1/users/me/tour-state"

async function fetchTourState(): Promise<TourState> {
  const resp = await fetch(API, { credentials: "include" })
  if (!resp.ok) {
    // Fresh / 401 / network — treat as a never-shown tour. The overlay
    // will not auto-fire unless we explicitly say "OK to fire" via the
    // local active flag, which only flips when we read a real state row.
    return { completed_at: null, dismissed_at: null, current_step: 0 }
  }
  return resp.json()
}

async function putTourState(state: Partial<TourState>): Promise<void> {
  await fetch(API, {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state),
  }).catch(() => {})
}

export function TourProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth()
  const navigate = useNavigate()
  const [active, setActive] = useState(false)
  const [currentIndex, setCurrentIndex] = useState(0)

  // On login: fetch tour state. If never-completed and never-dismissed,
  // auto-fire from the last known step. The server-side state means a
  // refresh / new-device login picks up where the user left off.
  useEffect(() => {
    if (!isAuthenticated) {
      setActive(false)
      return
    }
    let cancelled = false
    fetchTourState().then((state) => {
      if (cancelled) return
      const idx = Math.max(
        0,
        Math.min(state.current_step, TOUR_STEPS.length - 1),
      )
      setCurrentIndex(idx)
      const shouldAutofire = !state.completed_at && !state.dismissed_at
      if (shouldAutofire) {
        // Don't yank a freshly-logged-in user away from /login → /review
        // mid-navigation; give the router a tick to land.
        setTimeout(() => !cancelled && setActive(true), 400)
      }
    })
    return () => {
      cancelled = true
    }
  }, [isAuthenticated])

  // When a step has a `nav` target, push the route BEFORE the overlay
  // re-measures (the spotlight needs the target node painted first).
  useEffect(() => {
    if (!active) return
    const step = TOUR_STEPS[currentIndex]
    if (step?.nav) {
      navigate(step.nav, { replace: false })
    }
  }, [active, currentIndex, navigate])

  const start = useCallback(() => {
    setCurrentIndex(0)
    setActive(true)
    putTourState({ current_step: 0, completed_at: null, dismissed_at: null })
  }, [])

  const next = useCallback(() => {
    setCurrentIndex((prev) => {
      const nextIdx = prev + 1
      if (nextIdx >= TOUR_STEPS.length) {
        // Last step → mark completed, close overlay.
        setActive(false)
        putTourState({
          current_step: TOUR_STEPS.length - 1,
          completed_at: "now",
        })
        return prev
      }
      putTourState({ current_step: nextIdx })
      return nextIdx
    })
  }, [])

  const dismiss = useCallback(() => {
    setActive(false)
    putTourState({ current_step: currentIndex, dismissed_at: "now" })
  }, [currentIndex])

  const value = useMemo<TourContextValue>(
    () => ({
      active,
      currentStep: TOUR_STEPS[currentIndex] ?? null,
      currentIndex,
      total: TOUR_STEPS.length,
      start,
      next,
      dismiss,
    }),
    [active, currentIndex, start, next, dismiss],
  )

  return <TourContext.Provider value={value}>{children}</TourContext.Provider>
}

export function useTourContext(): TourContextValue {
  const ctx = useContext(TourContext)
  if (!ctx) {
    throw new Error("useTourContext: missing <TourProvider>")
  }
  return ctx
}
