import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { TOUR_STEPS } from "../tour-steps";

interface Props {
  open: boolean;
  step: number;
  onNext: () => void;
  onSkip: () => void;
}

export function OnboardingTour({ open, step, onNext, onSkip }: Props) {
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(onNext, 4200);
    return () => clearTimeout(t);
  }, [open, step, onNext]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onSkip();
      if (e.key === "ArrowRight" || e.key === " ") {
        e.preventDefault();
        onNext();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onNext, onSkip]);

  const current = TOUR_STEPS[step];

  return (
    <AnimatePresence>
      {open && current && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="pointer-events-none absolute inset-0 z-50"
        >
          {/* vignette */}
          <div
            aria-hidden
            className="absolute inset-0"
            style={{
              background: "radial-gradient(ellipse at center, transparent 35%, rgba(0,0,0,0.55) 90%)",
            }}
          />
          <motion.div
            key={step}
            initial={{ opacity: 0, y: 30, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -20 }}
            transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
            className="pointer-events-auto absolute bottom-32 left-1/2 w-[520px] -translate-x-1/2 rounded-lg border border-[#7C5CFF]/35 bg-[#06061a]/95 backdrop-blur"
            style={{
              boxShadow: "0 40px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(124,92,255,0.30), 0 0 60px rgba(124,92,255,0.20)",
            }}
          >
            <div className="flex items-center justify-between border-b border-white/5 px-5 py-2.5">
              <div
                className="text-[10px] uppercase tracking-[0.32em] text-[#7C5CFF]"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                ◆ Tour · {step + 1} of {TOUR_STEPS.length}
              </div>
              <button
                onClick={onSkip}
                className="text-[10px] uppercase tracking-[0.18em] text-white/45 hover:text-white"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                ESC to skip
              </button>
            </div>
            <div className="px-5 py-4">
              <h3
                className="text-[18px] font-semibold tracking-tight text-white"
                style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
              >
                {current.title}
              </h3>
              <p
                className="mt-1.5 text-[12px] leading-relaxed text-white/65"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                {current.body}
              </p>
            </div>
            <div className="flex items-center justify-between border-t border-white/5 px-5 py-2.5">
              <div className="flex gap-1">
                {TOUR_STEPS.map((_, i) => (
                  <span
                    key={i}
                    className="h-0.5 w-5 rounded-full transition-all"
                    style={{
                      background: i <= step ? "#7C5CFF" : "rgba(255,255,255,0.15)",
                    }}
                  />
                ))}
              </div>
              <button
                onClick={onNext}
                className="text-[10px] uppercase tracking-[0.22em] text-white hover:text-[#A4E8FF]"
                style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
              >
                next →
              </button>
            </div>
          </motion.div>

          {/* Bottom hint */}
          <div
            className="absolute bottom-6 left-1/2 -translate-x-1/2 text-[10px] uppercase tracking-[0.32em] text-white/35"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            ⎵ next · ESC skip
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
