/**
 * FO-4 Phase 2 — Self-service Add-Agent page (agent#194 / Decision 33).
 *
 * A single-route admin-shell page at `/admin/agents/new`. Unlike FO-3's
 * Create-L2 wizard, this is a single synchronous request/response — no SSE,
 * no step machine. The operator names an agent, picks a harness, sets a TTL,
 * and clicks Mint; the backend creates a stub user + `persona='agent'`
 * assignment + an `api_keys` row and returns the plaintext `cqa.v1.*` token
 * exactly once.
 *
 * Locked decisions (Decision 33, operator 2026-05-19):
 *   1. No scope/permission selector — FO-4 mints full-capability keys; the
 *      selector lands when per-key enforcement does (separate follow-up).
 *   2. Three install paths on the completion panel: the `8l join …` one-liner,
 *      a plugin-install command, and a QR code encoding the join command.
 *
 * The token reveal copies KeyRevealPanel's visual pattern (the
 * `brand-surface-raised` card, the code box + Copy, the "won't be shown
 * again" warning). KeyRevealPanel itself is typed to an L2 result, so this
 * builds a local reveal block in the same style rather than importing it.
 */

import { QRCodeSVG } from "qrcode.react"
import { useCallback, useEffect, useState } from "react"
import { ApiError, api } from "../api"
import type {
  AgentHarness,
  AgentKeyPublic,
  MintAgentKeyResponse,
} from "../types"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Harness options — values are the wire enum `AgentHarness`. */
const HARNESSES: readonly { value: AgentHarness; label: string }[] = [
  { value: "claude-code", label: "Claude Code" },
  { value: "claude-desktop", label: "Claude Desktop" },
  { value: "openclaw", label: "OpenClaw" },
  { value: "other", label: "Other" },
] as const

/** Default agent-key TTL. Decision 33 §Shape — 60 days. */
const DEFAULT_TTL = "60d"

/** Agent-name shape — a friendly slug; the backend derives the stub username. */
const AGENT_NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9 _-]{1,48}$/

/**
 * Plugin-install command for operators who do not yet have the `8l` CLI.
 * NOTE: the marketplace slug `OneZero1ai/8th-layer-agent` and the plugin id
 * `8l-cq` need verification once the plugin marketplace listing is published
 * — confirm against the published marketplace manifest before GA.
 */
const PLUGIN_INSTALL_COMMAND =
  "/plugin marketplace add OneZero1ai/8th-layer-agent\n" +
  "/plugin install 8l-cq@8th-layer-agent"

// ---------------------------------------------------------------------------
// Shared brand classes (match CreateL2Page / PersonasPage conventions)
// ---------------------------------------------------------------------------

const primaryBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"
const ghostBtn =
  "rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"

// ---------------------------------------------------------------------------
// Copyable code block — the KeyRevealPanel copy pattern, reusable.
// ---------------------------------------------------------------------------

function CopyBlock({
  value,
  label,
  testid,
  mono = true,
}: {
  value: string
  label?: string
  testid?: string
  mono?: boolean
}) {
  const [copied, setCopied] = useState(false)

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
    } catch {
      // Clipboard denied (insecure context / permissions) — leave the value
      // visible so the operator can select it manually. No state change.
    }
  }, [value])

  return (
    <div>
      {label && <p className="eyebrow mb-1.5">{label}</p>}
      <div className="flex items-start gap-2 rounded-lg bg-[var(--bg-via)] border border-[var(--rule-strong)] p-3">
        <code
          className={`flex-1 break-all whitespace-pre-wrap text-sm text-[var(--brand-primary)] ${
            mono ? "font-mono-brand" : ""
          }`}
          data-testid={testid}
        >
          {value}
        </code>
        <button type="button" onClick={copy} className={primaryBtn}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Existing agent-keys table
// ---------------------------------------------------------------------------

function formatDate(iso: string | null): string {
  if (!iso) return "never"
  return new Date(iso).toLocaleDateString()
}

function AgentKeyTable({ keys }: { keys: AgentKeyPublic[] }) {
  if (keys.length === 0) {
    return (
      <p className="text-sm text-[var(--ink-mute)]">
        No agent keys minted yet.
      </p>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm" data-testid="agent-key-table">
        <thead>
          <tr className="border-b border-[var(--rule)]">
            {["Name", "Agent username", "Prefix", "Expires", "Status"].map(
              (h) => (
                <th
                  key={h}
                  className="pb-2 eyebrow text-[var(--ink-mute)] font-normal"
                >
                  {h}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr
              key={k.id}
              className="border-b border-[var(--rule)] last:border-0"
            >
              <td className="py-2 text-[var(--ink)]">{k.name}</td>
              <td className="py-2 font-mono-brand text-[var(--ink-dim)]">
                {k.agent_username}
              </td>
              <td className="py-2 font-mono-brand text-[var(--ink-dim)]">
                {k.prefix}
              </td>
              <td className="py-2 text-[var(--ink-dim)]">
                {formatDate(k.expires_at)}
              </td>
              <td className="py-2">
                <span
                  className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-mono-brand ${
                    k.is_active
                      ? "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)]"
                      : "bg-[var(--surface-hover)] text-[var(--ink-mute)]"
                  }`}
                >
                  {k.is_active ? "Active" : "Inactive"}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Completion panel — one-time token reveal + three install paths.
// ---------------------------------------------------------------------------

function CompletionPanel({ result }: { result: MintAgentKeyResponse }) {
  return (
    <section className="brand-surface-raised p-6" data-testid="mint-complete">
      <p className="eyebrow text-[var(--emerald)]">Agent minted</p>
      <h2 className="font-display text-xl text-[var(--ink)] mt-1">
        {result.name} is ready to join
      </h2>
      <p className="mt-2 text-sm text-[var(--ink-dim)] leading-relaxed">
        Copy the agent token below now.{" "}
        <strong className="text-[var(--ink)]">
          It will not be shown again.
        </strong>{" "}
        Only its hash is stored — if you lose it, mint a new key.
      </p>

      <p className="mt-3 text-sm text-[var(--ink-mute)]">
        Agent username:{" "}
        <code className="font-mono-brand text-[var(--brand-primary)]">
          {result.agent_username}
        </code>
      </p>

      {/* ---- One-time token reveal ---- */}
      <div className="mt-4">
        <CopyBlock
          label="Agent token"
          value={result.token}
          testid="agent-token"
        />
      </div>

      {/* ---- Install paths ---- */}
      <div className="mt-8 space-y-6">
        <p className="eyebrow text-[var(--ink)]">Install paths</p>

        {/* (a) — 8l join one-liner */}
        <div data-testid="install-join">
          <p className="text-sm text-[var(--ink-dim)] mb-1.5">
            <strong className="text-[var(--ink)]">Join command.</strong> Run
            this in the agent&rsquo;s harness with the <code>8l</code> CLI
            installed.
          </p>
          <CopyBlock
            value={result.install.join_command}
            testid="join-command"
          />
        </div>

        {/* (b) — plugin-install command */}
        <div data-testid="install-plugin">
          <p className="text-sm text-[var(--ink-dim)] mb-1.5">
            <strong className="text-[var(--ink)]">Plugin install.</strong> For
            operators who do not have the <code>8l</code> CLI yet — install the
            plugin from the marketplace, then run the join command above.
          </p>
          <CopyBlock value={PLUGIN_INSTALL_COMMAND} testid="plugin-command" />
        </div>

        {/* (c) — QR code */}
        <div data-testid="install-qr">
          <p className="text-sm text-[var(--ink-dim)] mb-2.5">
            <strong className="text-[var(--ink)]">Scan from a device.</strong>{" "}
            This QR code encodes the join command.
          </p>
          <div className="inline-block rounded-lg bg-white p-3">
            <QRCodeSVG
              value={result.install.join_command}
              size={160}
              level="M"
              data-testid="join-qr"
            />
          </div>
        </div>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function AddAgentPage() {
  // Form state.
  const [agentName, setAgentName] = useState("")
  const [harness, setHarness] = useState<AgentHarness>("claude-code")
  const [ttl, setTtl] = useState(DEFAULT_TTL)

  // Submission state.
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [result, setResult] = useState<MintAgentKeyResponse | null>(null)

  // Existing agent keys (admin table).
  const [agentKeys, setAgentKeys] = useState<AgentKeyPublic[]>([])

  const loadKeys = useCallback(() => {
    api
      .listAgentKeys()
      .then((resp) => setAgentKeys(resp.data))
      .catch(() => {
        // The table is informational — a list failure must not block minting.
      })
  }, [])

  useEffect(() => {
    loadKeys()
  }, [loadKeys])

  // ---- derived validation -------------------------------------------------

  const nameValid = AGENT_NAME_PATTERN.test(agentName.trim())
  const ttlValid = /^\d+[smhd]$/.test(ttl.trim())
  const canSubmit = nameValid && ttlValid && !submitting

  // ---- submit -------------------------------------------------------------

  const handleMint = useCallback(async () => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const resp = await api.mintAgentKey({
        agent_name: agentName.trim(),
        harness,
        ttl: ttl.trim(),
      })
      setResult(resp)
      // Refresh the table so the new key shows when the operator scrolls.
      loadKeys()
    } catch (err) {
      if (err instanceof ApiError) {
        // 409 duplicate name, 422 bad name/ttl, 403 not-admin — the backend
        // message is operator-facing; surface it verbatim.
        setSubmitError(err.message)
      } else {
        setSubmitError(
          err instanceof Error ? err.message : "Failed to mint agent key.",
        )
      }
    } finally {
      setSubmitting(false)
    }
  }, [agentName, harness, ttl, loadKeys])

  // ---- render -------------------------------------------------------------

  return (
    <div className="space-y-8">
      <section>
        <p className="eyebrow">Admin</p>
        <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
          Add an Agent
        </h1>
        <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
          Mint a new agent key for this L2. Name the agent, pick its harness,
          and set a token lifetime — you get a one-time token plus copy-paste
          install paths. The agent joins as a stub identity with the{" "}
          <code className="font-mono-brand">agent</code> persona.
        </p>
      </section>

      {/* ---------- Form (replaced by the completion panel on success) ---------- */}
      {!result && (
        <section className="brand-surface-raised p-6">
          <p className="eyebrow">New agent</p>
          <h2 className="font-display text-xl text-[var(--ink)] mt-1">
            Agent details
          </h2>

          <form
            className="mt-5 space-y-5"
            onSubmit={(e) => {
              e.preventDefault()
              if (canSubmit) void handleMint()
            }}
          >
            {/* Agent name */}
            <label className="flex flex-col text-sm">
              <span className="eyebrow mb-1.5">Agent name</span>
              <input
                type="text"
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                placeholder="e.g. campaign-researcher"
                maxLength={48}
                aria-invalid={agentName.length > 0 && !nameValid}
                aria-describedby="agent-name-help"
                className="brand-input font-mono-brand"
              />
              <span id="agent-name-help" className="mt-1.5 text-xs">
                {agentName.length > 0 && !nameValid ? (
                  <span className="text-[var(--rose)]">
                    2–49 characters; letters, digits, spaces, hyphens, and
                    underscores.
                  </span>
                ) : (
                  <span className="text-[var(--ink-mute)]">
                    A friendly label for the agent.
                  </span>
                )}
              </span>
            </label>

            {/* Harness */}
            <label className="flex flex-col text-sm">
              <span className="eyebrow mb-1.5">Harness</span>
              <select
                value={harness}
                onChange={(e) => setHarness(e.target.value as AgentHarness)}
                className="brand-input font-mono-brand"
              >
                {HARNESSES.map((h) => (
                  <option key={h.value} value={h.value}>
                    {h.label}
                  </option>
                ))}
              </select>
              <span className="mt-1.5 text-xs text-[var(--ink-mute)]">
                The agent runtime this key is for.
              </span>
            </label>

            {/* TTL */}
            <label className="flex flex-col text-sm">
              <span className="eyebrow mb-1.5">Token lifetime (TTL)</span>
              <input
                type="text"
                value={ttl}
                onChange={(e) => setTtl(e.target.value)}
                placeholder={DEFAULT_TTL}
                aria-invalid={ttl.length > 0 && !ttlValid}
                aria-describedby="ttl-help"
                className="brand-input font-mono-brand"
              />
              <span id="ttl-help" className="mt-1.5 text-xs">
                {ttl.length > 0 && !ttlValid ? (
                  <span className="text-[var(--rose)]">
                    Must be a number followed by{" "}
                    <code className="font-mono-brand">s</code>,{" "}
                    <code className="font-mono-brand">m</code>,{" "}
                    <code className="font-mono-brand">h</code>, or{" "}
                    <code className="font-mono-brand">d</code> (e.g.{" "}
                    <code className="font-mono-brand">60d</code>).
                  </span>
                ) : (
                  <span className="text-[var(--ink-mute)]">
                    How long the token stays valid. Default 60 days.
                  </span>
                )}
              </span>
            </label>

            {submitError && (
              <p
                className="text-sm text-[var(--rose)]"
                data-testid="mint-error"
              >
                {submitError}
              </p>
            )}

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={!canSubmit}
                className={primaryBtn}
              >
                {submitting ? "Minting…" : "Mint agent key"}
              </button>
            </div>
          </form>
        </section>
      )}

      {/* ---------- Completion panel ---------- */}
      {result && <CompletionPanel result={result} />}

      {result && (
        <div className="flex justify-end">
          <button
            type="button"
            className={ghostBtn}
            onClick={() => {
              // Reset to the form for a second mint. The token is dropped
              // from state — it is never recoverable after this.
              setResult(null)
              setAgentName("")
              setHarness("claude-code")
              setTtl(DEFAULT_TTL)
              setSubmitError(null)
            }}
          >
            Mint another
          </button>
        </div>
      )}

      {/* ---------- Existing agent keys ---------- */}
      <section className="brand-surface p-6">
        <p className="eyebrow">Agent keys</p>
        <h2 className="font-display text-lg text-[var(--ink)] mt-1 mb-4">
          Existing agents in this L2
        </h2>
        <AgentKeyTable keys={agentKeys} />
      </section>
    </div>
  )
}
