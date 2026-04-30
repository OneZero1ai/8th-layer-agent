import { useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, Sparkles, Brain, ShieldCheck, ShieldAlert, Lock } from "lucide-react";
import type { DsnResolveResponse, DsnCandidate } from "../fixtures/dsn.fixture";
import { dsnFixtureFor } from "../fixtures/dsn.fixture";

export type DsnPhase = "idle" | "embed" | "fan_out" | "rank" | "results";

interface Props {
  onPhaseChange: (phase: DsnPhase, payload: { topL2Ids: string[]; intent: string } | null) => void;
}

async function resolveIntent(intent: string): Promise<DsnResolveResponse> {
  // Backend not yet live — try, then fall back to fixture.
  try {
    const resp = await fetch("/api/v1/network/dsn/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent, max_candidates: 5 }),
    });
    if (resp.ok) return await resp.json();
  } catch {
    // ignore
  }
  return dsnFixtureFor(intent);
}

export function DsnSearchPanel({ onPhaseChange }: Props) {
  const [intent, setIntent] = useState("");
  const [phase, setPhase] = useState<DsnPhase>("idle");
  const [response, setResponse] = useState<DsnResolveResponse | null>(null);

  const submit = async () => {
    if (!intent.trim()) return;
    setPhase("embed");
    onPhaseChange("embed", { topL2Ids: [], intent });
    const resp = await resolveIntent(intent);
    setResponse(resp);

    // Step the phases on a fixed timeline so the canvas animation is in sync.
    setTimeout(() => {
      setPhase("fan_out");
      onPhaseChange("fan_out", {
        topL2Ids: resp.candidates.map((c) => c.l2_id),
        intent,
      });
    }, 600);
    setTimeout(() => {
      setPhase("rank");
      onPhaseChange("rank", {
        topL2Ids: resp.candidates.slice(0, 3).map((c) => c.l2_id),
        intent,
      });
    }, 1500);
    setTimeout(() => {
      setPhase("results");
      onPhaseChange("results", {
        topL2Ids: resp.candidates.slice(0, 3).map((c) => c.l2_id),
        intent,
      });
    }, 2400);
  };

  const reset = () => {
    setPhase("idle");
    setResponse(null);
    onPhaseChange("idle", null);
  };

  return (
    <div data-testid="dsn-search" className="relative w-full">
      <div className="flex items-center gap-3">
        <div
          className="flex flex-1 items-center gap-3 rounded-md border border-white/10 bg-black/40 px-4 py-3 transition-all focus-within:border-[#7C5CFF]/60 focus-within:bg-black/55"
          style={{
            boxShadow: phase !== "idle" ? "0 0 28px rgba(124,92,255,0.32)" : "none",
          }}
        >
          <Search className="h-4 w-4 text-[#7C5CFF]" />
          <input
            data-testid="dsn-input"
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            placeholder="I need help with…"
            className="flex-1 bg-transparent text-[14px] text-white outline-none placeholder:text-white/30"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          />
          <span
            className="text-[10px] uppercase tracking-[0.28em] text-white/35"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            DSN ↵
          </span>
        </div>
        <button
          type="button"
          onClick={submit}
          disabled={!intent.trim()}
          className="rounded-md border border-[#7C5CFF]/45 bg-gradient-to-br from-[#7C5CFF]/30 to-[#5BD0FF]/15 px-4 py-3 text-[12px] font-semibold uppercase tracking-[0.18em] text-white transition-all hover:from-[#7C5CFF]/45 hover:to-[#5BD0FF]/25 disabled:cursor-not-allowed disabled:opacity-30"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          Resolve
        </button>
      </div>

      <AnimatePresence>
        {phase !== "idle" && (
          <motion.div
            initial={{ opacity: 0, y: 12, height: 0 }}
            animate={{ opacity: 1, y: 0, height: "auto" }}
            exit={{ opacity: 0, y: 12, height: 0 }}
            transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
            className="absolute bottom-full left-0 right-0 mb-3 overflow-hidden rounded-md border border-white/10 bg-[#06061a]/95 backdrop-blur"
            style={{
              boxShadow: "0 30px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(124,92,255,0.18)",
            }}
          >
            <div className="flex items-center justify-between border-b border-white/5 px-4 py-2">
              <div className="flex items-center gap-2">
                <Sparkles className="h-3 w-3 text-[#FFB347]" />
                <span
                  className="text-[10px] uppercase tracking-[0.28em] text-white/85"
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  DSN Resolver
                </span>
              </div>
              <button
                onClick={reset}
                className="text-[10px] uppercase tracking-[0.18em] text-white/45 hover:text-white"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                close
              </button>
            </div>

            <PhaseTimeline phase={phase} response={response} />

            {phase === "results" && response && (
              <CandidateCards candidates={response.candidates} />
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function PhaseTimeline({ phase, response }: { phase: DsnPhase; response: DsnResolveResponse | null }) {
  const steps: Array<{ key: DsnPhase; label: string; sub: string }> = [
    { key: "embed", label: "Embed", sub: "Bedrock Titan v2" },
    { key: "fan_out", label: "Fan-out", sub: "AIGRP × 6 L2s" },
    { key: "rank", label: "Rank", sub: "cosine top-K=3" },
    { key: "results", label: "Policy", sub: "consent overlay" },
  ];
  const order: DsnPhase[] = ["embed", "fan_out", "rank", "results"];
  const idx = order.indexOf(phase);
  const embedding = response?.embedding_preview ?? [];

  return (
    <div className="px-4 py-3">
      <div className="flex items-center gap-2">
        {steps.map((s, i) => {
          const done = i < idx;
          const active = i === idx;
          return (
            <div key={s.key} className="flex flex-1 items-center gap-2">
              <div
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md border ${
                  active
                    ? "border-[#7C5CFF] bg-[#7C5CFF]/20"
                    : done
                    ? "border-[#7CFFA8]/45 bg-[#7CFFA8]/10"
                    : "border-white/10 bg-white/[0.02]"
                }`}
              >
                <span
                  className={`text-[11px] font-bold ${
                    active ? "text-white" : done ? "text-[#7CFFA8]" : "text-white/35"
                  }`}
                  style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
                >
                  {i + 1}
                </span>
              </div>
              <div className="flex flex-col">
                <span
                  className={`text-[10px] uppercase tracking-[0.18em] ${
                    active || done ? "text-white" : "text-white/45"
                  }`}
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  {s.label}
                </span>
                <span
                  className="text-[9px] text-white/35"
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  {s.sub}
                </span>
              </div>
              {i < steps.length - 1 && (
                <div className="mx-1 h-px flex-1 bg-gradient-to-r from-white/10 to-white/[0.02]" />
              )}
            </div>
          );
        })}
      </div>

      {/* embedding sparkline */}
      <div className="mt-3 flex items-end gap-1">
        <Brain className="mb-1 h-3 w-3 text-[#5BD0FF]" />
        <div className="flex h-9 flex-1 items-end gap-[2px]">
          {(embedding.length > 0 ? embedding : Array(16).fill(0)).map((v, i) => {
            const h = 4 + Math.abs(v) * 28;
            const colored = phase !== "idle";
            return (
              <motion.div
                key={i}
                initial={{ height: 0 }}
                animate={{ height: colored ? h : 0 }}
                transition={{ delay: i * 0.018, duration: 0.5 }}
                className="w-[8px] rounded-sm"
                style={{
                  background:
                    v >= 0
                      ? "linear-gradient(180deg, #7C5CFF, #5BD0FF)"
                      : "linear-gradient(180deg, #FFB347, #FF8FA8)",
                }}
              />
            );
          })}
        </div>
        <div
          className="ml-2 text-[9px] uppercase tracking-[0.18em] text-white/35"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          1024d → 16d preview
        </div>
      </div>
    </div>
  );
}

function CandidateCards({ candidates }: { candidates: DsnCandidate[] }) {
  const top = useMemo(() => candidates.slice(0, 3), [candidates]);
  return (
    <div className="grid grid-cols-3 gap-2 border-t border-white/5 px-4 py-3">
      {top.map((c, i) => (
        <motion.div
          key={c.l2_id}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.08 }}
          className="rounded-md border border-white/10 bg-white/[0.03] p-3"
          style={{
            boxShadow: i === 0 ? "0 0 30px rgba(124,92,255,0.22)" : "none",
            borderColor: i === 0 ? "rgba(124,92,255,0.55)" : "rgba(255,255,255,0.10)",
          }}
        >
          <div className="flex items-baseline justify-between">
            <span
              className="text-[10px] uppercase tracking-[0.22em]"
              style={{
                color: c.enterprise === "orion" ? "#B6A0FF" : "#A4E8FF",
                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
              }}
            >
              {c.l2_id}
            </span>
            <span
              className="text-[10px] tabular-nums text-white/55"
              style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
            >
              #{i + 1}
            </span>
          </div>
          <div className="mt-1 flex items-baseline gap-1">
            <span
              className="text-[24px] font-bold leading-none text-white"
              style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
            >
              {c.sim_score.toFixed(2)}
            </span>
            <span
              className="text-[9px] uppercase tracking-[0.18em] text-white/35"
              style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
            >
              cosine
            </span>
          </div>
          <div
            className="mt-1 text-[10px] text-white/55"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            {c.ku_count_in_topic} kus · {c.expert_personas.length} experts
          </div>
          <div className="mt-2">
            <PolicyBadge policy={c.policy_if_queried} />
          </div>
        </motion.div>
      ))}
    </div>
  );
}

function PolicyBadge({ policy }: { policy: DsnCandidate["policy_if_queried"] }) {
  const map = {
    direct: { Icon: ShieldCheck, color: "#7CFFA8", label: "direct" },
    cross_group_summary: { Icon: ShieldCheck, color: "#5BD0FF", label: "x-group · summary" },
    cross_enterprise_blocked: { Icon: Lock, color: "#FF5C7C", label: "x-ent blocked" },
    summary_only: { Icon: ShieldAlert, color: "#FFB347", label: "summary only" },
  } as const;
  const { Icon, color, label } = map[policy];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]"
      style={{
        borderColor: color + "55",
        background: color + "12",
        color,
        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      }}
    >
      <Icon className="h-3 w-3" />
      {label}
    </span>
  );
}
