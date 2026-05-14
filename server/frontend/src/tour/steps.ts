/**
 * The Founder's First L2 walk-through — eight steps that land a new
 * founder from "I just logged in" to "I know what to do next."
 *
 * `target` is a CSS selector (we attach `data-tour-target="<id>"` to the
 * relevant DOM node and the engine queries on that). If the target is
 * missing (route doesn't render it, screen too narrow, etc.) the engine
 * falls back to a centered modal so the step is never silently lost.
 *
 * `nav` (optional) lets a step push the router to a path BEFORE rendering
 * — e.g. step 5 jumps to /review so the highlight lands on a visible tab
 * even if the user manually navigated away mid-tour.
 *
 * Copy is intentionally ~25 words per step. Shorter is harsh; longer the
 * user skims past. Eight steps × ~90 seconds = the right onboarding budget.
 */

export interface TourStep {
  /** Stable id — used in `data-tour-target` selectors and analytics. */
  id: string
  /** Optional route to navigate to before showing this step. */
  nav?: string
  /** Title rendered as the popover heading. */
  title: string
  /** Body copy. Plain text — no markdown. */
  body: string
  /** Optional CTA label for the "Next" button. Defaults to "Next". */
  ctaLabel?: string
}

export const TOUR_STEPS: TourStep[] = [
  {
    id: "welcome",
    title: "Welcome to your L2",
    body: "This is your Enterprise's Semantic Knowledge Layer — Layer 8 of the stack. Agent sessions register here, propose discoveries, and query a growing commons.",
  },
  {
    id: "group",
    title: "One Group, one L2 — for now",
    body: "Your Enterprise starts with the `default` group. Add more Groups later; each gets its own isolated L2 (model B).",
  },
  {
    id: "api-keys",
    nav: "/settings/api-keys",
    title: "Connect your first agent",
    body: "Mint a key here, then point a Claude Code session at this L2 via the `cq` plugin's setup-skill. Knowledge starts flowing in minutes.",
  },
  {
    id: "network",
    nav: "/network",
    title: "Watch the graph grow",
    body: "Sessions, Personas, Humans, KUs, peer Enterprises — all of it shows up here as nodes. This IS your operational picture.",
  },
  {
    id: "review",
    nav: "/review",
    title: "Approve knowledge",
    body: "When an agent proposes a KU that needs human eyes, it lands here. You ship only what you vouch for.",
  },
  {
    id: "dashboard",
    nav: "/dashboard",
    title: "Day-over-day",
    body: "Propose / approve / flag counts, fresh activity, fleet health. Mission control for the substrate.",
  },
  {
    id: "personas",
    nav: "/admin/personas",
    title: "Invite humans, assign personas",
    body: "One human, many personas. Each logs in with a passkey and gets its own activity trail under one Enterprise identity.",
  },
  {
    id: "done",
    title: "You're operational",
    body: "The substrate is live. Connect your first session and the graph fills in. Replay this tour anytime via the `?` button.",
    ctaLabel: "Finish",
  },
]
