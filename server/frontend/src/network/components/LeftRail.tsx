import { Cpu, Layers, GitBranch } from "lucide-react";

export type LayerKey = "L1" | "L2" | "L3";

interface Props {
  active: LayerKey;
  onChange: (k: LayerKey) => void;
}

const ITEMS: Array<{
  key: LayerKey;
  label: string;
  sub: string;
  Icon: typeof Cpu;
  enabled: boolean;
}> = [
  { key: "L1", label: "L1", sub: "agents", Icon: Cpu, enabled: true },
  { key: "L2", label: "L2", sub: "commons", Icon: Layers, enabled: true },
  { key: "L3", label: "L3", sub: "broker", Icon: GitBranch, enabled: false },
];

export function LeftRail({ active, onChange }: Props) {
  return (
    <div
      data-testid="layer-rail"
      className="flex w-[88px] flex-col items-stretch border-r border-white/5 bg-[#06061a]/80 py-4"
    >
      <div
        className="mb-4 px-2 text-center text-[9px] uppercase tracking-[0.32em] text-white/30"
        style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
      >
        OSI L8
      </div>
      <div className="flex flex-col gap-2 px-2">
        {ITEMS.map((it) => {
          const isActive = active === it.key;
          return (
            <button
              key={it.key}
              type="button"
              disabled={!it.enabled}
              onClick={() => it.enabled && onChange(it.key)}
              data-testid={`layer-${it.key}`}
              className={`group relative flex flex-col items-center gap-1 rounded-md py-3 transition-all ${
                it.enabled
                  ? "cursor-pointer hover:bg-white/5"
                  : "cursor-not-allowed opacity-30"
              }`}
              style={{
                background: isActive
                  ? "linear-gradient(180deg, rgba(124,92,255,0.22), rgba(91,208,255,0.06))"
                  : "transparent",
                boxShadow: isActive
                  ? "inset 0 0 0 1px rgba(124,92,255,0.55), 0 0 24px rgba(124,92,255,0.18)"
                  : "inset 0 0 0 1px rgba(255,255,255,0.04)",
              }}
            >
              <it.Icon
                className={`h-5 w-5 ${isActive ? "text-[#A4E8FF]" : "text-white/55 group-hover:text-white/80"}`}
                strokeWidth={1.6}
              />
              <span
                className={`text-[12px] font-bold tracking-tight ${isActive ? "text-white" : "text-white/65"}`}
                style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
              >
                {it.label}
              </span>
              <span
                className="text-[8px] uppercase tracking-[0.22em] text-white/40"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                {it.sub}
              </span>
              {!it.enabled && (
                <span className="absolute -right-1 -top-1 rounded-full bg-[#FFB347]/15 px-1.5 py-0.5 text-[8px] uppercase tracking-widest text-[#FFB347]">
                  Q3
                </span>
              )}
            </button>
          );
        })}
      </div>

      <div className="mt-auto px-2">
        <div
          className="rounded-md border border-white/5 bg-black/40 p-3 text-center"
          title="Layer 8 of the OSI model — Semantic Knowledge"
        >
          <div
            className="text-[8px] uppercase tracking-[0.28em] text-white/40"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            stack
          </div>
          <div
            className="mt-1 text-[18px] font-black leading-none text-[#7C5CFF]"
            style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
          >
            L8
          </div>
          <div
            className="mt-1 text-[8px] leading-tight text-white/45"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            semantic
            <br />
            knowledge
          </div>
        </div>
      </div>
    </div>
  );
}
