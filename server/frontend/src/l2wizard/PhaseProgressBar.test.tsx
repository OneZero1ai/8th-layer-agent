/**
 * FO-3 Phase 3 — tests for `PhaseProgressBar` (agent#193).
 */

import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { PhaseProgressBar } from "./PhaseProgressBar"

describe("PhaseProgressBar", () => {
  it("derives fill width from the phase index when no progress_pct given", () => {
    render(
      <PhaseProgressBar
        lifecycle="streaming"
        phase={4}
        phaseLabel={null}
        progressPct={null}
      />,
    )
    // 4 of 8 phases → 50%.
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "50",
    )
  })

  it("prefers the server-reported progress_pct over the phase index", () => {
    render(
      <PhaseProgressBar
        lifecycle="streaming"
        phase={1}
        phaseLabel="ACM certificate"
        progressPct={73}
      />,
    )
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "73",
    )
  })

  it("fills to 100% on completion regardless of phase", () => {
    render(
      <PhaseProgressBar
        lifecycle="completed"
        phase={3}
        phaseLabel={null}
        progressPct={null}
      />,
    )
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "100",
    )
    expect(screen.getByText(/provisioning complete/i)).toBeInTheDocument()
  })

  it("shows the failed state", () => {
    render(
      <PhaseProgressBar
        lifecycle="failed"
        phase={5}
        phaseLabel={null}
        progressPct={null}
      />,
    )
    expect(screen.getByText(/provisioning failed/i)).toBeInTheDocument()
  })

  it("renders the live phase label for the active phase", () => {
    render(
      <PhaseProgressBar
        lifecycle="streaming"
        phase={2}
        phaseLabel="Spinning up ECS"
        progressPct={null}
      />,
    )
    expect(screen.getByText("Spinning up ECS")).toBeInTheDocument()
  })
})
