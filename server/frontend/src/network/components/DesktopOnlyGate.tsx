import { useEffect, useState } from "react";
import { Monitor } from "lucide-react";

interface Props {
  children: React.ReactNode;
  minWidth?: number;
}

export function DesktopOnlyGate({ children, minWidth = 1280 }: Props) {
  const [width, setWidth] = useState<number | null>(null);

  useEffect(() => {
    const update = () => setWidth(window.innerWidth);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  if (width !== null && width < minWidth) {
    return (
      <div className="flex h-full items-center justify-center bg-[#06061a] p-12">
        <div className="max-w-md text-center">
          <Monitor className="mx-auto h-10 w-10 text-[#7C5CFF]" strokeWidth={1.4} />
          <h2
            className="mt-4 text-[20px] font-semibold tracking-tight text-white"
            style={{ fontFamily: "'Space Grotesk', system-ui, sans-serif" }}
          >
            Best viewed on desktop
          </h2>
          <p
            className="mt-2 text-[12px] leading-relaxed text-white/55"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            The Network Operations Center is dense — it needs at least {minWidth}px of width.
            Switch to a desktop browser to see the full topology.
          </p>
          <div
            className="mt-3 text-[10px] uppercase tracking-[0.28em] text-white/35"
            style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}
          >
            current: {width}px · need {minWidth}px+
          </div>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
