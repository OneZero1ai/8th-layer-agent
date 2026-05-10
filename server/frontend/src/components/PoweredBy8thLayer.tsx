/**
 * "Powered by 8th-Layer.ai" co-branding badge (FO-1d, Decision 30).
 *
 * Visible on every page in the L2 admin shell. Decision 30
 * §"co-branding rule" makes this badge non-overridable — it links to
 * the platform marketing site and uses platform tokens directly so a
 * customer brand override cannot suppress it.
 *
 * V1 ships this as a small text link in the layout footer; later tiers
 * (FO-7 white-label) may relax the visibility rule but the V1 default
 * is "always present, always dim, always platform-coloured."
 */

export function PoweredBy8thLayer() {
  return (
    <footer className="mt-12 mb-8 px-4 text-center">
      <a
        href="https://8th-layer.ai"
        target="_blank"
        rel="noreferrer noopener"
        className="font-mono-brand text-[10px] uppercase tracking-[0.22em] text-[var(--ink-faint)] hover:text-[var(--ink-mute)] transition-colors"
      >
        Powered by 8th-Layer.ai
      </a>
    </footer>
  )
}
