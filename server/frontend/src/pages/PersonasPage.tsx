import { useCallback, useEffect, useState } from "react"
import { api } from "../api"
import type { PersonaAssignment, PersonaListResponse } from "../types"

const PERSONA_OPTIONS = [
  "admin",
  "viewer",
  "agent",
  "external-collaborator",
] as const

type Persona = (typeof PERSONA_OPTIONS)[number]

// Persona badge colour mapping — follows the existing design system tokens.
function personaBadgeClasses(persona: string): string {
  switch (persona) {
    case "admin":
      return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
    case "agent":
      return "bg-[color-mix(in_srgb,var(--cyan)_14%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_30%,transparent)]"
    case "external-collaborator":
      return "bg-[color-mix(in_srgb,var(--violet)_14%,transparent)] text-[var(--violet)] border border-[color-mix(in_srgb,var(--violet)_30%,transparent)]"
    default:
      // viewer
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
  }
}

// Button style tokens — mirrors ApiKeysPage pattern.
const primaryBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"
const destructiveBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] border border-[color-mix(in_srgb,var(--rose)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_28%,transparent)] disabled:opacity-50 transition-all"
const ghostBtn =
  "rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"

interface CreateForm {
  email: string
  username: string
  persona: Persona
}

interface EditState {
  username: string
  persona: Persona
}

interface DisablePrompt {
  username: string
}

export function PersonasPage() {
  const [items, setItems] = useState<PersonaAssignment[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Create modal state.
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState<CreateForm>({
    email: "",
    username: "",
    persona: "viewer",
  })
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  // Edit modal state.
  const [editState, setEditState] = useState<EditState | null>(null)
  const [patching, setPatching] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  // Disable prompt state.
  const [disablePrompt, setDisablePrompt] = useState<DisablePrompt | null>(null)
  const [disabling, setDisabling] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const resp: PersonaListResponse = await api.listPersonas()
      setItems(resp.items)
      setTotal(resp.total)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load personas")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function handleCreate(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setCreating(true)
    setCreateError(null)
    try {
      await api.createPersona(
        createForm.email,
        createForm.username,
        createForm.persona,
      )
      setShowCreate(false)
      setCreateForm({ email: "", username: "", persona: "viewer" })
      await refresh()
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create persona",
      )
    } finally {
      setCreating(false)
    }
  }

  async function handlePatch(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!editState) return
    setPatching(true)
    setEditError(null)
    try {
      await api.patchPersona(editState.username, editState.persona)
      setEditState(null)
      await refresh()
    } catch (err) {
      setEditError(
        err instanceof Error ? err.message : "Failed to update persona",
      )
    } finally {
      setPatching(false)
    }
  }

  async function confirmDisable() {
    if (!disablePrompt) return
    setDisabling(true)
    try {
      await api.disablePersona(disablePrompt.username)
      setDisablePrompt(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to disable persona")
    } finally {
      setDisabling(false)
    }
  }

  return (
    <div className="space-y-8">
      <section className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">Admin</p>
          <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
            Personas
          </h1>
          <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
            Manage Human personas on this L2. Personas gate access level:
            admin, viewer, agent, or external-collaborator. Assigning a persona
            sends a magic-link invite.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className={primaryBtn}
        >
          + Add persona
        </button>
      </section>

      {error && (
        <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4">
          <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
            {error}
          </p>
        </div>
      )}

      <section>
        <div className="flex items-center gap-3 mb-4">
          <h2 className="font-display text-xl text-[var(--ink)]">
            All personas
            <span className="ml-3 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {total} total
            </span>
          </h2>
        </div>

        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="brand-surface h-14 animate-pulse rounded-xl"
              />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="brand-surface flex flex-col items-center justify-center py-12 gap-3">
            <span
              aria-hidden="true"
              className="font-display text-3xl text-[var(--ink-faint)]"
            >
              ∅
            </span>
            <span className="eyebrow text-[var(--brand-primary)]">
              No personas yet
            </span>
            <span className="text-sm text-[var(--ink-mute)]">
              Add one above to onboard a Human.
            </span>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
            {/* Table header */}
            <div className="grid grid-cols-[1fr_10rem_8rem_8rem_6rem] gap-4 border-b border-[var(--rule)] px-5 py-3">
              {["Username / Email", "Persona", "Status", "Assigned by", "Actions"].map(
                (h) => (
                  <span
                    key={h}
                    className="eyebrow text-[var(--ink-mute)] text-left"
                  >
                    {h}
                  </span>
                ),
              )}
            </div>
            {/* Rows */}
            {items.map((item) => {
              const isDisabled = item.disabled_at !== null
              return (
                <div
                  key={item.username}
                  className={`grid grid-cols-[1fr_10rem_8rem_8rem_6rem] gap-4 items-center px-5 py-3.5 border-b border-[var(--rule)] last:border-0 transition-colors ${isDisabled ? "opacity-50" : "hover:bg-[var(--surface-hover)]"}`}
                >
                  {/* Identity */}
                  <div className="min-w-0">
                    <p className="font-display text-sm text-[var(--ink)] truncate">
                      {item.username}
                    </p>
                    {item.email && (
                      <p className="font-mono-brand text-[10px] text-[var(--ink-mute)] truncate mt-0.5">
                        {item.email}
                      </p>
                    )}
                  </div>

                  {/* Persona badge */}
                  <div>
                    <span
                      className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${personaBadgeClasses(item.persona)}`}
                    >
                      {item.persona}
                    </span>
                  </div>

                  {/* Status */}
                  <div>
                    {isDisabled ? (
                      <span className="inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]">
                        Disabled
                      </span>
                    ) : (
                      <span className="inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]">
                        Active
                      </span>
                    )}
                  </div>

                  {/* Assigned by */}
                  <p className="font-mono-brand text-[10px] text-[var(--ink-mute)] truncate">
                    {item.assigned_by}
                  </p>

                  {/* Actions */}
                  <div className="flex items-center gap-2">
                    {!isDisabled && (
                      <>
                        <button
                          type="button"
                          aria-label={`Edit persona for ${item.username}`}
                          onClick={() =>
                            setEditState({
                              username: item.username,
                              persona: item.persona as Persona,
                            })
                          }
                          className="rounded border border-[var(--rule-strong)] bg-[var(--surface)] px-2 py-1 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] transition-colors"
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          aria-label={`Disable persona for ${item.username}`}
                          onClick={() =>
                            setDisablePrompt({ username: item.username })
                          }
                          className="rounded border border-[color-mix(in_srgb,var(--rose)_28%,transparent)] bg-[color-mix(in_srgb,var(--rose)_8%,transparent)] px-2 py-1 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] transition-colors"
                        >
                          Disable
                        </button>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* Create modal */}
      {showCreate && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-persona-heading"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4"
        >
          <div className="w-full max-w-md brand-surface-raised p-6 shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]">
            <p className="eyebrow">Add persona</p>
            <h3
              id="create-persona-heading"
              className="font-display text-xl text-[var(--ink)] mt-1"
            >
              Invite a Human
            </h3>
            <form onSubmit={handleCreate} className="mt-5 grid gap-4">
              <label className="flex flex-col text-sm">
                <span className="eyebrow mb-1.5">Email</span>
                <input
                  type="email"
                  required
                  value={createForm.email}
                  onChange={(e) =>
                    setCreateForm((f) => ({ ...f, email: e.target.value }))
                  }
                  placeholder="alice@example.com"
                  className="brand-input"
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="eyebrow mb-1.5">Username</span>
                <input
                  type="text"
                  required
                  maxLength={64}
                  value={createForm.username}
                  onChange={(e) =>
                    setCreateForm((f) => ({ ...f, username: e.target.value }))
                  }
                  placeholder="alice"
                  className="brand-input"
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="eyebrow mb-1.5">Persona</span>
                <select
                  value={createForm.persona}
                  onChange={(e) =>
                    setCreateForm((f) => ({
                      ...f,
                      persona: e.target.value as Persona,
                    }))
                  }
                  className="brand-input"
                >
                  {PERSONA_OPTIONS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              {createError && (
                <p className="text-sm text-[var(--rose)]">{createError}</p>
              )}
              <div className="flex justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => {
                    setShowCreate(false)
                    setCreateError(null)
                  }}
                  disabled={creating}
                  className={ghostBtn}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={
                    creating ||
                    !createForm.email ||
                    !createForm.username
                  }
                  className={primaryBtn}
                >
                  {creating ? "Creating…" : "Create & invite"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit persona modal */}
      {editState && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="edit-persona-heading"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4"
        >
          <div className="w-full max-w-sm brand-surface-raised p-6 shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]">
            <p className="eyebrow">Edit persona</p>
            <h3
              id="edit-persona-heading"
              className="font-display text-xl text-[var(--ink)] mt-1"
            >
              {editState.username}
            </h3>
            <form onSubmit={handlePatch} className="mt-5 grid gap-4">
              <label className="flex flex-col text-sm">
                <span className="eyebrow mb-1.5">Persona</span>
                <select
                  value={editState.persona}
                  onChange={(e) =>
                    setEditState((s) =>
                      s ? { ...s, persona: e.target.value as Persona } : s,
                    )
                  }
                  className="brand-input"
                >
                  {PERSONA_OPTIONS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              {editError && (
                <p className="text-sm text-[var(--rose)]">{editError}</p>
              )}
              <div className="flex justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditState(null)
                    setEditError(null)
                  }}
                  disabled={patching}
                  className={ghostBtn}
                >
                  Cancel
                </button>
                <button type="submit" disabled={patching} className={primaryBtn}>
                  {patching ? "Saving…" : "Save"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Disable confirm dialog */}
      {disablePrompt && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="disable-persona-heading"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4"
        >
          <div className="w-full max-w-md brand-surface-raised p-6 shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]">
            <p className="eyebrow text-[var(--rose)]">Destructive</p>
            <h3
              id="disable-persona-heading"
              className="font-display text-xl text-[var(--ink)] mt-1"
            >
              Disable &ldquo;{disablePrompt.username}&rdquo;?
            </h3>
            <p className="mt-2 text-sm text-[var(--ink-dim)]">
              The Human&apos;s persona assignment will be soft-disabled. Their
              account is not deleted — re-assign via Edit to re-enable.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDisablePrompt(null)}
                disabled={disabling}
                className={ghostBtn}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmDisable}
                disabled={disabling}
                className={destructiveBtn}
              >
                {disabling ? "Disabling…" : "Disable"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
