import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import App from "./App"

// Theme selection — driven by VITE_THEME at build time. Defaults to the
// 8th-Layer brand theme; set VITE_THEME=mainline-cq to fall back to the
// upstream cq look (useful for A/B during migration).
const theme = (import.meta.env.VITE_THEME as string | undefined) ?? "8th-layer"
const validTheme = theme === "mainline-cq" ? "mainline-cq" : "8th-layer"
document.documentElement.setAttribute("data-theme", validTheme)

const rootElement = document.getElementById("root")
if (!rootElement) {
  throw new Error("Root element #root not found in document")
}
createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
