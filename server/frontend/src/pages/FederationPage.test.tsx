import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import {
  EMPTY_FEDERATION_FIXTURE,
  FEDERATION_FIXTURE,
} from "../federation/fixtures"
import { FederationPage } from "./FederationPage"

describe("FederationPage", () => {
  it("renders header + nav with sub-tab counts", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    expect(screen.getByText(/peering health/i)).toBeInTheDocument()
    // Active sub-tab badge reflects fixture count.
    const activeTab = screen.getByRole("tab", { name: /active/i })
    expect(within(activeTab).getByText("3")).toBeInTheDocument()
  })

  it("renders the active peerings table by default", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    const table = screen.getByTestId("active-peerings-table")
    expect(within(table).getByText("Acme Corp")).toBeInTheDocument()
    expect(within(table).getByText("Phantom Labs")).toBeInTheDocument()
  })

  it("flags a silently-broken peering in the header summary", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    expect(
      screen.getByTestId("federation-silent-break-summary"),
    ).toHaveTextContent(/silently broken/i)
  })

  it("opens the drawer when an active peering row is clicked", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    const phantomRow = screen.getByText("Phantom Labs").closest("tr")
    if (!phantomRow) throw new Error("Phantom Labs row not found")
    fireEvent.click(phantomRow)
    const dialog = screen.getByRole("dialog")
    expect(
      within(dialog).getByText(/silent break detected/i),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByTestId("silent-break-banner"),
    ).toBeInTheDocument()
  })

  it("shows the pending offers inbox with accept/decline controls", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    fireEvent.click(screen.getByRole("tab", { name: /pending/i }))
    expect(screen.getByText("Aurora Bio")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: /accept/i })[0]).toBeEnabled()
    expect(screen.getAllByRole("button", { name: /decline/i })[0]).toBeEnabled()
  })

  it("shows outgoing offers with a withdraw button", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    fireEvent.click(screen.getByRole("tab", { name: /outgoing/i }))
    expect(screen.getByText("Zenith AI")).toBeInTheDocument()
    expect(
      screen.getAllByRole("button", { name: /withdraw/i })[0],
    ).toBeEnabled()
  })

  it("renders the health view including alarms", () => {
    render(<FederationPage initialData={FEDERATION_FIXTURE} />)
    fireEvent.click(screen.getByRole("tab", { name: /health/i }))
    expect(screen.getAllByText(/consult outcomes/i).length).toBeGreaterThan(0)
    expect(screen.getByTestId("federation-heatmap")).toBeInTheDocument()
    // Phantom Labs has 12% success rate → triggers alarm. Page renders the
    // peer in both the heatmap row and the alarm list.
    expect(screen.getAllByText(/Phantom Labs/i).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/alarm/i).length).toBeGreaterThan(0)
  })

  it("renders empty states for each sub-tab when the view is empty", () => {
    render(<FederationPage initialData={EMPTY_FEDERATION_FIXTURE} />)
    expect(screen.getByTestId("federation-empty")).toHaveTextContent(
      /no active peerings/i,
    )
    fireEvent.click(screen.getByRole("tab", { name: /pending/i }))
    expect(screen.getByTestId("federation-empty")).toHaveTextContent(
      /inbox empty/i,
    )
    fireEvent.click(screen.getByRole("tab", { name: /outgoing/i }))
    expect(screen.getByTestId("federation-empty")).toHaveTextContent(
      /no outgoing offers/i,
    )
    fireEvent.click(screen.getByRole("tab", { name: /health/i }))
    expect(screen.getByTestId("federation-empty")).toHaveTextContent(
      /no mesh data/i,
    )
  })
})
