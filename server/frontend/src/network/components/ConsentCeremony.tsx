import { AnimatePresence, motion } from "framer-motion"
import { Check, FileSignature } from "lucide-react"
import { useEffect, useRef, useState } from "react"

interface Props {
  open: boolean
  onSign: () => Promise<void> | void
  onClose: () => void
}

export function ConsentCeremony({ open, onSign, onClose }: Props) {
  const [signing, setSigning] = useState(false)
  const [signed, setSigned] = useState(false)

  useEffect(() => {
    if (!open) {
      // Defer to next tick to avoid synchronous-setState-in-effect lint rule.
      const t = window.setTimeout(() => {
        setSigning(false)
        setSigned(false)
      }, 0)
      return () => window.clearTimeout(t)
    }
  }, [open])

  const handleSign = async () => {
    if (signing || signed) return
    setSigning(true)
    await onSign()
    setSigned(true)
    setTimeout(() => onClose(), 2400)
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="absolute inset-0 z-40 flex items-center justify-center bg-[#04040f]/55 backdrop-blur-sm"
          onClick={onClose}
        >
          <motion.div
            initial={{ y: 80, opacity: 0, scale: 0.96 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 60, opacity: 0, scale: 0.96 }}
            transition={{
              type: "tween",
              duration: 0.42,
              ease: [0.22, 1, 0.36, 1],
            }}
            onClick={(e) => e.stopPropagation()}
            className="relative w-[560px] overflow-hidden rounded-lg border border-[#FFB347]/35 bg-gradient-to-b from-[#0a0918] to-[#06061a]"
            style={{
              boxShadow:
                "0 60px 120px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,179,71,0.30), 0 0 80px rgba(255,179,71,0.18)",
            }}
          >
            {/* Top ribbon */}
            <div
              className="h-1 w-full"
              style={{
                background:
                  "linear-gradient(90deg, transparent, #FFB347 50%, transparent)",
              }}
            />

            <div className="px-8 pt-8 pb-6">
              <div className="flex items-center gap-3">
                <div
                  className="flex h-10 w-10 items-center justify-center rounded-md border border-[#FFB347]/45 bg-[#FFB347]/10"
                  style={{ boxShadow: "0 0 24px rgba(255,179,71,0.32)" }}
                >
                  <FileSignature className="h-5 w-5 text-[#FFB347]" />
                </div>
                <div>
                  <div
                    className="text-[10px] uppercase tracking-[0.32em] text-[#FFB347]"
                    style={{
                      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    }}
                  >
                    ◆ Consent Ceremony
                  </div>
                  <h2
                    className="text-[20px] font-bold leading-tight tracking-tight text-white"
                    style={{
                      fontFamily: "'Space Grotesk', system-ui, sans-serif",
                    }}
                  >
                    Cross-Enterprise summary-only handshake
                  </h2>
                </div>
              </div>

              <div className="mt-6 rounded-md border border-white/10 bg-black/30 p-5">
                <div
                  className="text-[10px] uppercase tracking-[0.28em] text-white/40"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  parties
                </div>
                <div className="mt-2 flex items-center gap-3">
                  <PartyTile enterprise="orion" group="engineering" />
                  <ArrowAnimated />
                  <PartyTile enterprise="acme" group="engineering" />
                </div>

                <div
                  className="mt-4 grid grid-cols-3 gap-3 text-[10px] uppercase tracking-[0.18em]"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  <Field k="policy" v="summary_only" />
                  <Field k="ttl" v="24 hours" />
                  <Field k="audit" v="signed · timestamped" />
                </div>
              </div>

              <p
                className="mt-4 text-[11px] leading-relaxed text-white/55"
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                }}
              >
                Signing opens a redacted, summary-only path between these two
                L2s. Knowledge bodies remain inside the responding enterprise.
                Either party can revoke at any time.
              </p>

              <SignatureLine signed={signed} signing={signing} />

              <div className="mt-6 flex items-center justify-between gap-3">
                <button
                  onClick={onClose}
                  disabled={signing}
                  className="text-[11px] uppercase tracking-[0.18em] text-white/45 hover:text-white disabled:opacity-40"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  cancel
                </button>
                <button
                  onClick={handleSign}
                  disabled={signing || signed}
                  data-testid="consent-sign"
                  className="group relative overflow-hidden rounded-md border border-[#FFB347]/55 px-6 py-2.5 text-[12px] font-semibold uppercase tracking-[0.22em] text-[#FFB347] transition-all hover:bg-[#FFB347]/10 disabled:opacity-50"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    boxShadow: "0 0 28px rgba(255,179,71,0.22)",
                    background:
                      "linear-gradient(180deg, rgba(255,179,71,0.18), rgba(255,179,71,0.04))",
                  }}
                >
                  {signed ? (
                    <span className="flex items-center gap-2">
                      <Check className="h-4 w-4" />
                      signed
                    </span>
                  ) : signing ? (
                    "signing…"
                  ) : (
                    "▸ sign consent"
                  )}
                </button>
              </div>
            </div>

            {signed && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="border-t border-[#FFB347]/25 bg-[#FFB347]/5 px-8 py-3"
              >
                <div
                  className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-[#FFB347]"
                  style={{
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#FFB347]" />
                  consent active — try the cross-Enterprise query button
                </div>
              </motion.div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

function PartyTile({
  enterprise,
  group,
}: {
  enterprise: string
  group: string
}) {
  const tint =
    enterprise === "orion"
      ? { hue: "#7C5CFF", chip: "#B6A0FF" }
      : { hue: "#5BD0FF", chip: "#A4E8FF" }
  return (
    <div
      className="flex-1 rounded border border-white/10 bg-white/[0.02] p-3"
      style={{ boxShadow: `inset 0 0 0 1px ${tint.hue}22` }}
    >
      <div
        className="text-[9px] uppercase tracking-[0.28em]"
        style={{
          color: tint.chip,
          fontFamily: "'JetBrains Mono', ui-monospace, monospace",
        }}
      >
        enterprise
      </div>
      <div
        className="mt-0.5 text-[14px] font-bold uppercase tracking-tight text-white"
        style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
      >
        {enterprise}
      </div>
      <div
        className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-white/55"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        / {group}
      </div>
    </div>
  )
}

function ArrowAnimated() {
  return (
    <div className="relative w-12">
      <div className="h-px w-full bg-gradient-to-r from-[#7C5CFF] via-[#FFB347] to-[#5BD0FF]" />
      <motion.span
        className="absolute -top-[3px] h-[7px] w-[7px] rounded-full bg-[#FFB347]"
        animate={{ left: ["-4px", "calc(100% - 4px)"] }}
        transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        style={{ boxShadow: "0 0 12px #FFB347" }}
      />
    </div>
  )
}

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-sm border border-white/10 px-2 py-1.5">
      <div className="text-white/40">{k}</div>
      <div className="mt-0.5 text-white/85">{v}</div>
    </div>
  )
}

function SignatureLine({
  signed,
  signing,
}: {
  signed: boolean
  signing: boolean
}) {
  // Animated signature stroke — sketches a flourish across the line.
  const pathRef = useRef<SVGPathElement | null>(null)
  const [length, setLength] = useState(0)
  useEffect(() => {
    if (pathRef.current) {
      setLength(pathRef.current.getTotalLength())
    }
  }, [])

  return (
    <div className="mt-6 rounded border border-white/10 bg-black/40 p-4">
      <div
        className="mb-2 flex items-center justify-between text-[9px] uppercase tracking-[0.28em] text-white/35"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        <span>x — both parties</span>
        <span>2026-04-30 · UTC</span>
      </div>
      <svg viewBox="0 0 480 60" className="h-12 w-full">
        <line
          x1="0"
          y1="50"
          x2="480"
          y2="50"
          stroke="rgba(255,255,255,0.18)"
          strokeWidth="1"
        />
        <motion.path
          ref={pathRef}
          d="M 30 44 C 70 14, 110 70, 150 32 S 220 12, 280 38 S 360 60, 420 28"
          stroke="#FFB347"
          strokeWidth="2.4"
          fill="none"
          strokeLinecap="round"
          style={{
            filter: signing || signed ? "drop-shadow(0 0 6px #FFB347)" : "none",
          }}
          animate={{
            strokeDasharray: length || 1,
            strokeDashoffset: signing || signed ? 0 : length || 1,
          }}
          transition={{ duration: 1.6, ease: "easeInOut" }}
        />
      </svg>
    </div>
  )
}
