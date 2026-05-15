// Hand-rolled SVG sparkline. Used in the active peerings table and drawer
// — recharts is overkill for 30-point inline charts and the bundle ceiling
// for this PR is +5KB gzipped.

interface SparklineProps {
  values: number[] // 0..1, ordered oldest → newest
  width?: number
  height?: number
  ariaLabel?: string
}

export function Sparkline({
  values,
  width = 88,
  height = 22,
  ariaLabel = "30-day success rate",
}: SparklineProps) {
  if (values.length === 0) {
    return (
      <svg
        role="img"
        aria-label={ariaLabel}
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
      >
        <title>{ariaLabel}</title>
      </svg>
    )
  }
  const stepX = values.length > 1 ? width / (values.length - 1) : width
  const points = values
    .map((v, i) => `${i * stepX},${height - v * height}`)
    .join(" ")
  // Pick stroke colour by latest value to echo the badge palette.
  const last = values[values.length - 1] ?? 0
  const stroke =
    last >= 0.9 ? "var(--emerald)" : last >= 0.6 ? "var(--gold)" : "var(--rose)"
  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
    >
      <title>{ariaLabel}</title>
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth={1.25}
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
    </svg>
  )
}
