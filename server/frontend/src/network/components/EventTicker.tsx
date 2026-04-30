import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { TopologyResponse } from "../types";

export interface NocEvent {
  id: string;
  ts: number; // epoch ms
  l2_id: string;
  enterprise: string;
  action: "ku.proposed" | "aigrp.signature" | "forward.query" | "consent.signed" | "persona.online" | "ku.confirmed" | "dsn.resolve";
  domain?: string;
  detail?: string;
}

// Synthesise events from topology snapshots.
// Real impl will be a server-sent stream — for now we pulse synthetic events
// at a steady cadence so the ticker always has something to show.
const SAMPLE_DOMAINS = [
  "aws", "terraform", "ecs", "kubernetes", "go", "edge", "auth",
  "react", "typescript", "salesforce", "cloudfront", "iam", "lambda",
];

const SAMPLE_ACTIONS: NocEvent["action"][] = [
  "ku.proposed",
  "aigrp.signature",
  "forward.query",
  "ku.confirmed",
  "persona.online",
  "dsn.resolve",
];

function pickFrom<T>(arr: T[], seed: number): T {
  return arr[Math.floor(seed * arr.length) % arr.length];
}

interface Props {
  topology: TopologyResponse | null;
  injectedEvents?: NocEvent[];
}

export function EventTicker({ topology, injectedEvents }: Props) {
  const [events, setEvents] = useState<NocEvent[]>([]);
  const seqRef = useRef(0);

  // Inject scene events. Deferred to next tick so the effect's setState
  // doesn't trigger a synchronous re-render (react-hooks/set-state-in-effect).
  useEffect(() => {
    if (!injectedEvents || injectedEvents.length === 0) return;
    const t = window.setTimeout(() => {
      setEvents((prev) => [...injectedEvents, ...prev].slice(0, 30));
    }, 0);
    return () => window.clearTimeout(t);
  }, [injectedEvents]);

  // Synthetic event generator — pulse roughly every 1.4–3.2s.
  useEffect(() => {
    if (!topology) return;
    const allL2s = topology.enterprises.flatMap((e) =>
      e.l2s.map((l) => ({ l2_id: l.l2_id, enterprise: e.enterprise })),
    );
    if (allL2s.length === 0) return;

    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const seq = ++seqRef.current;
      const nodeIdx = Math.floor(Math.random() * allL2s.length);
      const node = allL2s[nodeIdx];
      const action = pickFrom(SAMPLE_ACTIONS, Math.random());
      const domain = pickFrom(SAMPLE_DOMAINS, Math.random());
      const evt: NocEvent = {
        id: `evt-${seq}-${Date.now()}`,
        ts: Date.now(),
        l2_id: node.l2_id,
        enterprise: node.enterprise,
        action,
        domain,
        detail: detailFor(action, domain),
      };
      setEvents((prev) => [evt, ...prev].slice(0, 30));
      const next = 1400 + Math.random() * 1800;
      setTimeout(tick, next);
    };
    const initial = setTimeout(tick, 800);
    return () => {
      cancelled = true;
      clearTimeout(initial);
    };
  }, [topology]);

  return (
    <aside
      data-testid="event-ticker"
      className="relative flex w-[300px] flex-col border-l border-white/5 bg-[#06061a]/80"
    >
      <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
        <div className="flex items-center gap-2">
          <span
            className="h-1.5 w-1.5 animate-pulse rounded-full"
            style={{ background: "#7CFFA8", boxShadow: "0 0 8px #7CFFA8" }}
          />
          <span
            className="text-[11px] font-semibold uppercase tracking-[0.28em] text-white/85"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            Live Events
          </span>
        </div>
        <span
          className="text-[9px] uppercase tracking-[0.28em] text-white/35"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          last 30
        </span>
      </div>

      <div
        className="relative flex-1 overflow-y-auto px-2 py-2"
        style={{
          maskImage: "linear-gradient(180deg, transparent 0, #000 12px, #000 calc(100% - 28px), transparent 100%)",
        }}
      >
        <AnimatePresence initial={false}>
          {events.map((e) => (
            <motion.div
              key={e.id}
              layout
              initial={{ opacity: 0, x: 16, backgroundColor: "rgba(124,92,255,0.18)" }}
              animate={{ opacity: 1, x: 0, backgroundColor: "rgba(255,255,255,0)" }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1], backgroundColor: { duration: 1.5 } }}
              className="mb-1 rounded-md border border-white/5 px-3 py-2"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span
                  className="text-[10px] tabular-nums text-white/45"
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  {fmtTime(e.ts)}
                </span>
                <span
                  className={`text-[9px] uppercase tracking-[0.18em] ${
                    e.enterprise === "orion" ? "text-[#B6A0FF]" : "text-[#A4E8FF]"
                  }`}
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  {e.l2_id}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <span
                  className="rounded-sm px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em]"
                  style={{
                    background: actionTint(e.action) + "22",
                    color: actionTint(e.action),
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  }}
                >
                  {e.action}
                </span>
                {e.domain && (
                  <span
                    className="rounded-sm border border-white/10 px-1.5 py-0.5 text-[9px] text-white/55"
                    style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                  >
                    #{e.domain}
                  </span>
                )}
              </div>
              {e.detail && (
                <div
                  className="mt-1 line-clamp-2 text-[10px] text-white/55"
                  style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
                >
                  {e.detail}
                </div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </aside>
  );
}

function fmtTime(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}:${String(d.getUTCSeconds()).padStart(2, "0")}.${String(d.getUTCMilliseconds()).padStart(3, "0").slice(0, 2)}`;
}

function actionTint(action: NocEvent["action"]): string {
  switch (action) {
    case "ku.proposed":
      return "#7C5CFF";
    case "ku.confirmed":
      return "#7CFFA8";
    case "aigrp.signature":
      return "#5BD0FF";
    case "forward.query":
      return "#A4E8FF";
    case "consent.signed":
      return "#FFB347";
    case "persona.online":
      return "#FF8FA8";
    case "dsn.resolve":
      return "#B6A0FF";
  }
}

function detailFor(action: NocEvent["action"], domain: string): string {
  switch (action) {
    case "ku.proposed":
      return `cosine sig ${shortHex()} · #${domain}`;
    case "ku.confirmed":
      return `tier=public · #${domain}`;
    case "aigrp.signature":
      return `peer-mesh sweep · 5 sigs propagated`;
    case "forward.query":
      return `cross-group · policy=summary_only`;
    case "consent.signed":
      return `summary_only · TTL 24h`;
    case "persona.online":
      return `harness=claude-code · #${domain}`;
    case "dsn.resolve":
      return `top-3 returned in <50ms`;
  }
}

function shortHex(): string {
  return Math.random().toString(16).slice(2, 8);
}
