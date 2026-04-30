import { Search, ShieldX, FileSignature } from "lucide-react";
import type { DemoScenario } from "../fixtures/demoTrace.fixture";

interface Props {
  onRun: (scenario: DemoScenario) => void;
  onSignConsent: () => void;
  hasConsent: boolean;
  busy: boolean;
}

const BUTTONS = [
  {
    id: "run-cross-group",
    label: "Run cross-Group query",
    sub: "acme/eng → acme/solutions",
    Icon: Search,
    tone: "info" as const,
    scenario: "cross-group-query" as DemoScenario,
  },
  {
    id: "try-cross-enterprise",
    label: "Try cross-Enterprise",
    sub: "orion/eng → acme/eng",
    Icon: ShieldX,
    tone: "blocked" as const,
    scenario: null,
  },
  {
    id: "sign-consent",
    label: "Sign cross-Enterprise consent",
    sub: "orion/eng ↔ acme/eng · summary_only",
    Icon: FileSignature,
    tone: "amber" as const,
    scenario: null,
  },
];

export function DemoControls({ onRun, onSignConsent, hasConsent, busy }: Props) {
  return (
    <div data-testid="demo-controls" className="flex items-center gap-2">
      {BUTTONS.map((b) => {
        const isCrossEntButton = b.id === "try-cross-enterprise";
        const isSignButton = b.id === "sign-consent";

        const onClick = () => {
          if (busy) return;
          if (isSignButton) {
            onSignConsent();
            return;
          }
          if (isCrossEntButton) {
            onRun(hasConsent ? "cross-enterprise-consented" : "cross-enterprise-blocked");
            return;
          }
          if (b.scenario) onRun(b.scenario);
        };

        const palette = TONE_PALETTE[b.tone];
        const isDimmedSign = isSignButton && hasConsent;

        return (
          <button
            key={b.id}
            type="button"
            disabled={busy || isDimmedSign}
            title={isDimmedSign ? "Consent already active" : b.label}
            onClick={onClick}
            data-demo-button={b.id}
            className="group relative flex flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left transition-all disabled:cursor-not-allowed disabled:opacity-40"
            style={{
              borderColor: palette.border,
              background: palette.bg,
              boxShadow: palette.shadow,
            }}
          >
            <div className="flex items-center gap-2">
              <b.Icon className="h-3.5 w-3.5" style={{ color: palette.text }} />
              <span
                className="text-[11px] font-semibold uppercase tracking-[0.18em]"
                style={{
                  color: palette.text,
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                }}
              >
                {b.label}
              </span>
            </div>
            <span
              className="text-[9px] tracking-[0.05em] text-white/45"
              style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
            >
              {b.sub}
            </span>
          </button>
        );
      })}
    </div>
  );
}

const TONE_PALETTE = {
  info: {
    border: "rgba(91,208,255,0.45)",
    bg: "linear-gradient(180deg, rgba(91,208,255,0.18), rgba(91,208,255,0.04))",
    shadow: "0 0 24px rgba(91,208,255,0.12)",
    text: "#A4E8FF",
  },
  blocked: {
    border: "rgba(255,92,124,0.45)",
    bg: "linear-gradient(180deg, rgba(255,92,124,0.18), rgba(255,92,124,0.04))",
    shadow: "0 0 24px rgba(255,92,124,0.10)",
    text: "#FF8FA8",
  },
  amber: {
    border: "rgba(255,179,71,0.55)",
    bg: "linear-gradient(180deg, rgba(255,179,71,0.20), rgba(255,179,71,0.05))",
    shadow: "0 0 28px rgba(255,179,71,0.18)",
    text: "#FFB347",
  },
};
