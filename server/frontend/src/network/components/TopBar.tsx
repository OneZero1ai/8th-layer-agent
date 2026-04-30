import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import type { TopologyResponse } from "../types";

interface Props {
  topology: TopologyResponse | null;
  lastUpdated: number | null;
  pollError: string | null;
}

function formatUtc(d: Date): string {
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
function formatLocal(d: Date): string {
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

export function TopBar({ topology, lastUpdated, pollError }: Props) {
  const [now, setNow] = useState(() => new Date());
  const [showLocal, setShowLocal] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const totalKus =
    topology?.enterprises.reduce(
      (s, e) => s + e.l2s.reduce((ss, l) => ss + l.ku_count, 0),
      0,
    ) ?? 0;
  const l2Count =
    topology?.enterprises.reduce((s, e) => s + e.l2s.length, 0) ?? 0;
  const consentCount = topology?.cross_enterprise_consents.length ?? 0;

  const nowMs = now.getTime();
  const stale = lastUpdated === null ? false : nowMs - lastUpdated > 15_000;
  const indicatorColor = pollError || stale ? "#FFB347" : "#7CFFA8";
  const indicatorLabel = pollError ? "fetch error" : stale ? "stale" : "live";

  return (
    <div
      data-testid="noc-topbar"
      className="relative z-10 flex h-16 items-stretch border-b border-white/5 bg-[#06061a]/85 backdrop-blur"
      style={{
        boxShadow: "inset 0 -1px 0 rgba(124,92,255,0.15)",
      }}
    >
      {/* Wordmark */}
      <div className="flex items-center gap-3 px-6 border-r border-white/5">
        <div
          aria-hidden
          className="flex h-9 w-9 items-center justify-center rounded-md"
          style={{
            background:
              "conic-gradient(from 220deg at 50% 50%, #7C5CFF 0%, #5BD0FF 35%, #FFB347 70%, #7C5CFF 100%)",
            boxShadow: "0 0 18px rgba(124,92,255,0.55)",
          }}
        >
          <span
            className="text-base font-black text-[#08081a]"
            style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
          >
            8
          </span>
        </div>
        <div className="leading-tight">
          <div
            className="text-[15px] font-semibold tracking-tight text-white"
            style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
          >
            8th-Layer<span className="text-[#7C5CFF]">.ai</span>
          </div>
          <div
            className="text-[10px] uppercase tracking-[0.32em] text-white/40"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            Live Network
          </div>
        </div>
      </div>

      {/* Live counters */}
      <div className="flex flex-1 items-center gap-8 px-8">
        <Counter label="Knowledge units" value={totalKus} suffix="kus" />
        <Counter label="L2s online" value={l2Count} suffix="/ 6" />
        <Counter label="Cross-Enterprise consents" value={consentCount} suffix="active" />
        <Counter
          label="Last activity"
          value={lastUpdated ? `${Math.max(0, Math.floor((nowMs - lastUpdated) / 1000))}s` : "—"}
          suffix="ago"
        />
      </div>

      {/* Clock + status */}
      <div className="flex items-center gap-6 px-6 border-l border-white/5">
        <button
          onClick={() => setShowLocal((v) => !v)}
          className="text-right leading-tight cursor-pointer"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          title="Click to toggle UTC / Local"
        >
          <div className="text-[18px] font-semibold tabular-nums text-white">
            {showLocal ? formatLocal(now) : formatUtc(now)}
          </div>
          <div className="text-[10px] uppercase tracking-[0.28em] text-white/40">
            {showLocal ? "local" : "utc"}
          </div>
        </button>

        <div
          data-testid="last-updated"
          className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5"
        >
          <span
            className="relative flex h-2 w-2"
            aria-hidden
          >
            <span
              className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-70"
              style={{ background: indicatorColor }}
            />
            <span
              className="relative inline-flex h-2 w-2 rounded-full"
              style={{ background: indicatorColor }}
            />
          </span>
          <span
            className="text-[10px] uppercase tracking-[0.22em] text-white/70"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            {indicatorLabel}
          </span>
          <Activity className="h-3 w-3 text-white/50" strokeWidth={2.4} />
        </div>
      </div>
    </div>
  );
}

function Counter({
  label,
  value,
  suffix,
}: {
  label: string;
  value: number | string;
  suffix?: string;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <div
        className="text-[22px] font-bold tabular-nums leading-none text-white"
        style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
      >
        {value}
      </div>
      {suffix && (
        <div
          className="text-[10px] uppercase tracking-[0.22em] text-[#7C5CFF]"
          style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
        >
          {suffix}
        </div>
      )}
      <div className="ml-1 text-[10px] uppercase tracking-[0.18em] text-white/40">
        {label}
      </div>
    </div>
  );
}
