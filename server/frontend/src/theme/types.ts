/**
 * Type contract for the JSON returned by `GET /api/v1/theme`.
 *
 * Mirrors `cq_server.theme.ThemeResolver.resolve()`. Keys are required;
 * value-level nullability matches the backend contract (Decision 30).
 */

export interface PlatformTheme {
  name: string
  version: string
  tokens: Record<string, string>
}

export interface EnterpriseTheme {
  id: string
  display_name: string
  logo_url: string | null
  accent_hex: string | null
  dark_mode_only: boolean
}

export interface L2Theme {
  id: string
  label: string
  subaccent_hex: string | null
  hero_motif: string | null
}

export interface ResolvedTheme {
  platform: PlatformTheme
  enterprise: EnterpriseTheme
  l2: L2Theme
}
