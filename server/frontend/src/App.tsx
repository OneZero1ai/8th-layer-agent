import { lazy, Suspense } from "react"
import { BrowserRouter, Navigate, Route, Routes } from "react-router"
import { AuthProvider, useAuth } from "./auth"
import { Layout } from "./components/Layout"
import { ProtectedRoute } from "./components/ProtectedRoute"
import { ApiKeysPage } from "./pages/ApiKeysPage"
import { CrosstalkPage } from "./pages/CrosstalkPage"
import { DashboardPage } from "./pages/DashboardPage"
import { InvitesPage } from "./pages/InvitesPage"
import { LoginPage } from "./pages/LoginPage"
import { NetworkPage } from "./pages/NetworkPage"
import { PersonasPage } from "./pages/PersonasPage"
import { ReviewPage } from "./pages/ReviewPage"
import { ThemeProvider } from "./theme"
import { TourOverlay } from "./tour/TourOverlay"
import { TourProvider } from "./tour/TourProvider"

// Federation tab is route-split — keeps it (and its fixtures) out of the
// initial bundle. Honours the +5KB gzip main-bundle ceiling for #172.
const FederationPage = lazy(() =>
  import("./pages/FederationPage").then((m) => ({ default: m.FederationPage })),
)

function FederationFallback() {
  return (
    <div className="space-y-8">
      <p className="eyebrow">Federation</p>
      <p className="text-sm text-[var(--ink-mute)]">Loading peerings…</p>
    </div>
  )
}

function AppRoutes() {
  const { isAuthenticated } = useAuth()
  return (
    <Routes>
      <Route
        path="/login"
        element={
          isAuthenticated ? <Navigate to="/review" replace /> : <LoginPage />
        }
      />
      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/network" element={<NetworkPage />} />
        <Route path="/crosstalk" element={<CrosstalkPage />} />
        <Route
          path="/federation"
          element={
            <Suspense fallback={<FederationFallback />}>
              <FederationPage />
            </Suspense>
          }
        />
        <Route path="/settings/api-keys" element={<ApiKeysPage />} />
        <Route path="/admin/personas" element={<PersonasPage />} />
        <Route path="/admin/invites" element={<InvitesPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/review" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <TourProvider>
            <AppRoutes />
            <TourOverlay />
          </TourProvider>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  )
}
