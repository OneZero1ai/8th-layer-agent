import { BrowserRouter, Navigate, Route, Routes } from "react-router"
import { AuthProvider, useAuth } from "./auth"
import { Layout } from "./components/Layout"
import { ProtectedRoute } from "./components/ProtectedRoute"
import { ApiKeysPage } from "./pages/ApiKeysPage"
import { DashboardPage } from "./pages/DashboardPage"
import { InvitesPage } from "./pages/InvitesPage"
import { LoginPage } from "./pages/LoginPage"
import { NetworkPage } from "./pages/NetworkPage"
import { PersonasPage } from "./pages/PersonasPage"
import { ReviewPage } from "./pages/ReviewPage"
import { ThemeProvider } from "./theme"
import { TourOverlay } from "./tour/TourOverlay"
import { TourProvider } from "./tour/TourProvider"

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
