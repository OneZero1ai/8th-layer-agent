/**
 * FO-3 Phase 3 — Create-L2 wizard page (agent#193 / Decision 32).
 *
 * A single-route, 5-step wizard inside the cq-server admin shell that
 * provisions an additional L2 in the caller's Enterprise:
 *
 *   1. Name     — L2 slug, pattern ^[a-z][a-z0-9-]{2,30}$, debounced
 *                 availability probe.
 *   2. Purpose  — free-text description, 5–500 chars.
 *   3. Region   — AWS region select (allowlist: us-east-1 — Decision 32 #1).
 *   4. DNS      — read-only `<l2>.<enterprise>.8th-layer.ai` confirm.
 *   5. Review   — summary + Provision → POST /api/v1/admin/l2s.
 *
 * Then a progress step: connect the SSE stream, render the ~8-phase progress
 * bar, and on COMPLETED show the one-time admin-key reveal panel.
 *
 * Per the brief, the step state is local `useState` (single-route wizard) —
 * not the multi-route Context the 8th-layer-signup wizard uses.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ApiError, api } from "../api"
import { KeyRevealPanel } from "../l2wizard/KeyRevealPanel"
import { PhaseProgressBar } from "../l2wizard/PhaseProgressBar"
import type { CreateL2Response } from "../l2wizard/types"
import { useL2ProvisioningSSE } from "../l2wizard/useL2ProvisioningSSE"
import { useTheme } from "../theme"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** L2 slug shape — mirrors the cq-server proxy's CreateL2Request field doc. */
export const L2_SLUG_PATTERN = /^[a-z][a-z0-9-]{2,30}$/

const DESCRIPTION_MIN = 5
const DESCRIPTION_MAX = 500

/**
 * AWS region allowlist. Decision 32 #1: per-L2 region override, server-side
 * allowlist starting at `us-east-1`. Add regions here as the directory's
 * allowlist widens.
 */
export const AWS_REGIONS: readonly { value: string; label: string }[] = [
  { value: "us-east-1", label: "us-east-1 — N. Virginia" },
] as const

/** The wizard's ordered config steps, plus the terminal progress step. */
type WizardStep = "name" | "purpose" | "region" | "dns" | "review" | "progress"

const CONFIG_STEPS: readonly WizardStep[] = [
  "name",
  "purpose",
  "region",
  "dns",
  "review",
] as const

const STEP_TITLES: Record<WizardStep, string> = {
  name: "Name",
  purpose: "Purpose",
  region: "Region",
  dns: "DNS",
  review: "Review",
  progress: "Provisioning",
}

type SlugStatus = "idle" | "checking" | "available" | "taken" | "unknown"

// ---------------------------------------------------------------------------
// Shared brand classes (match ApiKeysPage conventions)
// ---------------------------------------------------------------------------

const primaryBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"
const ghostBtn =
  "rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"

// ---------------------------------------------------------------------------
// Step-progress header
// ---------------------------------------------------------------------------

function StepIndicator({ current }: { current: WizardStep }) {
  // The progress step renders its own header — only show the config dots.
  const activeIdx = CONFIG_STEPS.indexOf(current)
  return (
    <ol className="flex flex-wrap items-center gap-2" aria-label="Wizard steps">
      {CONFIG_STEPS.map((step, idx) => {
        const done = activeIdx > idx
        const active = activeIdx === idx
        return (
          <li key={step} className="flex items-center gap-2">
            <span
              className={`flex h-6 w-6 items-center justify-center rounded-full font-mono-brand text-[11px] ${
                active
                  ? "bg-[color-mix(in_srgb,var(--brand-primary)_22%,transparent)] text-[var(--brand-primary)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)]"
                  : done
                    ? "bg-[color-mix(in_srgb,var(--emerald)_18%,transparent)] text-[var(--emerald)]"
                    : "bg-[var(--surface-hover)] text-[var(--ink-mute)]"
              }`}
            >
              {done ? "✓" : idx + 1}
            </span>
            <span
              className={`font-mono-brand text-[11px] uppercase tracking-[0.16em] ${
                active ? "text-[var(--ink)]" : "text-[var(--ink-mute)]"
              }`}
            >
              {STEP_TITLES[step]}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function CreateL2Page() {
  const { theme } = useTheme()
  // The DNS preview needs the Enterprise slug. The /theme endpoint exposes
  // it as `enterprise.id` (FO-1d). Fall back to a placeholder if the theme
  // has not resolved — the DNS step blocks `Continue` until it has.
  const enterpriseSlug = theme?.enterprise.id ?? ""

  const [step, setStep] = useState<WizardStep>("name")

  // Step 1 — name.
  const [slug, setSlug] = useState("")
  const [slugStatus, setSlugStatus] = useState<SlugStatus>("idle")

  // Step 2 — purpose.
  const [description, setDescription] = useState("")

  // Step 3 — region.
  const [region, setRegion] = useState(AWS_REGIONS[0].value)

  // Step 4 — DNS confirm.
  const [dnsConfirmed, setDnsConfirmed] = useState(false)

  // Step 5 / provision.
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [createResponse, setCreateResponse] = useState<CreateL2Response | null>(
    null,
  )

  // ---- derived validation -------------------------------------------------

  const slugValid = L2_SLUG_PATTERN.test(slug)
  const descriptionValid =
    description.trim().length >= DESCRIPTION_MIN &&
    description.trim().length <= DESCRIPTION_MAX
  const dnsName = useMemo(
    () =>
      slug && enterpriseSlug
        ? `${slug}.${enterpriseSlug}.8th-layer.ai`
        : `${slug || "<l2>"}.${enterpriseSlug || "<enterprise>"}.8th-layer.ai`,
    [slug, enterpriseSlug],
  )

  // ---- debounced slug availability probe ----------------------------------

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    if (!slugValid) {
      setSlugStatus("idle")
      return
    }
    setSlugStatus("checking")
    debounceRef.current = setTimeout(() => {
      api
        .checkL2SlugAvailable(slug)
        .then((result) => setSlugStatus(result))
        .catch(() => setSlugStatus("unknown"))
    }, 400)
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [slug, slugValid])

  // ---- SSE provisioning stream --------------------------------------------

  const stream = useL2ProvisioningSSE(createResponse?.stream_url ?? null)

  // ---- step navigation ----------------------------------------------------

  // The slug step blocks `Continue` on an explicit `taken` result, but not
  // on `unknown` (the proxy slug-availability route may not be deployed —
  // the create call's 409 is the authoritative uniqueness check).
  const canLeaveName = slugValid && slugStatus !== "taken"
  const canLeavePurpose = descriptionValid
  const canLeaveRegion = AWS_REGIONS.some((r) => r.value === region)
  const canLeaveDns = dnsConfirmed && enterpriseSlug !== ""

  function goNext() {
    const idx = CONFIG_STEPS.indexOf(step as WizardStep)
    if (idx >= 0 && idx < CONFIG_STEPS.length - 1) {
      setStep(CONFIG_STEPS[idx + 1])
    }
  }

  function goBack() {
    const idx = CONFIG_STEPS.indexOf(step as WizardStep)
    if (idx > 0) {
      setStep(CONFIG_STEPS[idx - 1])
    }
  }

  const handleProvision = useCallback(async () => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const resp = await api.createL2({
        l2_slug: slug,
        description: description.trim(),
        aws_region: region,
      })
      setCreateResponse(resp)
      setStep("progress")
    } catch (err) {
      if (err instanceof ApiError) {
        setSubmitError(err.message)
        // A 409 means the slug was taken between the probe and submit —
        // bounce the user back to the name step to pick another.
        if (err.status === 409) {
          setSlugStatus("taken")
          setStep("name")
        }
      } else {
        setSubmitError(
          err instanceof Error ? err.message : "Failed to start provisioning.",
        )
      }
    } finally {
      setSubmitting(false)
    }
  }, [slug, description, region])

  // ---- render -------------------------------------------------------------

  return (
    <div className="space-y-8">
      <section>
        <p className="eyebrow">Admin</p>
        <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
          Add an L2
        </h1>
        <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
          Provision an additional L2 in your Enterprise. An L2 is a knowledge
          domain with its own agents, personas, and admin shell. The new L2
          inherits your Enterprise&rsquo;s AWS account; you choose its region
          below.
        </p>
      </section>

      {step !== "progress" && (
        <div className="brand-surface p-4">
          <StepIndicator current={step} />
        </div>
      )}

      {/* ---------- Step 1 — Name ---------- */}
      {step === "name" && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">Step 1</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Name your L2
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            The slug identifies the L2 and forms its subdomain. Lowercase
            letters, digits, and hyphens; 3–31 characters; must start with a
            letter.
          </p>
          <label className="mt-5 flex flex-col text-sm">
            <span className="eyebrow mb-1.5">L2 slug</span>
            <input
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value.toLowerCase())}
              placeholder="e.g. marketing"
              maxLength={31}
              aria-invalid={slug.length > 0 && !slugValid}
              aria-describedby="slug-help"
              className="brand-input font-mono-brand"
            />
            <span id="slug-help" className="mt-1.5 text-xs">
              {slug.length > 0 && !slugValid && (
                <span className="text-[var(--rose)]">
                  Must match{" "}
                  <code className="font-mono-brand">
                    ^[a-z][a-z0-9-]&#123;2,30&#125;$
                  </code>
                  .
                </span>
              )}
              {slugValid && slugStatus === "checking" && (
                <span className="text-[var(--ink-mute)]">
                  Checking availability…
                </span>
              )}
              {slugValid && slugStatus === "available" && (
                <span className="text-[var(--emerald)]">
                  &ldquo;{slug}&rdquo; is available.
                </span>
              )}
              {slugValid && slugStatus === "taken" && (
                <span className="text-[var(--rose)]">
                  &ldquo;{slug}&rdquo; is already in use in this Enterprise.
                </span>
              )}
              {slugValid && slugStatus === "unknown" && (
                <span className="text-[var(--ink-mute)]">
                  Availability will be confirmed when you provision.
                </span>
              )}
            </span>
          </label>
          <div className="mt-6 flex justify-end">
            <button
              type="button"
              onClick={goNext}
              disabled={!canLeaveName}
              className={primaryBtn}
            >
              Continue
            </button>
          </div>
        </section>
      )}

      {/* ---------- Step 2 — Purpose ---------- */}
      {step === "purpose" && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">Step 2</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Describe its purpose
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            A short free-text description of what this L2 is for. It becomes the
            L2&rsquo;s directory description.
          </p>
          <label className="mt-5 flex flex-col text-sm">
            <span className="eyebrow mb-1.5">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={4}
              maxLength={DESCRIPTION_MAX}
              placeholder="e.g. Knowledge domain for the marketing team's campaign agents."
              aria-invalid={description.length > 0 && !descriptionValid}
              className="brand-input"
            />
            <span className="mt-1.5 flex justify-between text-xs">
              <span
                className={
                  description.length > 0 && !descriptionValid
                    ? "text-[var(--rose)]"
                    : "text-[var(--ink-mute)]"
                }
              >
                {DESCRIPTION_MIN}–{DESCRIPTION_MAX} characters.
              </span>
              <span className="font-mono-brand text-[var(--ink-mute)]">
                {description.trim().length}/{DESCRIPTION_MAX}
              </span>
            </span>
          </label>
          <div className="mt-6 flex justify-between">
            <button type="button" onClick={goBack} className={ghostBtn}>
              Back
            </button>
            <button
              type="button"
              onClick={goNext}
              disabled={!canLeavePurpose}
              className={primaryBtn}
            >
              Continue
            </button>
          </div>
        </section>
      )}

      {/* ---------- Step 3 — Region ---------- */}
      {step === "region" && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">Step 3</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Choose an AWS region
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            The L2&rsquo;s infrastructure is provisioned in this region within
            your Enterprise&rsquo;s AWS account.
          </p>
          <label className="mt-5 flex flex-col text-sm">
            <span className="eyebrow mb-1.5">Region</span>
            <select
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              className="brand-input font-mono-brand"
            >
              {AWS_REGIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
            <span className="mt-1.5 text-xs text-[var(--ink-mute)]">
              More regions become available as the platform allowlist widens.
            </span>
          </label>
          <div className="mt-6 flex justify-between">
            <button type="button" onClick={goBack} className={ghostBtn}>
              Back
            </button>
            <button
              type="button"
              onClick={goNext}
              disabled={!canLeaveRegion}
              className={primaryBtn}
            >
              Continue
            </button>
          </div>
        </section>
      )}

      {/* ---------- Step 4 — DNS confirm ---------- */}
      {step === "dns" && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">Step 4</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Confirm the DNS name
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            The L2&rsquo;s admin shell and agent endpoint will be served at this
            address. It is derived from the slug and your Enterprise.
          </p>
          <div className="mt-5 rounded-lg bg-[var(--bg-via)] border border-[var(--rule-strong)] p-4">
            <code
              className="break-all font-mono-brand text-sm text-[var(--brand-primary)]"
              data-testid="dns-preview"
            >
              {dnsName}
            </code>
          </div>
          {enterpriseSlug === "" && (
            <p className="mt-2 text-xs text-[var(--gold)]">
              Resolving your Enterprise… the DNS name will populate shortly.
            </p>
          )}
          <label className="mt-4 flex items-center gap-2 text-sm text-[var(--ink-dim)]">
            <input
              type="checkbox"
              checked={dnsConfirmed}
              onChange={(e) => setDnsConfirmed(e.target.checked)}
              className="accent-[var(--brand-primary)]"
            />
            This DNS name is correct.
          </label>
          <div className="mt-6 flex justify-between">
            <button type="button" onClick={goBack} className={ghostBtn}>
              Back
            </button>
            <button
              type="button"
              onClick={goNext}
              disabled={!canLeaveDns}
              className={primaryBtn}
            >
              Continue
            </button>
          </div>
        </section>
      )}

      {/* ---------- Step 5 — Review ---------- */}
      {step === "review" && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">Step 5</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Review &amp; provision
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            Provisioning takes a few minutes. You will see live progress and a
            one-time admin API key when it completes.
          </p>
          <dl className="mt-5 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="eyebrow">L2 slug</dt>
              <dd className="mt-0.5 font-mono-brand text-[var(--ink)]">
                {slug}
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Region</dt>
              <dd className="mt-0.5 font-mono-brand text-[var(--ink)]">
                {region}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="eyebrow">DNS name</dt>
              <dd className="mt-0.5 font-mono-brand text-[var(--brand-primary)]">
                {dnsName}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="eyebrow">Purpose</dt>
              <dd className="mt-0.5 text-[var(--ink-dim)]">
                {description.trim()}
              </dd>
            </div>
          </dl>
          {submitError && (
            <p className="mt-4 text-sm text-[var(--rose)]">{submitError}</p>
          )}
          <div className="mt-6 flex justify-between">
            <button
              type="button"
              onClick={goBack}
              disabled={submitting}
              className={ghostBtn}
            >
              Back
            </button>
            <button
              type="button"
              onClick={handleProvision}
              disabled={submitting}
              className={primaryBtn}
            >
              {submitting ? "Starting…" : "Provision L2"}
            </button>
          </div>
        </section>
      )}

      {/* ---------- Progress step ---------- */}
      {step === "progress" && createResponse && (
        <section
          className="brand-surface-raised p-6"
          data-testid="progress-step"
        >
          <p className="eyebrow">Provisioning</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Standing up{" "}
            <span className="font-mono-brand text-[var(--brand-primary)]">
              {slug}
            </span>
          </h2>
          <p className="mt-2 text-sm text-[var(--ink-dim)]">
            {stream.phase === "completed"
              ? "Your L2 is ready."
              : stream.phase === "failed"
                ? "Provisioning did not complete."
                : "This usually takes a few minutes. Keep this tab open."}
          </p>

          <div className="mt-6">
            <PhaseProgressBar
              lifecycle={stream.phase}
              phase={stream.jobState?.phase ?? null}
              phaseLabel={stream.jobState?.phase_label ?? null}
              progressPct={stream.jobState?.progress_pct ?? null}
            />
          </div>

          {stream.phase === "failed" && (
            <div className="mt-6 rounded-lg border border-[color-mix(in_srgb,var(--rose)_30%,transparent)] bg-[color-mix(in_srgb,var(--rose)_8%,transparent)] p-4">
              <p className="eyebrow text-[var(--rose)]">Provisioning failed</p>
              <p className="mt-1.5 text-sm text-[var(--ink-dim)]">
                {stream.error ||
                  "The provisioning job reported a failure. No L2 was created."}
              </p>
            </div>
          )}
        </section>
      )}

      {/* ---------- Completion — one-time key reveal ---------- */}
      {step === "progress" &&
        stream.phase === "completed" &&
        stream.jobState?.result && (
          <KeyRevealPanel result={stream.jobState.result} />
        )}
    </div>
  )
}
