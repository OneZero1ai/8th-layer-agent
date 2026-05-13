import { useCallback, useEffect, useState } from "react"
import { api } from "../api"
import type {
  CreateInviteRequest,
  InvitePublic,
  InviteRole,
  InviteStatus,
} from "../types"

const ROLE_OPTIONS: InviteRole[] = ["enterprise_admin", "l2_admin", "user"]

const STATUS_FILTERS: (InviteStatus | "all")[] = [
  "all",
  "pending",
  "claimed",
  "expired",
  "revoked",
]

function statusBadgeClasses(status: string): string {
  switch (status) {
    case "pending":
      return "bg-[color-mix(in_srgb,var(--brand-primary)_14%,transparent)] text-[var(--brand-primary)] border border-[color-mix(in_srgb,var(--brand-primary)_30%,transparent)]"
    case "claimed":
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
    case "expired":
      return "bg-[color-mix(in_srgb,var(--amber)_14%,transparent)] text-[var(--amber)] border border-[color-mix(in_srgb,var(--amber)_30%,transparent)]"
    case "revoked":
      return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
    default:
      return "bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]"
  }
}

function roleBadgeClasses(role: string): string {
  switch (role) {
    case "enterprise_admin":
      return "bg-[color-mix(in_srgb,var(--rose)_14%,transparent)] text-[var(--rose)] border border-[color-mix(in_srgb,var(--rose)_30%,transparent)]"
    case "l2_admin":
      return "bg-[color-mix(in_srgb,var(--violet)_14%,transparent)] text-[var(--violet)] border border-[color-mix(in_srgb,var(--violet)_30%,transparent)]"
    default:
      return "bg-[color-mix(in_srgb,var(--cyan)_14%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_30%,transparent)]"
  }
}

const primaryBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--brand-primary)_18%,transparent)] border border-[color-mix(in_srgb,var(--brand-primary)_45%,transparent)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--brand-primary)] hover:bg-[color-mix(in_srgb,var(--brand-primary)_28%,transparent)] disabled:opacity-50 transition-all"
const destructiveBtn =
  "rounded-md bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] border border-[color-mix(in_srgb,var(--rose)_45%,transparent)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_28%,transparent)] disabled:opacity-50 transition-all"
const inputCls =
  "w-full rounded-md border border-[var(--rule-strong)] bg-[var(--surface-raised)] px-3 py-2 font-mono-brand text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--brand-primary)] focus:ring-1 focus:ring-[color-mix(in_srgb,var(--brand-primary)_40%,transparent)]"

interface CreateForm {
  email: string
  role: InviteRole
  target_l2_id: string
}

export function InvitesPage() {
  const [items, setItems] = useState<InvitePublic[]>([])
  const [count, setCount] = useState(0)
  const [statusFilter, setStatusFilter] = useState<InviteStatus | "all">(
    "pending",
  )
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState<CreateForm>({
    email: "",
    role: "user",
    target_l2_id: "",
  })
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const [revoking, setRevoking] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const status = statusFilter === "all" ? undefined : statusFilter
      const resp = await api.listInvites(status)
      setItems(resp.data)
      setCount(resp.count)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load invites")
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function handleCreate(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setCreating(true)
    setCreateError(null)
    try {
      const body: CreateInviteRequest = {
        email: createForm.email,
        role: createForm.role,
      }
      if (createForm.role !== "enterprise_admin" && createForm.target_l2_id) {
        body.target_l2_id = createForm.target_l2_id
      }
      await api.createInvite(body)
      setShowCreate(false)
      setCreateForm({ email: "", role: "user", target_l2_id: "" })
      await refresh()
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create invite",
      )
    } finally {
      setCreating(false)
    }
  }

  async function handleRevoke(id: number) {
    setRevoking(id)
    try {
      await api.revokeInvite(id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke invite")
    } finally {
      setRevoking(null)
    }
  }

  return (
    <div className="space-y-8">
      <section className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">Admin</p>
          <h1 className="font-display text-3xl text-[var(--ink)] mt-1">
            Invites
          </h1>
          <p className="mt-3 text-sm text-[var(--ink-dim)] leading-relaxed max-w-prose">
            Invite a teammate by email. They&apos;ll receive a magic link that
            registers a passkey on this Enterprise and lands them in the admin
            UI. Invites expire in 24h.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className={primaryBtn}
        >
          + Invite member
        </button>
      </section>

      {error && (
        <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4">
          <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
            {error}
          </p>
        </div>
      )}

      {/* Status filter pills */}
      <div className="flex items-center gap-2">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setStatusFilter(s)}
            className={`rounded-md border px-3 py-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] transition-all ${
              statusFilter === s
                ? "border-[var(--brand-primary)] bg-[color-mix(in_srgb,var(--brand-primary)_15%,transparent)] text-[var(--brand-primary)]"
                : "border-[var(--rule)] bg-[var(--surface)] text-[var(--ink-mute)] hover:bg-[var(--surface-hover)]"
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      <section>
        <div className="flex items-center gap-3 mb-4">
          <h2 className="font-display text-xl text-[var(--ink)]">
            {statusFilter === "all" ? "All invites" : `${statusFilter} invites`}
            <span className="ml-3 font-mono-brand text-[11px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {count} total
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
              No invites in this state
            </span>
            <span className="text-sm text-[var(--ink-mute)]">
              {statusFilter === "pending"
                ? "Send one above to onboard a teammate."
                : "Switch filter to see invites in other states."}
            </span>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-[var(--rule)] bg-[var(--surface-raised)]">
            <div className="grid grid-cols-[1fr_8rem_7rem_10rem_6rem] gap-4 border-b border-[var(--rule)] px-5 py-3">
              {["Email", "Role", "Status", "Expires", "Actions"].map((h) => (
                <span
                  key={h}
                  className="eyebrow text-[var(--ink-mute)] text-left"
                >
                  {h}
                </span>
              ))}
            </div>
            {items.map((inv) => (
              <div
                key={inv.id}
                className="grid grid-cols-[1fr_8rem_7rem_10rem_6rem] gap-4 items-center px-5 py-3.5 border-b border-[var(--rule)] last:border-0 hover:bg-[var(--surface-hover)] transition-colors"
              >
                <div className="min-w-0">
                  <p className="font-display text-sm text-[var(--ink)] truncate">
                    {inv.email}
                  </p>
                  {inv.target_l2_id && (
                    <p className="font-mono-brand text-[10px] text-[var(--ink-mute)] truncate mt-0.5">
                      → {inv.target_l2_id}
                    </p>
                  )}
                </div>
                <div>
                  <span
                    className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${roleBadgeClasses(inv.role)}`}
                  >
                    {inv.role.replace("_", " ")}
                  </span>
                </div>
                <div>
                  <span
                    className={`inline-flex rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(inv.status)}`}
                  >
                    {inv.status}
                  </span>
                </div>
                <p className="font-mono-brand text-[10px] text-[var(--ink-mute)] truncate">
                  {new Date(inv.expires_at).toLocaleString()}
                </p>
                <div>
                  {inv.status === "pending" && (
                    <button
                      type="button"
                      onClick={() => handleRevoke(inv.id)}
                      disabled={revoking === inv.id}
                      className={destructiveBtn}
                    >
                      {revoking === inv.id ? "..." : "Revoke"}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {showCreate && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-[color-mix(in_srgb,var(--bg)_85%,transparent)] backdrop-blur-sm">
          <div className="brand-surface w-full max-w-md rounded-xl p-6">
            <p className="eyebrow">Admin</p>
            <h2 className="font-display text-2xl text-[var(--ink)] mt-1 mb-6">
              Invite a teammate
            </h2>

            <form onSubmit={handleCreate} className="space-y-5">
              <div>
                <label
                  htmlFor="invite-email"
                  className="eyebrow block mb-2 text-[var(--ink-mute)]"
                >
                  Email
                </label>
                <input
                  id="invite-email"
                  type="email"
                  required
                  value={createForm.email}
                  onChange={(e) =>
                    setCreateForm({ ...createForm, email: e.target.value })
                  }
                  className={inputCls}
                  placeholder="teammate@example.com"
                />
              </div>

              <div>
                <label
                  htmlFor="invite-role"
                  className="eyebrow block mb-2 text-[var(--ink-mute)]"
                >
                  Role
                </label>
                <select
                  id="invite-role"
                  value={createForm.role}
                  onChange={(e) =>
                    setCreateForm({
                      ...createForm,
                      role: e.target.value as InviteRole,
                    })
                  }
                  className={inputCls}
                >
                  {ROLE_OPTIONS.map((r) => (
                    <option key={r} value={r}>
                      {r.replace("_", " ")}
                    </option>
                  ))}
                </select>
              </div>

              {createForm.role !== "enterprise_admin" && (
                <div>
                  <label
                    htmlFor="invite-l2"
                    className="eyebrow block mb-2 text-[var(--ink-mute)]"
                  >
                    Target L2 (group)
                  </label>
                  <input
                    id="invite-l2"
                    type="text"
                    required
                    value={createForm.target_l2_id}
                    onChange={(e) =>
                      setCreateForm({
                        ...createForm,
                        target_l2_id: e.target.value,
                      })
                    }
                    className={inputCls}
                    placeholder="e.g. engineering"
                  />
                  <p className="font-mono-brand text-[10px] text-[var(--ink-mute)] mt-2">
                    Which L2 group this teammate joins. Leave empty only for
                    enterprise_admin.
                  </p>
                </div>
              )}

              {createError && (
                <div className="rounded-md border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-3">
                  <p className="text-[var(--rose)] font-mono-brand text-[10px] uppercase tracking-[0.18em]">
                    {createError}
                  </p>
                </div>
              )}

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  disabled={creating}
                  className="flex-1 rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-4 py-2 font-mono-brand text-[11px] uppercase tracking-[0.2em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] disabled:opacity-50 transition-all"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className={`flex-1 ${primaryBtn}`}
                >
                  {creating ? "Sending..." : "Send invite"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
