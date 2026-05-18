/**
 * FO-3 Phase 3 — `KeyRevealPanel` (agent#193 / Decision 32).
 *
 * The one-time admin-API-key reveal shown when an L2-create job reaches
 * `COMPLETED`. The new L2's `cqa.v1.*` admin key arrives in the terminal SSE
 * event's `result.admin_api_key` (PR #292) and is rendered here exactly once.
 *
 * Security posture (task brief):
 *   - The key lives in React props/state only. It is NEVER written to
 *     localStorage, sessionStorage, or the URL — the same XSS-leak surface
 *     FO-1c closed for human session tokens.
 *   - Copy-on-click puts the key on the clipboard; nothing else persists it.
 *   - A "won't be shown again" warning + an acknowledgement checkbox gate
 *     the "Open L2 Admin" follow-on, mirroring the ApiKeys mint modal.
 *
 * Decision 32 #2 also emails the key as a backup, so a user who navigates
 * away without copying is not locked out — the panel says so explicitly.
 */

import { useState } from "react"
import type { L2ProvisionResult } from "./types"

interface KeyRevealPanelProps {
  /** The job result from the terminal `completed` SSE event. */
  result: L2ProvisionResult
}

export function KeyRevealPanel({ result }: KeyRevealPanelProps) {
  const [copied, setCopied] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)

  const apiKey = result.admin_api_key ?? ""
  const adminUrl = result.admin_url ?? ""
  const dnsName = result.dns_name ?? ""

  async function copyKey() {
    if (!apiKey) return
    try {
      await navigator.clipboard.writeText(apiKey)
      setCopied(true)
    } catch {
      // Clipboard denied (insecure context / permissions) — leave the key
      // visible so the user can select it manually. No state change.
    }
  }

  const primaryBtn =
    "rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"

  return (
    <section className="brand-surface-raised p-6" data-testid="key-reveal">
      <p className="eyebrow text-[var(--emerald)]">L2 provisioned</p>
      <h2 className="font-display text-xl text-[var(--ink)] mt-1">
        Your new L2 is live
      </h2>
      <p className="mt-2 text-sm text-[var(--ink-dim)] leading-relaxed">
        Copy the admin API key below now.{" "}
        <strong className="text-[var(--ink)]">
          It will not be shown again.
        </strong>{" "}
        A backup copy has also been emailed to the Enterprise admin.
      </p>

      {dnsName && (
        <p className="mt-3 text-sm text-[var(--ink-mute)]">
          Address:{" "}
          <code className="font-mono-brand text-[var(--brand-primary)]">
            {dnsName}
          </code>
        </p>
      )}

      {apiKey ? (
        <div className="mt-4 flex items-center gap-2 rounded-lg bg-[var(--bg-via)] border border-[var(--rule-strong)] p-3">
          <code
            className="flex-1 break-all font-mono-brand text-sm text-[var(--brand-primary)]"
            data-testid="admin-api-key"
          >
            {apiKey}
          </code>
          <button type="button" onClick={copyKey} className={primaryBtn}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      ) : (
        <p className="mt-4 text-sm text-[var(--gold)]">
          The provisioning service did not return an inline key. Check the
          Enterprise admin email for the L2 admin key.
        </p>
      )}

      <label className="mt-4 flex items-center gap-2 text-sm text-[var(--ink-dim)]">
        <input
          type="checkbox"
          checked={acknowledged}
          onChange={(e) => setAcknowledged(e.target.checked)}
          className="accent-[var(--brand-primary)]"
        />
        I have copied the admin API key and saved it securely.
      </label>

      {adminUrl && (
        <div className="mt-4">
          <a
            href={adminUrl}
            aria-disabled={!acknowledged}
            target="_blank"
            rel="noreferrer"
            className={`inline-block ${primaryBtn} ${
              acknowledged ? "" : "pointer-events-none opacity-50"
            }`}
            onClick={(e) => {
              // Gated on the acknowledgement checkbox — the href stays set so
              // the element is a real link, but the navigation is suppressed
              // until the user confirms they have saved the key.
              if (!acknowledged) e.preventDefault()
            }}
          >
            Open L2 Admin →
          </a>
        </div>
      )}
    </section>
  )
}
