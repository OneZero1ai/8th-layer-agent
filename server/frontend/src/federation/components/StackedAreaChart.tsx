// Hand-rolled stacked-area SVG. Avoids importing recharts' AreaChart module
// (which was the difference between the +5KB gzip ceiling and a 9KB miss).
// LineChart is already in the bundle from DashboardPage; AreaChart is not.
//
// Layout: x-axis = time (auto-fit), y-axis = stacked total. Polygon fills
// stack from bottom up in series order. Hover tooltip is a thin column +
// floating label — same dark surface as recharts custom tooltips.

import { useState } from "react"

export interface StackedDatum {
  date: string
  [key: string]: string | number
}

export interface StackSeries {
  key: string
  label: string
  color: string
}

interface StackedAreaChartProps {
  data: StackedDatum[]
  series: StackSeries[]
  height?: number
}

export function StackedAreaChart({
  data,
  series,
  height = 192,
}: StackedAreaChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)
  if (data.length === 0) return null

  const padX = 32
  const padTop = 8
  const padBottom = 24
  const width = 720 // viewBox-internal; SVG scales via CSS
  const innerW = width - padX * 2
  const innerH = height - padTop - padBottom

  // Per-row totals → max for y-scale.
  const rowTotals = data.map((d) =>
    series.reduce((acc, s) => acc + Number(d[s.key] ?? 0), 0),
  )
  const maxTotal = Math.max(1, ...rowTotals)

  const xFor = (i: number) =>
    padX + (data.length === 1 ? innerW / 2 : (i / (data.length - 1)) * innerW)
  const yFor = (v: number) => padTop + innerH - (v / maxTotal) * innerH

  // Build stacked layers (cumulative).
  const layers: Array<{
    s: StackSeries
    pts: Array<[number, number, number]>
  }> = []
  const cumulative = new Array(data.length).fill(0)
  for (const s of series) {
    const pts: Array<[number, number, number]> = data.map((d, i) => {
      const base = cumulative[i] as number
      const v = Number(d[s.key] ?? 0)
      cumulative[i] = base + v
      return [xFor(i), yFor(base), yFor(base + v)]
    })
    layers.push({ s, pts })
  }

  function pathFor(layer: (typeof layers)[number]): string {
    // Top edge L→R, bottom edge R→L, closed.
    const top = layer.pts.map(([x, , yTop]) => `${x},${yTop}`).join(" L")
    const bot = [...layer.pts]
      .reverse()
      .map(([x, yBase]) => `${x},${yBase}`)
      .join(" L")
    return `M${top} L${bot} Z`
  }

  const ticks = [0, 0.5, 1].map((t) => Math.round(maxTotal * t))

  function handleMove(e: React.MouseEvent<SVGSVGElement>) {
    const svg = e.currentTarget
    const rect = svg.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * width
    if (px < padX || px > width - padX) {
      setHoverIdx(null)
      return
    }
    const idx = Math.round(((px - padX) / innerW) * (data.length - 1))
    if (idx >= 0 && idx < data.length) setHoverIdx(idx)
  }

  return (
    <div className="relative w-full" style={{ height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="w-full h-full"
        role="img"
        aria-label="Stacked area chart of consult outcomes over time"
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        <title>Consult outcomes over time</title>
        {/* y gridlines */}
        {ticks.map((t) => (
          <line
            key={t}
            x1={padX}
            x2={width - padX}
            y1={yFor(t)}
            y2={yFor(t)}
            stroke="rgba(255,255,255,0.06)"
            strokeWidth={1}
          />
        ))}
        {/* layers */}
        {layers.map((layer) => (
          <path
            key={layer.s.key}
            d={pathFor(layer)}
            fill={layer.s.color}
            fillOpacity={0.42}
            stroke={layer.s.color}
            strokeOpacity={0.7}
            strokeWidth={1}
          />
        ))}
        {/* y-axis labels */}
        {ticks.map((t) => (
          <text
            key={`lbl-${t}`}
            x={padX - 6}
            y={yFor(t) + 3}
            textAnchor="end"
            fontFamily="var(--font-mono)"
            fontSize={9}
            fill="rgba(230,230,230,0.42)"
          >
            {t}
          </text>
        ))}
        {/* x-axis labels — first, mid, last */}
        {[0, Math.floor(data.length / 2), data.length - 1].map((i) => (
          <text
            key={`x-${i}`}
            x={xFor(i)}
            y={height - 6}
            textAnchor="middle"
            fontFamily="var(--font-mono)"
            fontSize={9}
            fill="rgba(230,230,230,0.42)"
          >
            {data[i]?.date.slice(5) ?? ""}
          </text>
        ))}
        {hoverIdx !== null && (
          <line
            x1={xFor(hoverIdx)}
            x2={xFor(hoverIdx)}
            y1={padTop}
            y2={height - padBottom}
            stroke="rgba(91,208,255,0.5)"
            strokeWidth={1}
            strokeDasharray="2 2"
          />
        )}
      </svg>
      {/* legend */}
      <div className="absolute right-2 top-1 flex flex-wrap gap-3">
        {series.map((s) => (
          <span
            key={s.key}
            className="inline-flex items-center gap-1 font-mono-brand text-[10px] uppercase tracking-[0.16em] text-[var(--ink-mute)]"
          >
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ background: s.color }}
            />
            {s.label}
          </span>
        ))}
      </div>
      {hoverIdx !== null && data[hoverIdx] && (
        <div
          className="pointer-events-none absolute top-1 left-2 rounded-md border border-[var(--rule-strong)] bg-[var(--bg-via)] px-2 py-1.5 font-mono-brand text-[11px] text-[var(--ink-dim)]"
          role="status"
        >
          <p className="text-[var(--ink)]">{data[hoverIdx].date}</p>
          {series.map((s) => (
            <p key={s.key} style={{ color: s.color }}>
              {s.label}: {data[hoverIdx][s.key] ?? 0}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
