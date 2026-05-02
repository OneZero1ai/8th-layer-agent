import { AnimatePresence, motion } from "framer-motion"
import { Database, Network as NetIcon, Server, Users, X } from "lucide-react"
import { timeAgo } from "../../utils"
import type { TopologyL2 } from "../types"

interface Props {
  l2: TopologyL2 | null
  onClose: () => void
}

const ENTERPRISE_HUE: Record<
  string,
  { from: string; to: string; text: string }
> = {
  orion: { from: "#7C5CFF", to: "#3D2D8F", text: "#B6A0FF" },
  acme: { from: "#5BD0FF", to: "#1E5C7E", text: "#A4E8FF" },
}

export function L2DetailPanel({ l2, onClose }: Props) {
  return (
    <AnimatePresence>
      {l2 && (
        <motion.aside
          data-testid="l2-detail-panel"
          initial={{ x: 380, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: 380, opacity: 0 }}
          transition={{
            type: "tween",
            duration: 0.32,
            ease: [0.22, 1, 0.36, 1],
          }}
          className="absolute right-0 top-0 z-20 flex h-full w-[360px] flex-col border-l border-white/10 bg-[#08081a]/95 backdrop-blur"
          style={{
            boxShadow:
              "-30px 0 60px rgba(0,0,0,0.6), inset 1px 0 0 rgba(124,92,255,0.18)",
          }}
        >
          <DetailContent l2={l2} onClose={onClose} />
        </motion.aside>
      )}
    </AnimatePresence>
  )
}

function DetailContent({
  l2,
  onClose,
}: {
  l2: TopologyL2
  onClose: () => void
}) {
  const ent = l2.l2_id.split("/")[0]
  const hue = ENTERPRISE_HUE[ent] ?? ENTERPRISE_HUE.orion

  return (
    <>
      <div
        className="relative px-5 pt-5 pb-4"
        style={{
          background: `linear-gradient(180deg, ${hue.from}22 0%, transparent 100%)`,
        }}
      >
        <div className="flex items-start justify-between">
          <div>
            <div
              className="text-[10px] uppercase tracking-[0.32em]"
              style={{
                color: hue.text,
                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
              }}
            >
              ◆ L2 / cq Remote
            </div>
            <h3
              className="mt-1 text-xl font-semibold tracking-tight text-white"
              style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
            >
              {l2.l2_id}
            </h3>
          </div>
          <button
            onClick={onClose}
            aria-label="Close detail panel"
            className="rounded-md p-1 text-white/55 hover:bg-white/5 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div
          className="mt-3 flex items-center gap-2 rounded-md border border-white/10 bg-black/35 px-3 py-2"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          <Server className="h-3 w-3 text-white/40" />
          <code className="truncate text-[10px] text-white/65">
            {l2.endpoint_url}
          </code>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-px bg-white/5">
        <Stat icon={Database} label="KUs" value={l2.ku_count} hue={hue.text} />
        <Stat
          icon={NetIcon}
          label="Domains"
          value={l2.domain_count}
          hue={hue.text}
        />
        <Stat icon={Users} label="Peers" value={l2.peer_count} hue={hue.text} />
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        <Section title="AIGRP peers">
          {l2.peers.length === 0 ? (
            <p className="text-[11px] text-white/40">No peers</p>
          ) : (
            <ul className="space-y-1">
              {l2.peers.map((p) => (
                <li
                  key={p.l2_id}
                  className="flex items-center justify-between rounded border border-white/5 bg-white/[0.02] px-3 py-2"
                >
                  <span
                    className="text-[11px] text-white/80"
                    style={{
                      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    }}
                  >
                    {p.l2_id}
                  </span>
                  <span className="text-[10px] text-white/40">
                    {p.last_signature_at
                      ? `↑ ${timeAgo(p.last_signature_at)}`
                      : "—"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="Active personas">
          {l2.active_personas.length === 0 ? (
            <p className="text-[11px] text-white/40">None active</p>
          ) : (
            <ul className="space-y-2">
              {l2.active_personas.map((p) => (
                <li
                  key={p.persona}
                  className="rounded border border-white/5 bg-white/[0.02] p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className="truncate text-[12px] font-semibold text-white"
                      style={{
                        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                      }}
                    >
                      {p.persona}
                    </span>
                    <span className="flex items-center gap-1 text-[10px] text-white/40">
                      <span className="h-1.5 w-1.5 rounded-full bg-[#7CFFA8]" />
                      {timeAgo(p.last_seen_at)}
                    </span>
                  </div>
                  {p.working_dir_hint && (
                    <code
                      className="mt-1 block truncate text-[10px] text-white/45"
                      style={{
                        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                      }}
                    >
                      {p.working_dir_hint}
                    </code>
                  )}
                  {p.expertise_domains.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {p.expertise_domains.map((d) => (
                        <span
                          key={d}
                          className="rounded-sm border border-white/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] text-white/65"
                          style={{
                            fontFamily:
                              "'JetBrains Mono', ui-monospace, monospace",
                          }}
                        >
                          #{d}
                        </span>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="Deployment">
          <div className="space-y-1 text-[11px] text-white/55">
            <Row k="runtime" v="AWS Fargate · us-east-1" />
            <Row k="image" v="ecr.public/onezero1/cq-remote:latest" />
            <Row k="generated" v={l2.generated_at ?? "—"} />
          </div>
        </Section>
      </div>
    </>
  )
}

function Stat({
  icon: Icon,
  label,
  value,
  hue,
}: {
  icon: typeof Server
  label: string
  value: number
  hue: string
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 bg-[#06061a] py-3">
      <Icon className="h-3 w-3" style={{ color: hue }} />
      <div
        className="text-[20px] font-bold leading-none text-white"
        style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
      >
        {value}
      </div>
      <div
        className="text-[9px] uppercase tracking-[0.24em] text-white/40"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        {label}
      </div>
    </div>
  )
}

function Section({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <section className="mb-5">
      <h4
        className="mb-2 text-[10px] uppercase tracking-[0.32em] text-white/40"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        — {title}
      </h4>
      {children}
    </section>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span
        className="text-white/35"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        {k}
      </span>
      <span
        className="truncate text-white/75"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        {v}
      </span>
    </div>
  )
}
