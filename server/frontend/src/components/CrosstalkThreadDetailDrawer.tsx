// SPDX-License-Identifier: Apache-2.0
//
// Crosstalk thread detail drawer (#171). Same drawer shape as
// PersonaDetailDrawer (#170) — slides in from the right, click outside
// or Esc to close.
//
// Two modes:
//   - mode="in-l2"   → renders /crosstalk/threads/{id} (full thread + messages)
//   - mode="consult" → renders /consults/{id}/messages (inbox-side history)
//
// The consult mode passes the inbox row via `consultMeta` so we can
// render From/To/Status without a second round-trip — peer Enterprise +
// persona context is already in the inbox payload.

import { useCallback, useEffect, useState } from "react"
import { api } from "../api"
import type {
  ConsultMessage,
  ConsultThread,
  CrosstalkThreadWithMessages,
} from "../types"
import { timeAgo } from "../utils"

type Mode = "in-l2" | "consult"

interface InLProps {
  mode: "in-l2"
  threadId: string
  onClose: () => void
  onClosed?: () => void
}

interface ConsultProps {
  mode: "consult"
  threadId: string
  consultMeta: ConsultThread | null
  onClose: () => void
}

type Props = InLProps | ConsultProps

function statusBadgeClasses(status: string): string {
  switch (status) {
    case "open":
    case "received":
    case "acknowledged":
      return "bg-[color-mix(in_srgb,var(--emerald)_14%,transparent)] text-[var(--emerald)] border border-[color-mix(in_srgb,var(--emerald)_30%,transparent)]"
    case "replied":
    case "claimed":
      return "bg-[color-mix(in_srgb,var(--cyan)_18%,transparent)] text-[var(--cyan)] border border-[color-mix(in_srgb,var(--cyan)_38%,transparent)]"
    case "closed":
    case "resolved":
      return "bg-[var(--surface-hover)] text-[var(--ink-mute)] border border-[var(--rule-strong)]"
    default:
      return "bg-[color-mix(in_srgb,var(--gold)_14%,transparent)] text-[var(--gold)] border border-[color-mix(in_srgb,var(--gold)_28%,transparent)]"
  }
}

// Build a markdown export of the in-L2 thread + messages. Tiny renderer
// — no markdown lib import (keeps the bundle delta small). The output
// is meant for incident-review attachment, not pixel-perfect rendering.
function buildMarkdown(payload: CrosstalkThreadWithMessages): string {
  const { thread, messages } = payload
  const header = [
    `# Crosstalk thread: ${thread.subject || "(no subject)"}`,
    "",
    `- Thread ID: \`${thread.id}\``,
    `- Status: ${thread.status}`,
    `- Tenant: ${thread.enterprise_id}/${thread.group_id}`,
    `- Created: ${thread.created_at} by \`${thread.created_by_username}\``,
    `- Participants: ${thread.participants.map((p) => `\`${p}\``).join(", ")}`,
  ]
  if (thread.closed_at) {
    header.push(
      `- Closed: ${thread.closed_at} by \`${thread.closed_by_username ?? "—"}\` (${thread.closed_reason ?? "no reason"})`,
    )
  }
  header.push("", "---", "")
  const body = messages.map((m) => {
    const persona = m.from_persona ? ` (${m.from_persona})` : ""
    return [
      `### ${m.from_username}${persona} — ${m.sent_at}`,
      "",
      m.content,
      "",
    ].join("\n")
  })
  return [...header, ...body].join("\n")
}

function downloadMarkdown(filename: string, body: string) {
  const blob = new Blob([body], { type: "text/markdown" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// ---------------------------------------------------------------------------
// In-L2 thread inner panel
// ---------------------------------------------------------------------------

interface InLPanelProps {
  threadId: string
  onClosed?: () => void
}

function InLThreadPanel({ threadId, onClosed }: InLPanelProps) {
  const [data, setData] = useState<CrosstalkThreadWithMessages | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [closing, setClosing] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      const resp = await api.getCrosstalkThread(threadId, 200)
      setData(resp)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load thread")
    }
  }, [threadId])

  useEffect(() => {
    load()
  }, [load])

  const handleClose = useCallback(async () => {
    if (!data) return
    if (
      !window.confirm(
        `Close thread ${threadId.slice(0, 12)}…? This cannot be undone.`,
      )
    ) {
      return
    }
    setClosing(true)
    try {
      await api.closeCrosstalkThread(threadId, "admin override")
      onClosed?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to close thread")
    } finally {
      setClosing(false)
    }
  }, [data, threadId, onClosed])

  if (error) {
    return (
      <div className="rounded-xl border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] p-4 mt-6">
        <p className="text-[var(--rose)] font-mono-brand text-[11px] uppercase tracking-[0.18em]">
          {error}
        </p>
      </div>
    )
  }
  if (!data) {
    return <p className="mt-6 text-sm text-[var(--ink-mute)]">Loading…</p>
  }

  const { thread, messages } = data
  const isOpen = thread.status === "open"

  return (
    <>
      <section className="mt-6 brand-surface-raised p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <p className="eyebrow">Status</p>
            <span
              className={`mt-1 inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                thread.status,
              )}`}
            >
              {thread.status}
            </span>
          </div>
          <div>
            <p className="eyebrow">Messages</p>
            <p className="mt-1 font-mono-brand tabular-nums text-[var(--ink)]">
              {messages.length}
            </p>
          </div>
          <div>
            <p className="eyebrow">Opened</p>
            <p
              className="mt-1 text-[var(--ink-dim)]"
              title={new Date(thread.created_at).toLocaleString()}
            >
              {timeAgo(thread.created_at)} by{" "}
              <code className="font-mono-brand text-[var(--cyan)]">
                {thread.created_by_username}
              </code>
            </p>
          </div>
          <div>
            <p className="eyebrow">Tenant</p>
            <p className="mt-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)] truncate">
              {thread.enterprise_id}/{thread.group_id}
            </p>
          </div>
        </div>
        <div>
          <p className="eyebrow mb-1">Participants</p>
          <ul className="flex flex-wrap gap-1.5">
            {thread.participants.map((p) => (
              <li
                key={p}
                className="rounded-full border border-[var(--rule-strong)] bg-[var(--surface)] px-2.5 py-0.5 font-mono-brand text-[11px] text-[var(--ink-dim)]"
              >
                {p}
              </li>
            ))}
          </ul>
        </div>
        {thread.closed_at && (
          <div>
            <p className="eyebrow">Closed</p>
            <p className="mt-1 text-sm text-[var(--ink-dim)]">
              {timeAgo(thread.closed_at)} by{" "}
              <code className="font-mono-brand text-[var(--cyan)]">
                {thread.closed_by_username ?? "—"}
              </code>
              {thread.closed_reason ? ` — ${thread.closed_reason}` : ""}
            </p>
          </div>
        )}
        <div className="flex flex-wrap gap-2 pt-2 border-t border-[var(--rule)]">
          <button
            type="button"
            onClick={() =>
              downloadMarkdown(
                `crosstalk-${threadId.slice(0, 12)}.md`,
                buildMarkdown(data),
              )
            }
            className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            Export markdown
          </button>
          {isOpen && (
            <button
              type="button"
              onClick={handleClose}
              disabled={closing}
              className="rounded-md border border-[color-mix(in_srgb,var(--rose)_40%,transparent)] bg-[color-mix(in_srgb,var(--rose)_10%,transparent)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--rose)] hover:bg-[color-mix(in_srgb,var(--rose)_18%,transparent)] disabled:opacity-50 transition-colors"
            >
              {closing ? "Closing…" : "Close thread (admin)"}
            </button>
          )}
        </div>
      </section>

      <section className="mt-6">
        <p className="eyebrow">Timeline</p>
        <h3 className="font-display text-lg text-[var(--ink)] mt-1">
          Messages
        </h3>
        {messages.length === 0 ? (
          <p className="mt-3 text-sm text-[var(--ink-mute)]">
            No messages on this thread yet.
          </p>
        ) : (
          <ol className="mt-3 space-y-3 border-l border-[var(--rule-strong)] pl-4">
            {messages.map((m) => (
              <li key={m.id} className="relative">
                <span
                  aria-hidden="true"
                  className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-[var(--cyan)]"
                />
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="font-mono-brand text-[11px] uppercase tracking-[0.14em] text-[var(--cyan)]">
                    {m.from_username}
                  </span>
                  {m.from_persona && (
                    <span className="font-mono-brand text-[10px] uppercase tracking-[0.14em] text-[var(--violet)]">
                      ({m.from_persona})
                    </span>
                  )}
                  <span
                    className="font-mono-brand text-[10px] text-[var(--ink-mute)]"
                    title={new Date(m.sent_at).toLocaleString()}
                  >
                    {timeAgo(m.sent_at)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-[var(--ink-dim)] whitespace-pre-wrap break-words">
                  {m.content}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>
    </>
  )
}

// ---------------------------------------------------------------------------
// Cross-Enterprise consult inner panel
// ---------------------------------------------------------------------------

interface ConsultPanelProps {
  threadId: string
  meta: ConsultThread | null
}

function ConsultThreadPanel({ threadId, meta }: ConsultPanelProps) {
  const [messages, setMessages] = useState<ConsultMessage[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .consultMessages(threadId)
      .then((resp) => {
        if (!cancelled) setMessages(resp.messages)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load")
        }
      })
    return () => {
      cancelled = true
    }
  }, [threadId])

  return (
    <>
      <section className="mt-6 brand-surface-raised p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <p className="eyebrow">From Enterprise</p>
            <p className="mt-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--violet)]">
              {meta?.from_l2_id ?? "—"}
            </p>
          </div>
          <div>
            <p className="eyebrow">From persona</p>
            <p className="mt-1 font-mono-brand text-[11px] text-[var(--ink-dim)]">
              {meta?.from_persona ?? "—"}
            </p>
          </div>
          <div>
            <p className="eyebrow">To</p>
            <p className="mt-1 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-mute)]">
              {meta?.to_l2_id ?? "—"}
            </p>
            <p className="mt-0.5 font-mono-brand text-[11px] text-[var(--ink-dim)]">
              {meta?.to_persona ?? "—"}
            </p>
          </div>
          <div>
            <p className="eyebrow">Status</p>
            <span
              className={`mt-1 inline-flex items-center rounded-full px-2 py-0.5 font-mono-brand text-[10px] uppercase tracking-[0.16em] ${statusBadgeClasses(
                meta?.status ?? "unknown",
              )}`}
            >
              {meta?.status ?? "—"}
            </span>
          </div>
          {meta?.created_at && (
            <div>
              <p className="eyebrow">Opened</p>
              <p
                className="mt-1 text-[var(--ink-dim)]"
                title={new Date(meta.created_at).toLocaleString()}
              >
                {timeAgo(meta.created_at)}
              </p>
            </div>
          )}
          {meta?.claimed_by && (
            <div>
              <p className="eyebrow">Claimed by</p>
              <p className="mt-1 font-mono-brand text-[11px] text-[var(--cyan)]">
                {meta.claimed_by}
              </p>
            </div>
          )}
        </div>
        {meta?.resolution_summary && (
          <div className="border-t border-[var(--rule)] pt-3">
            <p className="eyebrow">Resolution</p>
            <p className="mt-1 text-sm text-[var(--ink-dim)] whitespace-pre-wrap">
              {meta.resolution_summary}
            </p>
          </div>
        )}
        {/* Routing health is deferred — would need an aggregated read
            over the AIGRP peer + forward-sign tables. Surfaced as a
            placeholder so operator expectations are set. */}
        <div className="border-t border-[var(--rule)] pt-3">
          <p className="eyebrow">Routing</p>
          <p className="mt-1 text-xs text-[var(--ink-mute)]">
            Per-thread peering health (AIGRP latency + signature success-rate)
            requires a follow-up backend aggregate read; the envelope timeline
            below is the current authoritative signal.
          </p>
        </div>
      </section>

      <section className="mt-6">
        <p className="eyebrow">Envelope timeline</p>
        <h3 className="font-display text-lg text-[var(--ink)] mt-1">
          Messages
        </h3>
        {error ? (
          <p className="mt-3 text-sm text-[var(--rose)]">{error}</p>
        ) : messages === null ? (
          <p className="mt-3 text-sm text-[var(--ink-mute)]">Loading…</p>
        ) : messages.length === 0 ? (
          <p className="mt-3 text-sm text-[var(--ink-mute)]">
            No messages mirrored to this L2 yet.
          </p>
        ) : (
          <ol className="mt-3 space-y-3 border-l border-[var(--rule-strong)] pl-4">
            {messages.map((m) => (
              <li key={m.message_id} className="relative">
                <span
                  aria-hidden="true"
                  className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-[var(--violet)]"
                />
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="font-mono-brand text-[10px] uppercase tracking-[0.14em] text-[var(--violet)]">
                    {m.from_l2_id}
                  </span>
                  <span className="font-mono-brand text-[10px] text-[var(--ink-dim)]">
                    {m.from_persona}
                  </span>
                  <span
                    className="font-mono-brand text-[10px] text-[var(--ink-mute)]"
                    title={new Date(m.created_at).toLocaleString()}
                  >
                    {timeAgo(m.created_at)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-[var(--ink-dim)] whitespace-pre-wrap break-words">
                  {m.content}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>
    </>
  )
}

// ---------------------------------------------------------------------------
// Drawer shell
// ---------------------------------------------------------------------------

export function CrosstalkThreadDetailDrawer(props: Props) {
  // Esc to close — mirrors PersonaDetailDrawer's behaviour for muscle
  // memory parity.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") props.onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [props])

  const heading: Mode = props.mode

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="crosstalk-detail-heading"
      className="fixed inset-0 z-30 flex justify-end"
    >
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <button
        type="button"
        aria-label="Close thread detail"
        className="flex-1 bg-black/65 backdrop-blur-sm"
        onClick={props.onClose}
      />
      <aside className="w-full max-w-xl overflow-y-auto bg-[var(--bg-via)] border-l border-[var(--rule-strong)] p-6 shadow-[0_0_80px_rgba(0,0,0,0.7)]">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="eyebrow">
              {heading === "in-l2"
                ? "Crosstalk thread"
                : "Cross-Enterprise consult"}
            </p>
            <h2
              id="crosstalk-detail-heading"
              className="font-display text-2xl text-[var(--ink)] mt-1 break-all"
            >
              {props.threadId}
            </h2>
          </div>
          <button
            type="button"
            onClick={props.onClose}
            className="rounded-md border border-[var(--rule-strong)] bg-[var(--surface)] px-3 py-1.5 font-mono-brand text-[10px] uppercase tracking-[0.18em] text-[var(--ink-dim)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            Close
          </button>
        </div>

        {props.mode === "in-l2" ? (
          <InLThreadPanel threadId={props.threadId} onClosed={props.onClosed} />
        ) : (
          <ConsultThreadPanel
            threadId={props.threadId}
            meta={props.consultMeta}
          />
        )}
      </aside>
    </div>
  )
}
