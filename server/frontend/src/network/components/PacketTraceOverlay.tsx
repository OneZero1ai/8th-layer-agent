import { AnimatePresence, motion } from "framer-motion"
import { RotateCcw, ShieldOff, X } from "lucide-react"
import { useEffect, useState } from "react"
import type {
  DemoTraceResponse,
  RedactedKuResult,
  TraceEvent,
} from "../fixtures/demoTrace.fixture"

interface Props {
  trace: DemoTraceResponse | null
  onReplay: () => void
  onClose: () => void
}

export function PacketTraceOverlay({ trace, onReplay, onClose }: Props) {
  const [stepIdx, setStepIdx] = useState(0)
  const [showResults, setShowResults] = useState(false)

  // Step the timeline as the scene plays.
  useEffect(() => {
    if (!trace) {
      setStepIdx(0)
      setShowResults(false)
      return
    }
    setStepIdx(0)
    setShowResults(false)

    let cancelled = false
    const totalSteps = trace.events.length
    const baseDelay = 700
    for (let i = 0; i < totalSteps; i++) {
      setTimeout(() => {
        if (cancelled) return
        setStepIdx(i + 1)
      }, i * baseDelay)
    }
    setTimeout(
      () => {
        if (!cancelled) setShowResults(true)
      },
      totalSteps * baseDelay + 200,
    )

    return () => {
      cancelled = true
    }
  }, [trace])

  return (
    <AnimatePresence>
      {trace && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="pointer-events-none absolute inset-0 z-30"
        >
          {/* Trace timeline (top-left) */}
          <motion.div
            initial={{ opacity: 0, x: -30 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -30 }}
            className="pointer-events-auto absolute left-6 top-6 w-[360px] rounded-lg border border-white/10 bg-[#06061a]/95 backdrop-blur"
            style={{
              boxShadow:
                "0 30px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(124,92,255,0.20)",
            }}
          >
            <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
              <div>
                <div
                  className="text-[10px] uppercase tracking-[0.32em] text-[#5BD0FF]"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  ◆ Packet Trace
                </div>
                <div
                  className="mt-0.5 text-[14px] font-semibold text-white"
                  style={{
                    fontFamily: "'Space Grotesk', system-ui, sans-serif",
                  }}
                >
                  {scenarioTitle(trace.scenario)}
                </div>
              </div>
              <button
                onClick={onClose}
                className="rounded text-white/55 hover:text-white"
                aria-label="Close trace"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="max-h-[420px] overflow-y-auto px-4 py-3">
              <ol className="relative space-y-2">
                <span className="absolute left-[12px] top-2 bottom-2 w-px bg-white/10" />
                {trace.events.map((evt, i) => (
                  <TraceRow
                    key={evt.step}
                    evt={evt}
                    revealed={i < stepIdx}
                    active={i === stepIdx - 1}
                  />
                ))}
              </ol>

              <div className="mt-3 flex items-center justify-between rounded-md border border-white/10 bg-black/30 px-3 py-2">
                <span
                  className="text-[10px] uppercase tracking-[0.22em] text-white/45"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  total
                </span>
                <span
                  className="text-[14px] font-bold tabular-nums text-white"
                  style={{
                    fontFamily: "'Space Grotesk', system-ui, sans-serif",
                  }}
                >
                  {trace.total_latency_ms}ms
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2 border-t border-white/5 px-4 py-2">
              <button
                onClick={onReplay}
                className="flex items-center gap-1.5 rounded border border-white/10 bg-white/5 px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-white/75 hover:bg-white/10"
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                }}
              >
                <RotateCcw className="h-3 w-3" />
                replay
              </button>
              <button
                onClick={onClose}
                className="text-[10px] uppercase tracking-[0.18em] text-white/45 hover:text-white"
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                }}
              >
                back to topology
              </button>
            </div>
          </motion.div>

          {/* Result panel (rises from bottom) */}
          <AnimatePresence>
            {showResults && (
              <motion.div
                key="results"
                initial={{ opacity: 0, y: 60 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 60 }}
                transition={{
                  type: "tween",
                  duration: 0.4,
                  ease: [0.22, 1, 0.36, 1],
                }}
                className="pointer-events-auto absolute bottom-6 left-1/2 w-[680px] -translate-x-1/2 rounded-lg border border-white/10 bg-[#06061a]/95 backdrop-blur"
                style={{
                  boxShadow:
                    "0 40px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(124,92,255,0.20)",
                }}
              >
                <div className="flex items-center justify-between border-b border-white/5 px-5 py-3">
                  <div>
                    <div
                      className="text-[10px] uppercase tracking-[0.32em] text-[#FFB347]"
                      style={{
                        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                      }}
                    >
                      ◆ Knowledge Returned
                    </div>
                    <div
                      className="mt-0.5 text-[14px] font-semibold text-white"
                      style={{
                        fontFamily: "'Space Grotesk', system-ui, sans-serif",
                      }}
                    >
                      {trace.final_results.length} result
                      {trace.final_results.length === 1 ? "" : "s"} · policy
                      boundary respected
                    </div>
                  </div>
                  <span
                    className="rounded-sm border border-white/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.22em] text-white/55"
                    style={{
                      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    }}
                  >
                    {trace.scenario}
                  </span>
                </div>
                <div className="grid grid-cols-3 gap-3 px-5 py-4">
                  {trace.final_results.map((r, i) => (
                    <KuResultCard key={r.ku_id + i} r={r} />
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

function scenarioTitle(s: DemoTraceResponse["scenario"]): string {
  switch (s) {
    case "cross-group-query":
      return "Cross-Group query (intra-Enterprise)"
    case "cross-enterprise-blocked":
      return "Cross-Enterprise — no consent"
    case "cross-enterprise-consented":
      return "Cross-Enterprise — consented"
  }
}

function TraceRow({
  evt,
  revealed,
  active,
}: {
  evt: TraceEvent
  revealed: boolean
  active: boolean
}) {
  return (
    <motion.li
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: revealed ? 1 : 0.25, x: 0 }}
      transition={{ duration: 0.3 }}
      className="relative pl-8"
    >
      <span
        className="absolute left-1.5 top-1.5 flex h-4 w-4 items-center justify-center rounded-full"
        style={{
          background: active ? "#7C5CFF" : revealed ? "#5BD0FF" : "#24244c",
          boxShadow: active ? "0 0 16px #7C5CFF" : "none",
        }}
      >
        <span className="text-[8px] font-bold text-[#08081a]">{evt.step}</span>
      </span>
      <div className="flex items-baseline justify-between gap-2">
        <span
          className="text-[10px] uppercase tracking-[0.18em] text-white/85"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          {evt.action.replace(/_/g, " ")}
        </span>
        <span
          className="text-[9px] tabular-nums text-white/50"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          {evt.latency_ms}ms
        </span>
      </div>
      <div
        className="text-[10px] text-white/55"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        @ {evt.l2_id}
      </div>
      <div
        className="mt-0.5 text-[10px] text-white/40"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        ↳ {evt.payload_preview}
      </div>
    </motion.li>
  )
}

function KuResultCard({ r }: { r: RedactedKuResult }) {
  if (r.policy === "blocked") {
    return (
      <div
        className="rounded-md border border-[#FF5C7C]/45 bg-[#FF5C7C]/10 p-3"
        style={{ boxShadow: "0 0 24px rgba(255,92,124,0.12)" }}
      >
        <div className="flex items-center gap-2">
          <ShieldOff className="h-3.5 w-3.5 text-[#FF8FA8]" />
          <span
            className="text-[10px] uppercase tracking-[0.22em] text-[#FF8FA8]"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            Cross-Enterprise blocked
          </span>
        </div>
        <p
          className="mt-2 text-[11px] leading-relaxed text-white/55"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          {r.reason ?? "No active peering agreement. Boundary held."}
        </p>
      </div>
    )
  }
  return (
    <div className="rounded-md border border-white/10 bg-white/[0.03] p-3">
      <div
        className="text-[10px] uppercase tracking-[0.22em] text-[#FFB347]"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        ◆ {r.l2_id}
      </div>
      <h4
        className="mt-1 line-clamp-2 text-[12px] font-semibold leading-snug text-white"
        style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
      >
        {r.title}
      </h4>
      <p
        className="mt-1 line-clamp-2 text-[10px] leading-snug text-white/55"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        {r.summary}
      </p>
      <div className="mt-2 flex flex-wrap gap-1">
        {r.domain_tags.slice(0, 3).map((t) => (
          <span
            key={t}
            className="rounded-sm border border-white/10 px-1.5 py-0.5 text-[8px] uppercase tracking-[0.14em] text-white/55"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            #{t}
          </span>
        ))}
      </div>
      <div className="mt-2 rounded-sm border border-white/10 bg-black/30 px-2 py-1.5">
        <div
          className="text-[8px] uppercase tracking-[0.22em] text-white/35"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          body
        </div>
        <div
          className="mt-0.5 text-[10px] italic text-white/40"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          [hidden — policy: {r.policy}]
        </div>
      </div>
    </div>
  )
}
