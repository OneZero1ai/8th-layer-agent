// Per-peer × per-day success-rate heatmap. Hand-rolled CSS grid; cell tone
// derived from CSS-token color-mix for token-pure styling.

import type { MeshHealth } from "../types"

interface HealthHeatmapProps {
  data: MeshHealth["heatmap"]
}

function cellStyle(rate: number): string {
  // Map rate → emerald (>=0.9), gold (0.6-0.9), rose (<0.6) with alpha by rate.
  if (rate >= 0.9) {
    const a = 22 + Math.round((rate - 0.9) * 200) // 22..42%
    return `color-mix(in srgb, var(--emerald) ${a}%, transparent)`
  }
  if (rate >= 0.6) {
    const a = 18 + Math.round((rate - 0.6) * 60) // 18..36%
    return `color-mix(in srgb, var(--gold) ${a}%, transparent)`
  }
  const a = 18 + Math.round((1 - rate) * 30) // 18..48%
  return `color-mix(in srgb, var(--rose) ${a}%, transparent)`
}

export function HealthHeatmap({ data }: HealthHeatmapProps) {
  if (data.length === 0) {
    return null
  }
  const dayCount = data[0]?.days.length ?? 0
  return (
    <figure
      className="overflow-x-auto m-0"
      data-testid="federation-heatmap"
      aria-label="Per-peer success rate heatmap, last 30 days"
    >
      <div
        className="grid gap-px"
        style={{
          gridTemplateColumns: `minmax(140px,200px) repeat(${dayCount}, 12px)`,
        }}
      >
        {data.map((row) => (
          <div key={row.peer.enterprise_id} className="contents">
            <div
              className="px-2 py-1 truncate font-mono-brand text-[11px] text-[var(--ink-dim)] bg-[var(--surface)] border-r border-[var(--rule)]"
              title={row.peer.enterprise_id}
            >
              {row.peer.display_name}
            </div>
            {row.days.map((d) => (
              <div
                key={d.date}
                className="h-6"
                title={`${row.peer.display_name} · ${d.date} · ${(d.success_rate * 100).toFixed(0)}%`}
                style={{ background: cellStyle(d.success_rate) }}
              />
            ))}
          </div>
        ))}
      </div>
    </figure>
  )
}
